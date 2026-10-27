"""Telegram data center addresses.

The built-in list is what clients use to make first contact; once connected,
:meth:`DataCenters.update` replaces it with the server's own ``help.getConfig``.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from wirelet import schema

# (dc_id) -> [(ip, port)], from the official desktop client.
PRODUCTION = {
    1: [("149.154.175.50", 443)],
    2: [("149.154.167.51", 443), ("95.161.76.100", 443)],
    3: [("149.154.175.100", 443)],
    4: [("149.154.167.91", 443)],
    5: [("149.154.171.5", 443)],
}
PRODUCTION_V6 = {
    1: [("2001:b28:f23d:f001::a", 443)],
    2: [("2001:67c:4e8:f002::a", 443)],
    3: [("2001:b28:f23d:f003::a", 443)],
    4: [("2001:67c:4e8:f004::a", 443)],
    5: [("2001:b28:f23f:f005::a", 443)],
}
TEST = {
    1: [("149.154.175.10", 443)],
    2: [("149.154.167.40", 443)],
    3: [("149.154.175.117", 443)],
}
TEST_V6 = {
    1: [("2001:b28:f23d:f001::e", 443)],
    2: [("2001:67c:4e8:f002::e", 443)],
    3: [("2001:b28:f23d:f003::e", 443)],
}

TEST_DC_SHIFT = 10000


class DataCenters:
    def __init__(self, test_mode: bool = False, ipv6: bool = False) -> None:
        self.test_mode = test_mode
        self.ipv6 = ipv6
        builtin = (TEST_V6 if ipv6 else TEST) if test_mode else (PRODUCTION_V6 if ipv6 else PRODUCTION)
        self._main: Dict[int, List[Tuple[str, int]]] = {k: list(v) for k, v in builtin.items()}
        self._media: Dict[int, List[Tuple[str, int]]] = {}
        self._cdn: Dict[int, List[Tuple[str, int]]] = {}

    def address(self, dc_id: int, media: bool = False) -> Tuple[str, int]:
        if media and self._media.get(dc_id):
            return self._media[dc_id][0]
        if dc_id in self._main and self._main[dc_id]:
            return self._main[dc_id][0]
        if dc_id in self._cdn:
            return self._cdn[dc_id][0]
        raise ValueError(f"Unknown DC {dc_id}")

    def protocol_id(self, dc_id: int, media: bool = False) -> int:
        """The dc number sent in ``p_q_inner_data_dc``."""
        value = dc_id + (TEST_DC_SHIFT if self.test_mode else 0)
        return -value if media else value

    def update(self, config: "schema.objects.Config") -> None:
        main: Dict[int, List[Tuple[str, int]]] = {}
        media: Dict[int, List[Tuple[str, int]]] = {}
        cdn: Dict[int, List[Tuple[str, int]]] = {}

        for opt in config.dc_options:
            if bool(opt.ipv6) != self.ipv6 or opt.tcpo_only or opt.secret:
                continue
            target = cdn if opt.cdn else media if opt.media_only else main
            target.setdefault(opt.id, []).append((opt.ip_address, opt.port))

        if main:
            self._main = main
        self._media = media
        self._cdn = cdn
