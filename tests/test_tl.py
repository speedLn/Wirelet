"""Serialization of the generated API, and the TL primitives under it."""

import inspect
import re
from io import BytesIO

import pytest

from wirelet import schema
from wirelet.schema.core import Blob, Boolean, I32, I64, I128, Seq, Struct, Text, decode_result
from wirelet.schema.registry import NAMES


def all_classes():
    seen = []
    for qualname in NAMES.values():
        obj = schema
        for part in qualname.split("."):
            obj = getattr(obj, part)
        seen.append(obj)
    return seen


CLASSES = all_classes()


def constructors_of(base_qualname: str):
    return [c for c in CLASSES if c.NAME.startswith("objects.") and
            any(b.__dict__.get("NAME") == base_qualname for b in c.__mro__)]


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
    if hint == "Struct":
        return schema.objects.InputPeerSelf()
    match = re.match(r"schema\.(kinds\..+)", hint)
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
        data = obj.encode()
        assert data[:4] == I32(cls.CID, False), cls
        back = Struct.decode(BytesIO(data))
        assert type(back) is cls
        assert back == obj, cls.NAME
        assert back.encode() == data, cls.NAME


def test_layer_and_ids_are_unique():
    assert schema.LAYER >= 232
    assert len({c.CID for c in CLASSES}) == len(CLASSES)


def test_reserved_names_are_renamed():
    # "self" and "from" are TL field names; they become is_self and from_.
    user = schema.objects.User(id=1, is_self=True)
    assert Struct.decode(BytesIO(user.encode())).is_self
    renamed = [c for c in CLASSES if "from_" in inspect.signature(c.__init__).parameters]
    assert renamed, "expected at least one TL field named 'from'"


def test_bytes_padding_and_long_form():
    for n in (0, 1, 3, 253, 254, 1000, 70000):
        data = bytes(range(256)) * (n // 256) + bytes(range(n % 256))
        encoded = Blob(data)
        assert len(encoded) % 4 == 0
        assert Blob.decode(BytesIO(encoded)) == data
    assert Text.decode(BytesIO(Text("emoji 😀"))) == "emoji 😀"


def test_ints_bool_vector():
    assert I32.decode(BytesIO(I32(-5))) == -5
    assert I64.decode(BytesIO(I64(-(2 ** 63)))) == -(2 ** 63)
    assert I128.decode(BytesIO(I128(2 ** 120))) == 2 ** 120
    assert Boolean.decode(BytesIO(Boolean(True))) is True
    assert Boolean.decode(BytesIO(Boolean(False))) is False
    assert Seq.decode(BytesIO(Seq([1, 2, 3], I64)), I64.decode) == [1, 2, 3]


def test_vector_of_longs_result_needs_the_function():
    # contacts.getContactIDs returns Vector<int>: untagged ints only the
    # function knows how to read.
    fn = schema.methods.contacts.GetContactIDs(hash=0)
    assert fn.decode_result(BytesIO(Seq([10, 20], I32))) == [10, 20]


def test_generic_wrappers_read_the_inner_result():
    inner = schema.methods.contacts.GetContactIDs(hash=0)
    wrapped = schema.methods.InvokeWithLayer(layer=schema.LAYER, query=schema.methods.InitConnection(
        api_id=1, device_model="d", system_version="s", app_version="a", system_lang_code="en",
        lang_pack="", lang_code="en", query=inner))
    assert wrapped.decode_result(BytesIO(Seq([5], I32))) == [5]


def test_gzip_and_rpc_error_results():
    import gzip

    payload = schema.objects.Pong(msg_id=1, ping_id=2).encode()
    packed = I32(0x3072CFA1, False) + Blob(gzip.compress(payload))
    assert decode_result(BytesIO(packed)) == schema.objects.Pong(msg_id=1, ping_id=2)

    err = schema.objects.RpcError(error_code=400, error_message="X").encode()
    assert isinstance(schema.methods.help.GetConfig().decode_result(BytesIO(err)), schema.objects.RpcError)


def test_unknown_constructor_is_explained():
    from wirelet.schema.core import SchemaError

    with pytest.raises(SchemaError, match="newer layer"):
        Struct.decode(BytesIO(I32(0x12345678, False)))


def test_isinstance_against_base_types():
    assert isinstance(schema.objects.InputPeerSelf(), schema.kinds.InputPeer)
    assert isinstance(schema.objects.updates.State(pts=1, qts=0, date=0, seq=0, unread_count=0),
                      schema.kinds.updates.State)
