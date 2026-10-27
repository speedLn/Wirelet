"""Encoding for Telegram's binary format, and the polymorphic decoder.

Every generated class in ``wirelet.schema`` builds on these helpers. Calling a
primitive encodes (``I32(5)`` -> 4 bytes); ``.decode(stream)`` reads one back.
All integers are little-endian, as MTProto requires.
"""

from __future__ import annotations

import gzip
import importlib
import json
import struct
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Type

SEQ_CID = 0x1CB5C415
TRUE_CID = 0x997275B5
FALSE_CID = 0xBC799737
GZIP_CID = 0x3072CFA1
CONTAINER_CID = 0x73F1F8DC
RPC_ANSWER_CID = 0xF35C6D01
RPC_ERROR_CID = 0x2144CA19


class SchemaError(ValueError):
    """Bytes from the server do not match the schema this build knows."""


def _take(s: BytesIO, n: int) -> bytes:
    data = s.read(n)
    if len(data) != n:
        raise SchemaError(f"wanted {n} bytes, stream had {len(data)}")
    return data


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

class I32(bytes):
    WIDTH = 4

    def __new__(cls, value: int, signed: bool = True) -> bytes:  # type: ignore[misc]
        return int(value).to_bytes(cls.WIDTH, "little", signed=signed)

    @classmethod
    def decode(cls, s: BytesIO, signed: bool = True) -> int:
        return int.from_bytes(_take(s, cls.WIDTH), "little", signed=signed)


class I64(I32):
    WIDTH = 8


class I128(I32):
    WIDTH = 16


class I256(I32):
    WIDTH = 32


class F64(bytes):
    def __new__(cls, value: float) -> bytes:  # type: ignore[misc]
        return struct.pack("<d", value)

    @staticmethod
    def decode(s: BytesIO, *_: Any) -> float:
        return struct.unpack("<d", _take(s, 8))[0]


class Blob(bytes):
    """Length-prefixed bytes, padded to 4."""

    def __new__(cls, value: bytes) -> bytes:  # type: ignore[misc]
        value = bytes(value)
        n = len(value)
        if n <= 253:
            return bytes([n]) + value + bytes(-(n + 1) % 4)
        return b"\xfe" + n.to_bytes(3, "little") + value + bytes(-n % 4)

    @staticmethod
    def decode(s: BytesIO, *_: Any) -> bytes:
        n = _take(s, 1)[0]
        if n <= 253:
            data = _take(s, n)
            s.read(-(n + 1) % 4)
        else:
            n = int.from_bytes(_take(s, 3), "little")
            data = _take(s, n)
            s.read(-n % 4)
        return data


class Text(Blob):
    """A UTF-8 string, encoded like :class:`Blob`."""

    def __new__(cls, value: str) -> bytes:  # type: ignore[misc]
        return super().__new__(cls, value.encode("utf-8"))

    @staticmethod
    def decode(s: BytesIO, *_: Any) -> str:
        return Blob.decode(s).decode("utf-8", errors="replace")


class Boolean(bytes):
    def __new__(cls, value: bool) -> bytes:  # type: ignore[misc]
        return I32(TRUE_CID if value else FALSE_CID, False)

    @staticmethod
    def decode(s: BytesIO, *_: Any) -> bool:
        cid = I32.decode(s, False)
        if cid == TRUE_CID:
            return True
        if cid == FALSE_CID:
            return False
        raise SchemaError(f"expected a Bool, got 0x{cid:08x}")


class Seq(bytes):
    """A boxed vector."""

    def __new__(cls, values: List[Any], item: Optional[Callable[[Any], bytes]] = None) -> bytes:  # type: ignore[misc]
        item = item or encode_struct
        return b"".join([I32(SEQ_CID, False), I32(len(values))] + [item(v) for v in values])

    @staticmethod
    def decode(s: BytesIO, item: Optional[Callable[[BytesIO], Any]] = None) -> List[Any]:
        cid = I32.decode(s, False)
        if cid != SEQ_CID:
            raise SchemaError(f"expected a Vector, got 0x{cid:08x}")
        return decode_bare_seq(s, item)


def decode_bare_seq(s: BytesIO, item: Optional[Callable[[BytesIO], Any]] = None) -> List[Any]:
    item = item or Struct.decode
    return [item(s) for _ in range(I32.decode(s))]


def encode_struct(obj: "Struct") -> bytes:
    return obj.encode()


# --------------------------------------------------------------------------- #
# Structs
# --------------------------------------------------------------------------- #

_cache: Dict[int, Type["Struct"]] = {}


def class_for(cid: int) -> Type["Struct"]:
    found = _cache.get(cid)
    if found is not None:
        return found

    from wirelet.schema.registry import NAMES

    name = NAMES.get(cid)
    if name is None:
        raise SchemaError(
            f"unknown constructor 0x{cid:08x}: Telegram is probably on a newer layer "
            f"than this wirelet build - upgrade wirelet"
        )
    *module, cls_name = name.split(".")
    cls = getattr(importlib.import_module("wirelet.schema." + ".".join(module)), cls_name)
    _cache[cid] = cls
    return cls


