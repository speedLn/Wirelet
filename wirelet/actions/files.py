"""Uploading and downloading files."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import mimetypes
import os
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Optional, Union

from wirelet import faults, schema, utils
from wirelet.model import Media, Message, Ref

PART = 512 * 1024
BIG = 10 * 1024 * 1024
CHUNK = 1024 * 1024
PARALLEL = 4

KIND_BY_EXT = {
    "photo": (".jpg", ".jpeg", ".png", ".heic", ".heif"),
    "video": (".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"),
    "gif": (".gif",),
    "audio": (".mp3", ".m4a", ".flac", ".wav", ".aac"),
    "voice": (".ogg", ".oga", ".opus"),
    "sticker": (".tgs",),
}
KINDS = ("photo", "video", "round", "gif", "audio", "voice", "sticker", "file")


async def _tick(progress, done: int, total: int) -> None:
    if progress is None:
        return
    result = progress(done, total)
    if inspect.isawaitable(result):
        await result


def _url(value) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _guess_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    for kind, exts in KIND_BY_EXT.items():
        if ext in exts:
            return kind
    return "file"


class FileActions:
    async def upload(self, file, *, name: Optional[str] = None, progress=None) -> "schema.kinds.InputFile":
        """Upload a file once and get an ``InputFile`` usable in any schema method."""
        if isinstance(file, (str, Path)):
            path = Path(file)
            size, name, stream, owned = path.stat().st_size, name or path.name, path.open("rb"), True
        elif isinstance(file, (bytes, bytearray)):
            size, name, stream, owned = len(file), name or "file", BytesIO(file), True
        else:
            stream, owned = file, False
            start = stream.tell()
            stream.seek(0, os.SEEK_END)
            size = stream.tell() - start
            stream.seek(start)
            name = name or Path(getattr(file, "name", "file")).name
        if size == 0:
            raise ValueError("can't upload an empty file")

        big = size > BIG
        parts = (size + PART - 1) // PART
        file_id = utils.random_id()
        digest = None if big else hashlib.md5()
        session = await self._session_for(self.storage.dc_id, media=True) if big else self.session

        queue: asyncio.Queue = asyncio.Queue(maxsize=PARALLEL * 2)
        done = 0

        async def worker() -> None:
            nonlocal done
            while True:
                job = await queue.get()
                if job is None:
                    return
                index, chunk = job
                method = (schema.methods.upload.SaveBigFilePart(
                    file_id=file_id, file_part=index, file_total_parts=parts, bytes=chunk) if big else
                    schema.methods.upload.SaveFilePart(file_id=file_id, file_part=index, bytes=chunk))
                for attempt in range(5):
                    try:
                        if not await session.invoke(method, timeout=60):
                            raise faults.from_telegram(400, "FILE_PART_REJECTED", method.TL)
                        break
                    except faults.FLOOD_WAIT as e:
                        await asyncio.sleep((e.amount or 1) + 1)
                    except (asyncio.TimeoutError, ConnectionError, faults.ServerTrouble):
                        if attempt == 4:
                            raise
                        await asyncio.sleep(1 + attempt)
                done += len(chunk)
                await _tick(progress, min(done, size), size)

        workers = [asyncio.create_task(worker()) for _ in range(PARALLEL if big else 1)]
        try:
            for index in range(parts):
                chunk = await asyncio.to_thread(stream.read, PART)
                if digest is not None:
                    digest.update(chunk)
                while True:
                    failed = next((w for w in workers if w.done() and w.exception()), None)
                    if failed is not None:
                        raise failed.exception()
                    try:
                        await asyncio.wait_for(queue.put((index, chunk)), 1)
                        break
                    except asyncio.TimeoutError:
                        continue
            for _ in workers:
                await queue.put(None)
            await asyncio.gather(*workers)
        except BaseException:
            for w in workers:
                w.cancel()
            raise
        finally:
            if owned:
                stream.close()

        o = schema.objects
        if big:
            return o.InputFileBig(id=file_id, parts=parts, name=name)
        return o.InputFile(id=file_id, parts=parts, name=name, md5_checksum=digest.hexdigest())

    async def _input_media(self, file, kind: Optional[str], *, name=None, mime=None, thumb=None,
                           spoiler=False, ttl=None, duration=None, width=None, height=None,
                           title=None, performer=None, progress=None):
        o = schema.objects
        if kind is not None and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")

        if isinstance(file, Media):
            if file.ref is None:
                raise ValueError(f"this {file.kind} has no file to send")
            file = file.ref

        if isinstance(file, str) and not _url(file) and not Path(file).exists():
            try:
                return Ref.unpack(file).as_input_media(spoiler)
            except ValueError:
                raise FileNotFoundError(f"{file!r} is not a file, a URL or a wirelet ref") from None

        if _url(file):
            if (kind or _guess_kind(file.split("?")[0])) == "photo":
                return o.InputMediaPhotoExternal(url=file, spoiler=spoiler or None, ttl_seconds=ttl)
            return o.InputMediaDocumentExternal(url=file, spoiler=spoiler or None, ttl_seconds=ttl)

        if isinstance(file, (str, Path)):
            name = name or Path(file).name
        elif not isinstance(file, (bytes, bytearray)):
            name = name or Path(getattr(file, "name", "file")).name
        kind = kind or (_guess_kind(name) if name else "file")

        uploaded = await self.upload(file, name=name, progress=progress)
        if kind == "photo":
            return o.InputMediaUploadedPhoto(file=uploaded, spoiler=spoiler or None, ttl_seconds=ttl)

        name = name or uploaded.name
        attributes = [o.DocumentAttributeFilename(file_name=name)]
        default_mime = {"voice": "audio/ogg", "round": "video/mp4", "gif": "video/mp4",
                        "sticker": "application/x-tgsticker" if name.endswith(".tgs") else "image/webp"}
        mime = mime or mimetypes.guess_type(name)[0] or default_mime.get(kind) or "application/octet-stream"

        if kind in ("video", "round", "gif"):
            attributes.append(o.DocumentAttributeVideo(
                duration=duration or 0, w=width or 0, h=height or 0,
                round_message=(kind == "round") or None, supports_streaming=(kind == "video") or None))
            if kind == "gif":
                attributes.append(o.DocumentAttributeAnimated())
        elif kind in ("audio", "voice"):
            attributes.append(o.DocumentAttributeAudio(
                duration=int(duration or 0), voice=(kind == "voice") or None, title=title, performer=performer))
        elif kind == "sticker":
            attributes.append(o.DocumentAttributeSticker(alt="", stickerset=o.InputStickerSetEmpty()))

        return o.InputMediaUploadedDocument(
            file=uploaded, mime_type=mime, attributes=attributes,
            thumb=await self.upload(thumb) if thumb is not None else None,
            force_file=(kind == "file") or None, spoiler=spoiler or None, ttl_seconds=ttl,
            nosound_video=(kind == "gif") or None,
        )

    async def download(self, target: Union[Message, Media, str], to=None, *, memory: bool = False,
                       progress=None) -> Union[str, BytesIO, None]:
        """Download a message's file, a :class:`Media` or a ``ref``.

        Saves into ``downloads/`` unless ``to`` names a file or folder, and
        returns the path - or a ``BytesIO`` with ``memory=True``. Returns None
        for a message without a file.
        """
        message = target if isinstance(target, Message) else None
        media = message.media if message is not None else target
        if media is None or (isinstance(media, Media) and media.ref is None):
            return None

        ref_string = media.ref if isinstance(media, Media) else media
        size = getattr(media, "size", None) or 0
        filename = getattr(media, "file_name", None) or _default_name(media)
        ref = Ref.unpack(ref_string)

        for attempt in range(2):
            try:
                return await self._fetch_file(ref, size, to, filename, memory, progress)
            except faults.FILE_REFERENCE_EXPIRED:
                if message is None or attempt:
                    raise
                # References expire; a fresh copy of the message carries a new one.
                fresh = await self.fetch(message.chat.id, message.id)
                ref = Ref.unpack(fresh.media.ref)
        return None

    async def _fetch_file(self, ref: Ref, size: int, to, filename: str, memory: bool, progress):
        session = await self._session_for(ref.dc, media=True)
        location = ref.location()

        if memory:
            out: BinaryIO = BytesIO()
            final = partial = None
        else:
            final = Path(to) if to else Path("downloads") / filename
            if to and (str(to).endswith(("/", os.sep)) or Path(to).is_dir()):
                final = Path(to) / filename
            final.parent.mkdir(parents=True, exist_ok=True)
            partial = final.with_name(final.name + ".part")
            out = partial.open("wb")

        offset = 0
        try:
            while True:
                try:
                    chunk = await session.invoke(schema.methods.upload.GetFile(
                        location=location, offset=offset, limit=CHUNK), timeout=60)
                except faults.FILE_MIGRATE as e:
                    session = await self._session_for(e.amount, media=True)
                    continue
                except faults.FLOOD_WAIT as e:
                    await asyncio.sleep((e.amount or 1) + 1)
                    continue
                data = chunk.bytes
                if data:
                    await asyncio.to_thread(out.write, data)
                    offset += len(data)
                    await _tick(progress, offset, size or offset)
                if len(data) < CHUNK:
                    break
        except BaseException:
            if partial is not None:
                out.close()
                partial.unlink(missing_ok=True)
            raise

        if memory:
            out.seek(0)
            out.name = filename
            return out
        out.close()
        partial.replace(final)
        return str(final)


def _default_name(media) -> str:
    kind = getattr(media, "kind", "file")
    mime = getattr(media, "mime", None)
    ext = (mimetypes.guess_extension(mime) if mime else None) or (".jpg" if kind == "photo" else "")
    return f"{kind}_{utils.random_id() & 0xFFFFFFFF:08x}{ext}"
