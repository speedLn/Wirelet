"""TL serialization primitives and the polymorphic object reader.

Everything generated into ``wirelet.raw.types`` and ``wirelet.raw.functions``
builds on the helpers here. Integers are little-endian as MTProto requires.
"""

from __future__ import annotations

import gzip
import importlib
import json
import struct
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Type

VECTOR_ID = 0x1CB5C415
BOOL_TRUE_ID = 0x997275B5
BOOL_FALSE_ID = 0xBC799737
GZIP_PACKED_ID = 0x3072CFA1
MSG_CONTAINER_ID = 0x73F1F8DC
RPC_RESULT_ID = 0xF35C6D01
RPC_ERROR_ID = 0x2144CA19


class TLDecodeError(ValueError):
    """The server sent bytes that do not match the schema this build knows."""


# --------------------------------------------------------------------------- #
# Primitives. Calling the class serializes, ``.read`` deserializes.
# --------------------------------------------------------------------------- #

def _read_exact(b: BytesIO, n: int) -> bytes:
    data = b.read(n)
    if len(data) != n:
        raise TLDecodeError(f"Expected {n} bytes, got {len(data)}")
    return data


class Int(bytes):
    SIZE = 4

    def __new__(cls, value: int, signed: bool = True) -> bytes:  # type: ignore[misc]
        return int(value).to_bytes(cls.SIZE, "little", signed=signed)

    @classmethod
    def read(cls, b: BytesIO, signed: bool = True) -> int:
        return int.from_bytes(_read_exact(b, cls.SIZE), "little", signed=signed)


class Long(Int):
    SIZE = 8


class Int128(Int):
    SIZE = 16


class Int256(Int):
    SIZE = 32


class Double(bytes):
    def __new__(cls, value: float) -> bytes:  # type: ignore[misc]
        return struct.pack("<d", value)

    @staticmethod
    def read(b: BytesIO, *args: Any) -> float:
        return struct.unpack("<d", _read_exact(b, 8))[0]


class Bytes(bytes):
    def __new__(cls, value: bytes) -> bytes:  # type: ignore[misc]
        value = bytes(value)
        length = len(value)

        if length <= 253:
            head = bytes([length])
            padding = -(length + 1) % 4
        else:
            head = b"\xfe" + length.to_bytes(3, "little")
            padding = -length % 4

        return head + value + bytes(padding)

    @staticmethod
    def read(b: BytesIO, *args: Any) -> bytes:
        length = _read_exact(b, 1)[0]

        if length <= 253:
            data = _read_exact(b, length)
            b.read(-(length + 1) % 4)
        else:
            length = int.from_bytes(_read_exact(b, 3), "little")
            data = _read_exact(b, length)
            b.read(-length % 4)

        return data


class String(Bytes):
    def __new__(cls, value: str) -> bytes:  # type: ignore[misc]
        return super().__new__(cls, value.encode("utf-8"))

    @staticmethod
    def read(b: BytesIO, *args: Any) -> str:
        return Bytes.read(b).decode("utf-8", errors="replace")


class Bool(bytes):
    def __new__(cls, value: bool) -> bytes:  # type: ignore[misc]
        return Int(BOOL_TRUE_ID if value else BOOL_FALSE_ID, False)

    @staticmethod
    def read(b: BytesIO, *args: Any) -> bool:
        cid = Int.read(b, False)
        if cid == BOOL_TRUE_ID:
            return True
        if cid == BOOL_FALSE_ID:
            return False
        raise TLDecodeError(f"Expected a Bool, got constructor 0x{cid:08x}")


class Vector(bytes):
    def __new__(cls, values: List[Any], item: Callable[[Any], bytes] = None) -> bytes:  # type: ignore[misc]
        item = item or write_object
        return b"".join([Int(VECTOR_ID, False), Int(len(values))] + [item(v) for v in values])

    @staticmethod
    def read(b: BytesIO, item: Callable[[BytesIO], Any] = None) -> List[Any]:
        cid = Int.read(b, False)
        if cid != VECTOR_ID:
            raise TLDecodeError(f"Expected a Vector, got constructor 0x{cid:08x}")
        return read_bare_vector(b, item)


def read_bare_vector(b: BytesIO, item: Callable[[BytesIO], Any] = None) -> List[Any]:
    item = item or TLObject.read
    count = Int.read(b)
    return [item(b) for _ in range(count)]


def write_object(obj: "TLObject") -> bytes:
    return obj.write()


# --------------------------------------------------------------------------- #
# Objects
# --------------------------------------------------------------------------- #

_classes: Dict[int, Type["TLObject"]] = {}


def _class_for(cid: int) -> Type["TLObject"]:
    cls = _classes.get(cid)
    if cls is not None:
        return cls

    from wirelet.raw.all import OBJECTS

    qualname = OBJECTS.get(cid)
    if qualname is None:
        raise TLDecodeError(
            f"Unknown constructor 0x{cid:08x}. The server may be using a newer "
            f"layer than this build of wirelet; try upgrading."
        )

    parts = qualname.split(".")
    module = importlib.import_module("wirelet.raw." + ".".join(parts[:-1]))
    cls = getattr(module, parts[-1])
    _classes[cid] = cls
    return cls


