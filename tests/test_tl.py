"""Serialization of the generated API, and the TL primitives under it."""

import inspect
import re
from io import BytesIO

import pytest

from wirelet import raw
from wirelet.raw.all import OBJECTS
from wirelet.raw.core import Bool, Bytes, Int, Int128, Long, String, TLObject, Vector, read_result


def all_classes():
    seen = []
    for qualname in OBJECTS.values():
        obj = raw
        for part in qualname.split("."):
            obj = getattr(obj, part)
        seen.append(obj)
    return seen


CLASSES = all_classes()


def constructors_of(base_qualname: str):
    return [c for c in CLASSES if c.QUALNAME.startswith("types.") and
            any(b.__dict__.get("QUALNAME") == base_qualname for b in c.__mro__)]


def sample(hint: str, depth: int, full: bool):
    hint = hint.strip().strip("'\"")
    if hint == "int":
        return 7
    if hint == "str":
        return "héllo ✓"
    if hint == "bytes":
        return b"\x00\x01\xfe"
    if hint == "float":
        return 2.5
    if hint == "bool":
        return True
    if hint.startswith("List["):
        inner = hint[5:-1]
        return [sample(inner, depth + 1, full)] if depth < 3 else []
    if hint == "TLObject":
        return raw.types.InputPeerSelf()
    match = re.match(r"raw\.(base\..+)", hint)
    if match:
        options = sorted(constructors_of(match.group(1)), key=lambda c: len(c.__slots__))
        return build(options[0], depth + 1, full and depth < 2)
    raise AssertionError(f"no sample for {hint}")


def build(cls, depth=0, full=False):
    kwargs = {}
    for name, param in inspect.signature(cls.__init__).parameters.items():
        if name == "self":
            continue
        hint = param.annotation
        optional = param.default is None
        if optional:
            if not full:
                continue
            hint = re.sub(r"^Optional\[(.*)\]$", r"\1", hint)
        kwargs[name] = sample(hint, depth, full)
    return cls(**kwargs)


@pytest.mark.parametrize("full", [False, True], ids=["required-only", "every-field"])
def test_every_object_round_trips(full):
    assert len(CLASSES) > 2500
    for cls in CLASSES:
        obj = build(cls, full=full)
        data = obj.write()
        assert data[:4] == Int(cls.ID, False), cls
        back = TLObject.read(BytesIO(data))
        assert type(back) is cls
        assert back == obj, cls.QUALNAME
        assert back.write() == data, cls.QUALNAME


def test_layer_and_ids_are_unique():
    assert raw.LAYER >= 232
    assert len({c.ID for c in CLASSES}) == len(CLASSES)


def test_reserved_names_are_renamed():
    # "self" and "from" are TL field names; they become is_self and from_.
    user = raw.types.User(id=1, is_self=True)
    assert TLObject.read(BytesIO(user.write())).is_self
    renamed = [c for c in CLASSES if "from_" in inspect.signature(c.__init__).parameters]
    assert renamed, "expected at least one TL field named 'from'"


def test_bytes_padding_and_long_form():
    for n in (0, 1, 3, 253, 254, 1000, 70000):
        data = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
        encoded = Bytes(data)
        assert len(encoded) % 4 == 0
        assert Bytes.read(BytesIO(encoded)) == data
    assert String.read(BytesIO(String("emoji 😀"))) == "emoji 😀"


def test_ints_bool_vector():
    assert Int.read(BytesIO(Int(-5))) == -5
    assert Long.read(BytesIO(Long(-(2 ** 63)))) == -(2 ** 63)
    assert Int128.read(BytesIO(Int128(2 ** 120))) == 2 ** 120
    assert Bool.read(BytesIO(Bool(True))) is True
    assert Bool.read(BytesIO(Bool(False))) is False
    assert Vector.read(BytesIO(Vector([1, 2, 3], Long)), Long.read) == [1, 2, 3]


def test_vector_of_longs_result_needs_the_function():
    # contacts.getContactIDs returns Vector<int>: untagged ints only the
    # function knows how to read.
    fn = raw.functions.contacts.GetContactIDs(hash=0)
    assert fn.read_result(BytesIO(Vector([10, 20], Int))) == [10, 20]


def test_generic_wrappers_read_the_inner_result():
    inner = raw.functions.contacts.GetContactIDs(hash=0)
    wrapped = raw.functions.InvokeWithLayer(layer=raw.LAYER, query=raw.functions.InitConnection(
        api_id=1, device_model="d", system_version="s", app_version="a", system_lang_code="en",
        lang_pack="", lang_code="en", query=inner))
    assert wrapped.read_result(BytesIO(Vector([5], Int))) == [5]


def test_gzip_and_rpc_error_results():
    import gzip

    payload = raw.types.Pong(msg_id=1, ping_id=2).write()
    packed = Int(0x3072CFA1, False) + Bytes(gzip.compress(payload))
    assert read_result(BytesIO(packed)) == raw.types.Pong(msg_id=1, ping_id=2)

    err = raw.types.RpcError(error_code=400, error_message="X").write()
    assert isinstance(raw.functions.help.GetConfig().read_result(BytesIO(err)), raw.types.RpcError)


def test_unknown_constructor_is_explained():
    from wirelet.raw.core import TLDecodeError

    with pytest.raises(TLDecodeError, match="newer layer"):
        TLObject.read(BytesIO(Int(0x12345678, False)))


def test_isinstance_against_base_types():
    assert isinstance(raw.types.InputPeerSelf(), raw.base.InputPeer)
    assert isinstance(raw.types.updates.State(pts=1, qts=0, date=0, seq=0, unread_count=0),
                      raw.base.updates.State)