class Struct:
    """Base of every generated method and object."""

    __slots__ = ()

    CID: int = 0
    NAME: str = "Struct"
    TL: str = ""

    @staticmethod
    def decode(s: BytesIO, *args: Any) -> Any:
        cid = I32.decode(s, False)
        if cid == GZIP_CID:
            return Struct.decode(BytesIO(gzip.decompress(Blob.decode(s))))
        if cid == SEQ_CID:
            return decode_bare_seq(s)
        if cid == TRUE_CID:
            return True
        if cid == FALSE_CID:
            return False
        if cid == CONTAINER_CID:
            return Container.decode(s)
        if cid == RPC_ANSWER_CID:
            return RpcAnswer.decode(s)
        return class_for(cid).decode(s, *args)

    def encode(self) -> bytes:
        raise NotImplementedError

    def __bytes__(self) -> bytes:
        return self.encode()

    def fields(self):
        """Names of this struct's fields, in schema order."""
        for cls in type(self).__mro__:
            yield from getattr(cls, "__slots__", ())

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"@": self.TL}
        for name in self.fields():
            value = getattr(self, name, None)
            if value is not None and value is not False:
                out[name] = _plain(value)
        return out

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{n}={getattr(self, n, None)!r}" for n in self.fields()
            if getattr(self, n, None) not in (None, False)
        )
        return f"schema.{self.NAME}({shown})"

    def __str__(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False, default=str)

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return NotImplemented

        # an unset ``flags.N?true`` is None when built, False when decoded
        def norm(v: Any) -> Any:
            return None if v is False else v

        return all(norm(getattr(self, n, None)) == norm(getattr(other, n, None)) for n in self.fields())

    def __hash__(self) -> int:
        return id(self)


def _plain(value: Any) -> Any:
    if isinstance(value, Struct):
        return value.as_dict()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def decode_result(
    s: BytesIO,
    query: Optional[Struct] = None,
    item: Optional[Callable[[BytesIO], Any]] = None,
    primitive: Optional[Callable[[BytesIO], Any]] = None,
) -> Any:
    """Decode the answer to a method whose return type is known.

    Telegram does not tag ``Vector<long>`` items, so the method that was sent
    says how to read its answer; generated methods pass that here.
    """
    start = s.tell()
    cid = I32.decode(s, False)

    if cid == GZIP_CID:
        return decode_result(BytesIO(gzip.decompress(Blob.decode(s))), query, item, primitive)
    if cid == RPC_ERROR_CID:
        s.seek(start)
        return Struct.decode(s)
    if query is not None:
        s.seek(start)
        return query.decode_result(s)
    if item is not None:
        if cid != SEQ_CID:
            raise SchemaError(f"expected a Vector answer, got 0x{cid:08x}")
        return decode_bare_seq(s, item)

    s.seek(start)
    return primitive(s) if primitive is not None else Struct.decode(s)


# --------------------------------------------------------------------------- #
# MTProto service structures parsed by hand
# --------------------------------------------------------------------------- #

class Envelope:
    """One MTProto message inside a container, its body still undecoded."""

    __slots__ = ("msg_id", "seq_no", "data")

    def __init__(self, msg_id: int, seq_no: int, data: bytes) -> None:
        self.msg_id = msg_id
        self.seq_no = seq_no
        self.data = data


class Container:
    CID = CONTAINER_CID

    __slots__ = ("envelopes",)

    def __init__(self, envelopes: List[Envelope]) -> None:
        self.envelopes = envelopes

    @staticmethod
    def decode(s: BytesIO, *_: Any) -> "Container":
        out = []
        for _ in range(I32.decode(s)):
            msg_id, seq_no, length = I64.decode(s), I32.decode(s), I32.decode(s)
            out.append(Envelope(msg_id, seq_no, _take(s, length)))
        return Container(out)

    def encode(self) -> bytes:
        parts = [I32(self.CID, False), I32(len(self.envelopes))]
        for e in self.envelopes:
            parts += [I64(e.msg_id), I32(e.seq_no), I32(len(e.data)), e.data]
        return b"".join(parts)


class RpcAnswer:
    """``rpc_result``: the answer to the request sent as ``req_msg_id``."""

    CID = RPC_ANSWER_CID

    __slots__ = ("req_msg_id", "data")

    def __init__(self, req_msg_id: int, data: bytes) -> None:
        self.req_msg_id = req_msg_id
        self.data = data

    @staticmethod
    def decode(s: BytesIO, *_: Any) -> "RpcAnswer":
        return RpcAnswer(I64.decode(s), s.read())