class TLObject:
    __slots__ = ()

    ID: int = 0
    QUALNAME: str = "TLObject"
    TL_NAME: str = ""

    @staticmethod
    def read(b: BytesIO, *args: Any) -> Any:
        cid = Int.read(b, False)

        if cid == GZIP_PACKED_ID:
            return TLObject.read(BytesIO(gzip.decompress(Bytes.read(b))))
        if cid == VECTOR_ID:
            return read_bare_vector(b)
        if cid == BOOL_TRUE_ID:
            return True
        if cid == BOOL_FALSE_ID:
            return False
        if cid == MSG_CONTAINER_ID:
            return MsgContainer.read(b)
        if cid == RPC_RESULT_ID:
            return RpcResult.read(b)

        return _class_for(cid).read(b, *args)

    def write(self, *args: Any) -> bytes:
        raise NotImplementedError

    def __bytes__(self) -> bytes:
        return self.write()

    # -- introspection ------------------------------------------------------ #

    def _fields(self):
        for cls in type(self).__mro__:
            for name in getattr(cls, "__slots__", ()):
                yield name

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"_": self.QUALNAME}
        for name in self._fields():
            value = getattr(self, name, None)
            if value is None or value is False:
                continue
            out[name] = _to_plain(value)
        return out

    def __repr__(self) -> str:
        args = ", ".join(
            f"{name}={getattr(self, name, None)!r}"
            for name in self._fields()
            if getattr(self, name, None) not in (None, False)
        )
        return f"wirelet.raw.{self.QUALNAME}({args})"

    def __str__(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return NotImplemented
        # An unset ``flags.N?true`` field is None when built and False when read.
        def norm(v: Any) -> Any:
            return None if v is False else v

        return all(
            norm(getattr(self, n, None)) == norm(getattr(other, n, None))
            for n in self._fields()
        )

    def __hash__(self) -> int:
        return id(self)


def _to_plain(value: Any) -> Any:
    if isinstance(value, TLObject):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, bytes):
        return "<%d bytes>" % len(value)
    return value


def read_result(
    b: BytesIO,
    query: Optional[TLObject] = None,
    vector_item: Optional[Callable[[BytesIO], Any]] = None,
    primitive: Optional[Callable[[BytesIO], Any]] = None,
) -> Any:
    """Read the body of an ``rpc_result`` for a function whose result type is known.

    Telegram does not tag ``Vector<long>`` elements, so the function that was
    sent has to say how to read its answer; generated functions pass that here.
    """
    start = b.tell()
    cid = Int.read(b, False)

    if cid == GZIP_PACKED_ID:
        inner = BytesIO(gzip.decompress(Bytes.read(b)))
        return read_result(inner, query, vector_item, primitive)

    if cid == RPC_ERROR_ID:
        b.seek(start)
        return TLObject.read(b)

    if query is not None:
        b.seek(start)
        return query.read_result(b)

    if vector_item is not None:
        if cid != VECTOR_ID:
            raise TLDecodeError(f"Expected a Vector result, got 0x{cid:08x}")
        return read_bare_vector(b, vector_item)

    b.seek(start)
    if primitive is not None:
        return primitive(b)

    return TLObject.read(b)


# --------------------------------------------------------------------------- #
# MTProto service objects parsed by hand
# --------------------------------------------------------------------------- #

class Message:
    """One message of the encrypted MTProto layer (not a chat message)."""

    __slots__ = ("msg_id", "seq_no", "length", "body")

    def __init__(self, body: Any, msg_id: int, seq_no: int, length: int) -> None:
        self.body = body
        self.msg_id = msg_id
        self.seq_no = seq_no
        self.length = length

    def __repr__(self) -> str:
        return f"Message(msg_id={self.msg_id}, seq_no={self.seq_no}, body={self.body!r})"


class RawMessage:
    """A message whose body is still bytes, so an rpc_result can be typed later."""

    __slots__ = ("msg_id", "seq_no", "data")

    def __init__(self, msg_id: int, seq_no: int, data: bytes) -> None:
        self.msg_id = msg_id
        self.seq_no = seq_no
        self.data = data


class MsgContainer:
    ID = MSG_CONTAINER_ID

    __slots__ = ("messages",)

    def __init__(self, messages: List[Any]) -> None:
        self.messages = messages

    @staticmethod
    def read(b: BytesIO, *args: Any) -> "MsgContainer":
        count = Int.read(b)
        messages = []
        for _ in range(count):
            msg_id = Long.read(b)
            seq_no = Int.read(b)
            length = Int.read(b)
            messages.append(RawMessage(msg_id, seq_no, _read_exact(b, length)))
        return MsgContainer(messages)

    def write(self, *args: Any) -> bytes:
        out = [Int(self.ID, False), Int(len(self.messages))]
        for m in self.messages:
            body = m.body if isinstance(m.body, bytes) else m.body.write()
            out += [Long(m.msg_id), Int(m.seq_no), Int(len(body)), body]
        return b"".join(out)


class RpcResult:
    ID = RPC_RESULT_ID

    __slots__ = ("req_msg_id", "data")

    def __init__(self, req_msg_id: int, data: bytes) -> None:
        self.req_msg_id = req_msg_id
        self.data = data

    @staticmethod
    def read(b: BytesIO, *args: Any) -> "RpcResult":
        return RpcResult(Long.read(b), b.read())
