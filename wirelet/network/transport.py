"""TCP transports: how MTProto packets are framed on the wire.

https://core.telegram.org/mtproto/mtproto-transports
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Type

log = logging.getLogger(__name__)

TRANSPORT_ERRORS = {
    404: "auth key not found",
    429: "too many connections from this IP (transport flood)",
    444: "invalid DC",
}


class TransportError(ConnectionError):
    """Telegram closed the transport with a 4-byte error code."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"Transport error {code}: {TRANSPORT_ERRORS.get(code, 'unknown')}")


class Transport:
    """Base class: owns one TCP stream and frames packets on it."""

    TAG = b""

    def __init__(self, proxy: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self, host: str, port: int) -> None:
        if self.proxy:
            sock = await _proxy_socket(self.proxy, host, port, self.timeout)
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(sock=sock), self.timeout
            )
        else:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.timeout
            )

        if self.TAG:
            self.writer.write(self.TAG)
            await self.writer.drain()

    async def close(self) -> None:
        writer, self.writer, self.reader = self.writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception:  # noqa: BLE001 - closing a dead socket can fail any way
            pass

    async def send(self, packet: bytes) -> None:
        if not self.connected:
            raise ConnectionError("Transport is not connected")
        async with self._write_lock:
            self.writer.write(self.frame(packet))
            await self.writer.drain()

    async def recv(self) -> bytes:
        if self.reader is None:
            raise ConnectionError("Transport is not connected")
        packet = await self.read_frame()
        if len(packet) == 4:
            raise TransportError(-int.from_bytes(packet, "little", signed=True))
        return packet

    async def _read(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError as e:
            raise ConnectionError("Connection closed by Telegram") from e

    # subclasses
    def frame(self, packet: bytes) -> bytes:
        raise NotImplementedError

    async def read_frame(self) -> bytes:
        raise NotImplementedError


class TCPAbridged(Transport):
    """1-byte length prefix for short packets; the lightest framing."""

    TAG = b"\xef"

    def frame(self, packet: bytes) -> bytes:
        words = len(packet) // 4
        if words < 127:
            return bytes([words]) + packet
        return b"\x7f" + words.to_bytes(3, "little") + packet

    async def read_frame(self) -> bytes:
        words = (await self._read(1))[0]
        if words >= 127:
            words = int.from_bytes(await self._read(3), "little")
        return await self._read(words * 4)


class TCPIntermediate(Transport):
    """4-byte length prefix."""

    TAG = b"\xee\xee\xee\xee"

    def frame(self, packet: bytes) -> bytes:
        return len(packet).to_bytes(4, "little") + packet

    async def read_frame(self) -> bytes:
        length = int.from_bytes(await self._read(4), "little")
        return await self._read(length)


class TCPPaddedIntermediate(TCPIntermediate):
    """Intermediate framing with 0-15 random bytes appended, to vary packet sizes."""

    TAG = b"\xdd\xdd\xdd\xdd"

    def frame(self, packet: bytes) -> bytes:
        padding = os.urandom(os.urandom(1)[0] % 16)
        return (len(packet) + len(padding)).to_bytes(4, "little") + packet + padding

    async def read_frame(self) -> bytes:
        data = await super().read_frame()
        if len(data) == 4:
            return data
        # Encrypted packets are 24 + 16k bytes; plain ones carry their length.
        if data[:8] == bytes(8):
            length = int.from_bytes(data[16:20], "little")
            return data[:20 + length]
        return data[:len(data) - (len(data) - 24) % 16]


TRANSPORTS: Dict[str, Type[Transport]] = {
    "abridged": TCPAbridged,
    "intermediate": TCPIntermediate,
    "padded": TCPPaddedIntermediate,
}


async def _proxy_socket(proxy: Dict[str, Any], host: str, port: int, timeout: float):
    try:
        from python_socks import ProxyType
        from python_socks.async_.asyncio import Proxy
    except ImportError as e:  # pragma: no cover
        raise ImportError("Proxy support needs: pip install wirelet[proxy]") from e

    scheme = str(proxy.get("scheme", "socks5")).lower()
    proxy_type = {
        "socks4": ProxyType.SOCKS4,
        "socks5": ProxyType.SOCKS5,
        "http": ProxyType.HTTP,
    }[scheme]

    p = Proxy.create(
        proxy_type=proxy_type,
        host=proxy["hostname"],
        port=int(proxy["port"]),
        username=proxy.get("username"),
        password=proxy.get("password"),
        rdns=True,
    )
    return await p.connect(dest_host=host, dest_port=port, timeout=timeout)
