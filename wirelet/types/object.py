from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from wirelet import Client


class Object:
    """Base for the friendly types. ``raw`` keeps the API object it came from."""

    _client: "Client"

    def _bind(self, client: "Client") -> "Object":
        self._client = client
        return self

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"_": type(self).__name__}
        for key, value in vars(self).items():
            if key.startswith("_") or key == "raw" or value is None or value is False:
                continue
            out[key] = _plain(value)
        return out

    def __str__(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{k}={v!r}" for k, v in vars(self).items()
            if not k.startswith("_") and k != "raw" and v not in (None, False)
        )
        return f"{type(self).__name__}({fields})"


def _plain(value: Any) -> Any:
    if isinstance(value, Object):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value
