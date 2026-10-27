from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from wirelet.wire import Wire


class Model:
    """Base of the friendly objects. ``raw`` holds the schema object behind it,
    ``wire`` the :class:`~wirelet.Wire` it came from."""

    wire: "Wire"

    def _attach(self, wire: "Wire") -> "Model":
        self.wire = wire
        return self

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"@": type(self).__name__}
        for key, value in vars(self).items():
            if key.startswith("_") or key in ("raw", "wire") or value is None or value is False:
                continue
            out[key] = _plain(value)
        return out

    def __str__(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False, default=str)

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{k}={v!r}" for k, v in vars(self).items()
            if not k.startswith("_") and k not in ("raw", "wire") and v not in (None, False, [])
        )
        return f"<{type(self).__name__} {shown}>"


def _plain(value: Any) -> Any:
    if isinstance(value, Model):
        return value.as_dict()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value
