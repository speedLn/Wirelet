"""Photos, documents and the ``file_id`` strings that point at them.

A ``file_id`` is everything needed to download or re-send a file, packed into
one URL-safe string. It is specific to wirelet and to the account that saw the
file, because Telegram's ``file_reference`` and ``access_hash`` are per-user.
"""

from __future__ import annotations

import base64
import struct
from typing import List, NamedTuple, Optional

from wirelet import raw
from wirelet.raw.core import Bytes, String
from .object import Object

FILE_ID_VERSION = 1
PHOTO, DOCUMENT = 1, 2


class FileId(NamedTuple):
    kind: int
    dc_id: int
    id: int
    access_hash: int
    file_reference: bytes
    thumb_size: str = ""

    def encode(self) -> str:
        head = struct.pack("<BBiqq", FILE_ID_VERSION, self.kind, self.dc_id, self.id, self.access_hash)
        body = head + Bytes(self.file_reference) + String(self.thumb_size)
        return base64.urlsafe_b64encode(body).decode().rstrip("=")

    @staticmethod
    def decode(value: str) -> "FileId":
        from io import BytesIO

        try:
            data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            version, kind, dc_id, file_id, access_hash = struct.unpack_from("<BBiqq", data)
            if version != FILE_ID_VERSION:
                raise ValueError
            rest = BytesIO(data[struct.calcsize("<BBiqq"):])
            return FileId(kind, dc_id, file_id, access_hash, Bytes.read(rest), String.read(rest))
        except Exception as e:  # noqa: BLE001
            raise ValueError("This is not a wirelet file_id") from e

    def input_media(self):
        if self.kind == PHOTO:
            return raw.types.InputMediaPhoto(id=raw.types.InputPhoto(
                id=self.id, access_hash=self.access_hash, file_reference=self.file_reference))
        return raw.types.InputMediaDocument(id=raw.types.InputDocument(
            id=self.id, access_hash=self.access_hash, file_reference=self.file_reference))

    def location(self):
        if self.kind == PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=self.id, access_hash=self.access_hash,
                file_reference=self.file_reference, thumb_size=self.thumb_size)
        return raw.types.InputDocumentFileLocation(
            id=self.id, access_hash=self.access_hash,
            file_reference=self.file_reference, thumb_size=self.thumb_size)


class Media(Object):
    file_id: str
    file_size: Optional[int]


class Photo(Media):
    def __init__(self, *, file_id: str, width: int, height: int, file_size: Optional[int], raw=None) -> None:
        self.file_id = file_id
        self.width = width
        self.height = height
        self.file_size = file_size
        self.raw = raw

    @staticmethod
    def _parse(client, photo) -> Optional["Photo"]:
        if not isinstance(photo, raw.types.Photo):
            return None
        sizes = [s for s in photo.sizes if isinstance(s, (raw.types.PhotoSize, raw.types.PhotoSizeProgressive))]
        if not sizes:
            return None

        def size_of(s) -> int:
            return s.size if isinstance(s, raw.types.PhotoSize) else max(s.sizes or [0])

        biggest = max(sizes, key=size_of)
        fid = FileId(PHOTO, photo.dc_id, photo.id, photo.access_hash, photo.file_reference, biggest.type)
        return Photo(file_id=fid.encode(), width=biggest.w, height=biggest.h,
                     file_size=size_of(biggest), raw=photo)._bind(client)


class Document(Media):
    """Any file. Specialised documents (video, audio...) subclass this."""

    def __init__(self, *, file_id: str, file_name: Optional[str], mime_type: str,
                 file_size: int, raw=None, **extra) -> None:
        self.file_id = file_id
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size
        for key, value in extra.items():
            setattr(self, key, value)
        self.raw = raw


class Video(Document):
    pass


class VideoNote(Document):
    pass


class Animation(Document):
    pass


class Audio(Document):
    pass


class Voice(Document):
    pass


class Sticker(Document):
    pass


def parse_document(client, doc, media=None) -> Optional[Document]:
    if not isinstance(doc, raw.types.Document):
        return None

    attrs = {type(a): a for a in doc.attributes}
    t = raw.types
    filename = attrs.get(t.DocumentAttributeFilename)
    video = attrs.get(t.DocumentAttributeVideo)
    audio = attrs.get(t.DocumentAttributeAudio)
    sticker = attrs.get(t.DocumentAttributeSticker)
    image = attrs.get(t.DocumentAttributeImageSize)

    fid = FileId(DOCUMENT, doc.dc_id, doc.id, doc.access_hash, doc.file_reference).encode()
    common = dict(
        file_id=fid,
        file_name=filename.file_name if filename else None,
        mime_type=doc.mime_type,
        file_size=doc.size,
        raw=doc,
    )

    if sticker is not None:
        size = video or image
        cls, extra = Sticker, dict(emoji=sticker.alt, width=getattr(size, "w", None),
                                   height=getattr(size, "h", None),
                                   is_animated=doc.mime_type == "application/x-tgsticker",
                                   is_video=doc.mime_type == "video/webm")
    elif t.DocumentAttributeAnimated in attrs:
        cls, extra = Animation, dict(width=getattr(video, "w", None), height=getattr(video, "h", None),
                                     duration=getattr(video, "duration", None))
    elif video is not None:
        cls = VideoNote if video.round_message else Video
        extra = dict(width=video.w, height=video.h, duration=video.duration,
                     supports_streaming=bool(video.supports_streaming))
    elif audio is not None:
        if audio.voice:
            cls, extra = Voice, dict(duration=audio.duration)
        else:
            cls, extra = Audio, dict(duration=audio.duration, title=audio.title, performer=audio.performer)
    else:
        cls, extra = Document, {}

    return cls(**common, **extra)._bind(client)


MEDIA_ATTRS: List[str] = ["photo", "video", "video_note", "animation", "audio", "voice", "sticker", "document"]
