"""Media attached to messages, and the reference strings that point at files.

Every file a wire sees gets a ``ref``: one string with everything needed to
download or re-send it. Pass a ``ref`` (or the :class:`Media` itself) wherever
a file is accepted. Refs belong to the account that saw the file, because
Telegram's access hashes are per account.
"""

from __future__ import annotations

import base64
import struct
from io import BytesIO
from typing import NamedTuple, Optional

from wirelet import schema
from wirelet.schema.core import Blob, Text
from .base import Model

_MAGIC = b"WL"
_HEAD = "<2sBBiqq"
PHOTO, DOCUMENT = 1, 2

# Kinds a Media can have. Files first, then things that are not files.
FILE_KINDS = ("photo", "video", "round", "gif", "audio", "voice", "sticker", "file")


class Ref(NamedTuple):
    """The parts of a file reference."""

    store: int          # PHOTO or DOCUMENT
    dc: int
    id: int
    hash: int
    file_reference: bytes
    size_type: str = ""

    def pack(self) -> str:
        head = struct.pack(_HEAD, _MAGIC, 1, self.store, self.dc, self.id, self.hash)
        data = head + Blob(self.file_reference) + Text(self.size_type)
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    @staticmethod
    def unpack(value: str) -> "Ref":
        try:
            data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            magic, version, store, dc, file_id, access_hash = struct.unpack_from(_HEAD, data)
            if magic != _MAGIC or version != 1:
                raise ValueError
            rest = BytesIO(data[struct.calcsize(_HEAD):])
            return Ref(store, dc, file_id, access_hash, Blob.decode(rest), Text.decode(rest))
        except Exception as e:  # noqa: BLE001
            raise ValueError("not a wirelet file ref") from e

    def as_input_media(self, spoiler: bool = False):
        o = schema.objects
        if self.store == PHOTO:
            return o.InputMediaPhoto(id=o.InputPhoto(
                id=self.id, access_hash=self.hash, file_reference=self.file_reference), spoiler=spoiler or None)
        return o.InputMediaDocument(id=o.InputDocument(
            id=self.id, access_hash=self.hash, file_reference=self.file_reference), spoiler=spoiler or None)

    def location(self):
        o = schema.objects
        if self.store == PHOTO:
            return o.InputPhotoFileLocation(id=self.id, access_hash=self.hash,
                                            file_reference=self.file_reference, thumb_size=self.size_type)
        return o.InputDocumentFileLocation(id=self.id, access_hash=self.hash,
                                           file_reference=self.file_reference, thumb_size=self.size_type)


class Media(Model):
    """What a message carries besides text.

    ``kind`` is one of ``photo video round gif audio voice sticker file`` for
    files (which have a ``ref``), or the schema's name for anything else
    (``poll``, ``geo``, ``contact``, ``dice``...), with details in ``raw``.
    """

    def __init__(self, kind: str, *, ref: Optional[str] = None, size: Optional[int] = None,
                 mime: Optional[str] = None, file_name: Optional[str] = None,
                 width: Optional[int] = None, height: Optional[int] = None,
                 duration: Optional[float] = None, title: Optional[str] = None,
                 performer: Optional[str] = None, emoji: Optional[str] = None,
                 spoiler: bool = False, raw=None) -> None:
        self.kind = kind
        self.ref = ref
        self.size = size
        self.mime = mime
        self.file_name = file_name
        self.width = width
        self.height = height
        self.duration = duration
        self.title = title
        self.performer = performer
        self.emoji = emoji
        self.spoiler = spoiler
        self.raw = raw

    @property
    def is_file(self) -> bool:
        return self.ref is not None


def _photo(wire, photo, spoiler: bool) -> Optional[Media]:
    o = schema.objects
    if not isinstance(photo, o.Photo):
        return None
    sizes = [s for s in photo.sizes if isinstance(s, (o.PhotoSize, o.PhotoSizeProgressive))]
    if not sizes:
        return None

    def weight(s) -> int:
        return s.size if isinstance(s, o.PhotoSize) else max(s.sizes or [0])

    big = max(sizes, key=weight)
    ref = Ref(PHOTO, photo.dc_id, photo.id, photo.access_hash, photo.file_reference, big.type).pack()
    return Media("photo", ref=ref, size=weight(big), mime="image/jpeg", width=big.w,
                 height=big.h, spoiler=spoiler, raw=photo)._attach(wire)


def _document(wire, doc, spoiler: bool, round_hint: bool = False, voice_hint: bool = False) -> Optional[Media]:
    o = schema.objects
    if not isinstance(doc, o.Document):
        return None

    attrs = {type(a): a for a in doc.attributes}
    name = attrs.get(o.DocumentAttributeFilename)
    video = attrs.get(o.DocumentAttributeVideo)
    audio = attrs.get(o.DocumentAttributeAudio)
    sticker = attrs.get(o.DocumentAttributeSticker)
    image = attrs.get(o.DocumentAttributeImageSize)

    if sticker is not None:
        kind = "sticker"
    elif o.DocumentAttributeAnimated in attrs:
        kind = "gif"
    elif video is not None:
        kind = "round" if (video.round_message or round_hint) else "video"
    elif audio is not None:
        kind = "voice" if (audio.voice or voice_hint) else "audio"
    else:
        kind = "file"

    dims = video or image
    return Media(
        kind,
        ref=Ref(DOCUMENT, doc.dc_id, doc.id, doc.access_hash, doc.file_reference).pack(),
        size=doc.size, mime=doc.mime_type, file_name=name.file_name if name else None,
        width=getattr(dims, "w", None), height=getattr(dims, "h", None),
        duration=getattr(video, "duration", None) or getattr(audio, "duration", None),
        title=getattr(audio, "title", None), performer=getattr(audio, "performer", None),
        emoji=getattr(sticker, "alt", None), spoiler=spoiler, raw=doc,
    )._attach(wire)


def media_from(wire, media) -> Optional[Media]:
    """A :class:`Media` for a message's ``media`` field, or None for none/web previews."""
    o = schema.objects
    if media is None or isinstance(media, (o.MessageMediaEmpty, o.MessageMediaWebPage)):
        return None
    if isinstance(media, o.MessageMediaPhoto):
        return _photo(wire, media.photo, bool(media.spoiler)) or Media("photo", raw=media)._attach(wire)
    if isinstance(media, o.MessageMediaDocument):
        return (_document(wire, media.document, bool(media.spoiler), bool(media.round), bool(media.voice))
                or Media("file", raw=media)._attach(wire))
    kind = type(media).__name__.removeprefix("MessageMedia").lower()
    return Media(kind, raw=media)._attach(wire)
