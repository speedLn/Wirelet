from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import mimetypes
import os
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Callable, Optional, Union

from wirelet import errors, raw, utils
from wirelet.types import FileId, Message
from wirelet.types.media import Media

log = logging.getLogger(__name__)

FileLike = Union[str, Path, bytes, BinaryIO]
Progress = Optional[Callable[[int, int], object]]

PART_SIZE = 512 * 1024
BIG_FILE = 10 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024
UPLOAD_WORKERS = 4


async def _report(progress: Progress, done: int, total: int) -> None:
    if progress is None:
        return
    result = progress(done, total)
    if inspect.isawaitable(result):
        await result


def _is_url(value) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


class Media_:
    # -- upload ---------------------------------------------------------- #

    async def save_file(self, file: FileLike, *, file_name: Optional[str] = None,
                        progress: Progress = None) -> "raw.base.InputFile":
        """Upload a file and return an InputFile to use in any raw media request."""
        if isinstance(file, (str, Path)):
            path = Path(file)
            size = path.stat().st_size
            name = file_name or path.name
            stream: BinaryIO = path.open("rb")
            close = True
        elif isinstance(file, (bytes, bytearray)):
            stream, size, name, close = BytesIO(file), len(file), file_name or "file", True
        else:
            stream = file
            start = stream.tell()
            stream.seek(0, os.SEEK_END)
            size = stream.tell() - start
            stream.seek(start)
            name = file_name or Path(getattr(file, "name", "file")).name
            close = False

        if size == 0:
            raise ValueError("Can't upload an empty file")

        is_big = size > BIG_FILE
        parts = (size + PART_SIZE - 1) // PART_SIZE
        file_id = utils.random_id()
        md5 = hashlib.md5() if not is_big else None
        session = await self._session_for(self.storage.dc_id, media=True) if is_big else self.session

        queue: asyncio.Queue = asyncio.Queue(maxsize=UPLOAD_WORKERS * 2)
        done = 0

        async def worker() -> None:
            nonlocal done
            while True:
                item = await queue.get()
                if item is None:
                    return
                index, chunk = item
                if is_big:
                    query = raw.functions.upload.SaveBigFilePart(
                        file_id=file_id, file_part=index, file_total_parts=parts, bytes=chunk)
                else:
                    query = raw.functions.upload.SaveFilePart(file_id=file_id, file_part=index, bytes=chunk)
                for attempt in range(5):
                    try:
                        if not await session.invoke(query, timeout=60):
                            raise errors.named("FilePartFailed")("Telegram refused a file part", 400)
                        break
                    except (asyncio.TimeoutError, ConnectionError, errors.InternalServerError):
                        if attempt == 4:
                            raise
                        await asyncio.sleep(1 + attempt)
                    except errors.FloodWait as e:
                        await asyncio.sleep((e.value or 1) + 1)
                done += len(chunk)
                await _report(progress, min(done, size), size)

        workers = [asyncio.create_task(worker()) for _ in range(UPLOAD_WORKERS if is_big else 1)]
        try:
            for index in range(parts):
                chunk = await asyncio.to_thread(stream.read, PART_SIZE)
                if md5 is not None:
                    md5.update(chunk)
                while not await _put(queue, (index, chunk), workers):
                    pass
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
        except BaseException:
            for w in workers:
                w.cancel()
            raise
        finally:
            if close:
                stream.close()

        if is_big:
            return raw.types.InputFileBig(id=file_id, parts=parts, name=name)
        return raw.types.InputFile(id=file_id, parts=parts, name=name, md5_checksum=md5.hexdigest())

    # -- sending media --------------------------------------------------- #

    async def _send_media(self, chat_id, media, caption: str, parse_mode, caption_entities,
                          reply_to_message_id, message_thread_id, reply_markup,
                          disable_notification: bool, protect_content: bool, schedule_date) -> Message:
        peer = await self.resolve_peer(chat_id)
        text, entities = await self._parse_text(caption or "", parse_mode, caption_entities)
        random_id = utils.random_id()
        query = raw.functions.messages.SendMedia(
            peer=peer, media=media, message=text, random_id=random_id, entities=entities,
            reply_to=self._reply_to(reply_to_message_id, message_thread_id),
            reply_markup=await self._markup(reply_markup),
            silent=disable_notification or None, noforwards=protect_content or None,
            schedule_date=utils.datetime_to_timestamp(schedule_date),
        )
        result = await self.invoke(query)
        return await self._sent_message(result, peer, random_id)

    async def _input_media(self, file, kind: str, *, file_name=None, mime_type=None,
                           attributes=None, thumb=None, progress=None, spoiler=False,
                           force_document=False, ttl_seconds=None):
        t = raw.types
        if isinstance(file, Media):
            file = file.file_id

        if isinstance(file, str) and not _is_url(file) and not Path(file).exists():
            try:
                fid = FileId.decode(file)
            except ValueError:
                raise FileNotFoundError(f"{file!r} is neither a file on disk, a URL nor a file_id") from None
            media = fid.input_media()
            media.spoiler = spoiler or None
            return media

        if _is_url(file):
            if kind == "photo":
                return t.InputMediaPhotoExternal(url=file, spoiler=spoiler or None, ttl_seconds=ttl_seconds)
            return t.InputMediaDocumentExternal(url=file, spoiler=spoiler or None, ttl_seconds=ttl_seconds)

        uploaded = await self.save_file(file, file_name=file_name, progress=progress)

        if kind == "photo":
            return t.InputMediaUploadedPhoto(file=uploaded, spoiler=spoiler or None, ttl_seconds=ttl_seconds)

        name = file_name or uploaded.name
        mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        attrs = [t.DocumentAttributeFilename(file_name=name), *(attributes or [])]
        thumb_file = await self.save_file(thumb) if thumb is not None else None
        return t.InputMediaUploadedDocument(
            file=uploaded, mime_type=mime, attributes=attrs, thumb=thumb_file,
            force_file=force_document or None, spoiler=spoiler or None, ttl_seconds=ttl_seconds,
            nosound_video=(kind == "animation") or None,
        )

    def _media_kwargs(self, kwargs):
        from .messages import _DEFAULT
        return dict(
            caption=kwargs.pop("caption", ""),
            parse_mode=kwargs.pop("parse_mode", _DEFAULT),
            caption_entities=kwargs.pop("caption_entities", None),
            reply_to_message_id=kwargs.pop("reply_to_message_id", None),
            message_thread_id=kwargs.pop("message_thread_id", None),
            reply_markup=kwargs.pop("reply_markup", None),
            disable_notification=kwargs.pop("disable_notification", False),
            protect_content=kwargs.pop("protect_content", False),
            schedule_date=kwargs.pop("schedule_date", None),
        )

    async def send_photo(self, chat_id, photo: FileLike, **kwargs) -> Message:
        """Send a photo from a path, bytes, a file object, a URL or a file_id.

        Also takes ``caption``, ``parse_mode``, ``reply_to_message_id``,
        ``reply_markup``, ``has_spoiler``, ``ttl_seconds``, ``progress`` and the
        other options of :meth:`send_message`.
        """
        common = self._media_kwargs(kwargs)
        media = await self._input_media(photo, "photo", progress=kwargs.pop("progress", None),
                                        spoiler=kwargs.pop("has_spoiler", False),
                                        ttl_seconds=kwargs.pop("ttl_seconds", None))
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    async def send_document(self, chat_id, document: FileLike, **kwargs) -> Message:
        """Send any file as a document. ``file_name``, ``mime_type``, ``thumb``
        and ``force_document`` are accepted too."""
        common = self._media_kwargs(kwargs)
        media = await self._input_media(
            document, "document", file_name=kwargs.pop("file_name", None),
            mime_type=kwargs.pop("mime_type", None), thumb=kwargs.pop("thumb", None),
            progress=kwargs.pop("progress", None), force_document=kwargs.pop("force_document", True))
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    async def send_video(self, chat_id, video: FileLike, *, duration: float = 0, width: int = 0,
                         height: int = 0, supports_streaming: bool = True, **kwargs) -> Message:
        common = self._media_kwargs(kwargs)
        attrs = [raw.types.DocumentAttributeVideo(duration=duration, w=width, h=height,
                                                  supports_streaming=supports_streaming or None)]
        media = await self._input_media(
            video, "video", file_name=kwargs.pop("file_name", None), mime_type=kwargs.pop("mime_type", "video/mp4"),
            attributes=attrs, thumb=kwargs.pop("thumb", None), progress=kwargs.pop("progress", None),
            spoiler=kwargs.pop("has_spoiler", False), ttl_seconds=kwargs.pop("ttl_seconds", None))
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    async def send_animation(self, chat_id, animation: FileLike, *, duration: float = 0, width: int = 0,
                             height: int = 0, **kwargs) -> Message:
        common = self._media_kwargs(kwargs)
        attrs = [raw.types.DocumentAttributeAnimated(),
                 raw.types.DocumentAttributeVideo(duration=duration, w=width, h=height)]
        media = await self._input_media(
            animation, "animation", file_name=kwargs.pop("file_name", None),
            mime_type=kwargs.pop("mime_type", "video/mp4"), attributes=attrs,
            progress=kwargs.pop("progress", None))
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    async def send_audio(self, chat_id, audio: FileLike, *, duration: int = 0, title: Optional[str] = None,
                         performer: Optional[str] = None, **kwargs) -> Message:
        common = self._media_kwargs(kwargs)
        attrs = [raw.types.DocumentAttributeAudio(duration=duration, title=title, performer=performer)]
        media = await self._input_media(
            audio, "audio", file_name=kwargs.pop("file_name", None), mime_type=kwargs.pop("mime_type", None),
            attributes=attrs, thumb=kwargs.pop("thumb", None), progress=kwargs.pop("progress", None))
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    async def send_voice(self, chat_id, voice: FileLike, *, duration: int = 0, **kwargs) -> Message:
        """Send a voice note (OGG/Opus works best)."""
        common = self._media_kwargs(kwargs)
        attrs = [raw.types.DocumentAttributeAudio(duration=duration, voice=True)]
        media = await self._input_media(
            voice, "voice", file_name=kwargs.pop("file_name", None),
            mime_type=kwargs.pop("mime_type", "audio/ogg"), attributes=attrs,
            progress=kwargs.pop("progress", None))
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    async def send_sticker(self, chat_id, sticker: FileLike, **kwargs) -> Message:
        """Send a sticker by file_id, or a .webp / .tgs / .webm file."""
        common = self._media_kwargs(kwargs)
        common["caption"] = ""
        name = kwargs.pop("file_name", None) or (Path(sticker).name if isinstance(sticker, (str, Path))
                                                 and Path(sticker).exists() else "sticker.webp")
        mime = {".tgs": "application/x-tgsticker", ".webm": "video/webm"}.get(Path(name).suffix, "image/webp")
        media = await self._input_media(sticker, "sticker", file_name=name, mime_type=mime)
        _no_extra(kwargs)
        return await self._send_media(chat_id, media, **common)

    # -- download -------------------------------------------------------- #

    async def download_media(
        self,
        media: Union[Message, Media, str],
        file_name: Optional[Union[str, Path]] = None,
        *,
        in_memory: bool = False,
        progress: Progress = None,
    ) -> Union[str, BytesIO, None]:
        """Download a message's media, a media object or a file_id.

        Saves into ``downloads/`` by default and returns the path, or returns
        a BytesIO with ``in_memory=True``. Returns None if there is no media.
        """
        message = media if isinstance(media, Message) else None
        if message is not None:
            media = message.file
            if media is None:
                return None

        file_id = media.file_id if isinstance(media, Media) else media
        size = getattr(media, "file_size", None) or 0
        default_name = getattr(media, "file_name", None) or _default_name(media)
        fid = FileId.decode(file_id)

        for attempt in range(2):
            try:
                return await self._download(fid, size, file_name, default_name, in_memory, progress)
            except errors.FileReferenceExpired:
                if message is None or attempt:
                    raise
                # File references expire; a fresh copy of the message has a new one.
                fresh = (await self.get_messages(message.chat.id, [message.id]))[0]
                fid = FileId.decode(fresh.file.file_id)
        return None

    async def _download(self, fid: FileId, size: int, file_name, default_name: str,
                        in_memory: bool, progress: Progress):
        session = await self._session_for(fid.dc_id, media=True)
        location = fid.location()

        if in_memory:
            out: BinaryIO = BytesIO()
            target = None
        else:
            path = Path(file_name) if file_name else Path("downloads") / default_name
            if file_name and (str(file_name).endswith(("/", os.sep)) or Path(file_name).is_dir()):
                path = Path(file_name) / default_name
            path.parent.mkdir(parents=True, exist_ok=True)
            target = path
            out = path.with_name(path.name + ".part").open("wb")

        offset = 0
        try:
            while True:
                try:
                    chunk = await session.invoke(raw.functions.upload.GetFile(
                        location=location, offset=offset, limit=DOWNLOAD_CHUNK, precise=None,
                    ), timeout=60)
                except errors.FileMigrate as e:
                    session = await self._session_for(e.value, media=True)
                    continue
                except errors.FloodWait as e:
                    await asyncio.sleep((e.value or 1) + 1)
                    continue

                data = chunk.bytes
                if data:
                    await asyncio.to_thread(out.write, data)
                    offset += len(data)
                    await _report(progress, offset, size or offset)
                if len(data) < DOWNLOAD_CHUNK:
                    break
        except BaseException:
            if target is not None:
                out.close()
                target.with_name(target.name + ".part").unlink(missing_ok=True)
            raise

        if in_memory:
            out.seek(0)
            out.name = default_name
            return out

        out.close()
        final = target.with_name(target.name + ".part")
        final.replace(target)
        return str(target)


async def _put(queue: asyncio.Queue, item, workers) -> bool:
    """Put ``item`` unless a worker already failed (then raise its error)."""
    for w in workers:
        if w.done() and w.exception() is not None:
            raise w.exception()
    try:
        await asyncio.wait_for(queue.put(item), 1)
        return True
    except asyncio.TimeoutError:
        return False


def _default_name(media) -> str:
    ext = ""
    mime = getattr(media, "mime_type", None)
    if mime:
        ext = mimetypes.guess_extension(mime) or ""
    elif type(media).__name__ == "Photo":
        ext = ".jpg"
    kind = type(media).__name__.lower() if not isinstance(media, str) else "file"
    return f"{kind}_{utils.random_id() & 0xFFFFFFFF:08x}{ext}"


def _no_extra(kwargs: dict) -> None:
    if kwargs:
        raise TypeError(f"Unexpected argument(s): {', '.join(kwargs)}")
