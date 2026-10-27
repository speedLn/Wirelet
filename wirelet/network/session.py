"""An encrypted MTProto session with one data center.

A session owns one TCP connection. It encrypts requests, matches answers to
the requests that caused them, acknowledges the server's messages, keeps the
salt and clock in step, pings to keep the link alive and, when the link drops,
reconnects and resends whatever was still waiting for an answer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from io import BytesIO
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional

from wirelet import faults, schema
from wirelet.crypto import mtproto
from wirelet.schema.core import (
    GZIP_CID, CONTAINER_CID, RPC_ANSWER_CID, Blob, Container,
    RpcAnswer, Struct,
)
from .transport import Transport, TransportError

if TYPE_CHECKING:
    from .dc import DataCenters

log = logging.getLogger(__name__)

UpdateCallback = Callable[[Struct], Awaitable[None]]


class _Request:
    __slots__ = ("query", "future", "msg_id", "sent_at")

    def __init__(self, query: Struct, future: asyncio.Future) -> None:
        self.query = query
        self.future = future
        self.msg_id = 0
        self.sent_at = 0.0


class Session:
    PING_INTERVAL = 30
    PING_DISCONNECT_DELAY = 75
    ACK_FLUSH_SIZE = 16
    RECONNECT_DELAYS = (0.5, 1, 2, 4, 8, 15, 30)

    def __init__(
        self,
        *,
        dc_id: int,
        auth_key: bytes,
        server_salt: int,
        time_offset: int,
        dcs: "DataCenters",
        transport_factory: Callable[[], Transport],
        init_query: Callable[[Struct], Struct],
        media: bool = False,
        on_update: Optional[UpdateCallback] = None,
        on_new_session: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.dc_id = dc_id
        self.auth_key = auth_key
        self.key_id = mtproto.auth_key_id(auth_key)
        self.salt = server_salt
        self.time_offset = time_offset
        self.dcs = dcs
        self.media = media
        self._transport_factory = transport_factory
        self._init_query = init_query
        self.on_update = on_update
        self.on_new_session = on_new_session

        self.session_id = os.urandom(8)
        self._seq = 0
        self._last_msg_id = 0

        self.transport: Optional[Transport] = None
        self._pending: Dict[int, _Request] = {}
        self._acks: List[int] = []

        self._recv_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._reconnect_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._closed = False
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._closed = False
        await self._connect()
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop(self) -> None:
        self._closed = True
        for task in (self._ping_task, self._recv_task):
            if task is not None:
                task.cancel()
        for task in (self._ping_task, self._recv_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._ping_task = self._recv_task = None

        if self.transport is not None:
            await self.transport.close()
        self._connected.clear()

        for request in self._pending.values():
            if not request.future.done():
                request.future.set_exception(ConnectionError("Session stopped"))
        self._pending.clear()

    async def _connect(self) -> None:
        host, port = self.dcs.address(self.dc_id, self.media)
        self.transport = self._transport_factory()
        await self.transport.connect(host, port)
        log.info("Connected to DC%s%s (%s:%s)", self.dc_id, " media" if self.media else "", host, port)

        self._recv_task = asyncio.create_task(self._recv_loop(self.transport))
        self._initialized = False

        # The first request on a connection must say who we are; nothing else
        # goes out until it has.
        await self._send_init()
        self._connected.set()

    async def _send_init(self) -> None:
        query = self._init_query(schema.methods.help.GetConfig())
        config = await self._invoke_once(query, timeout=15)
        self._initialized = True
        if isinstance(config, schema.objects.Config):
            self.dcs.update(config)

        # Requests that were cut off by a reconnect go out again now.
        for request in list(self._pending.values()):
            if request.future.done() or request.query is query:
                continue
            await self._resend(request)

    async def _reconnect(self, reason: str) -> None:
        if self._closed:
            return
        if self._reconnect_lock.locked():
            return

        async with self._reconnect_lock:
            self._connected.clear()
            log.warning("DC%s connection lost (%s); reconnecting", self.dc_id, reason)

            if self.transport is not None:
                await self.transport.close()

            for attempt, delay in enumerate(self._backoff()):
                if self._closed:
                    return
                try:
                    await self._connect()
                    log.info("DC%s reconnected", self.dc_id)
                    return
                except (OSError, asyncio.TimeoutError, faults.TelegramFault) as e:
                    log.warning("Reconnect attempt %d to DC%s failed: %s", attempt + 1, self.dc_id, e)
                    if self.transport is not None:
                        await self.transport.close()
                    await asyncio.sleep(delay)

    def _backoff(self):
        i = 0
        while True:
            yield self.RECONNECT_DELAYS[min(i, len(self.RECONNECT_DELAYS) - 1)]
            i += 1

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    def _next_msg_id(self) -> int:
        now = time.time() + self.time_offset
        msg_id = int(now * 2 ** 32) & ~3
        if msg_id <= self._last_msg_id:
            msg_id = self._last_msg_id + 4
        self._last_msg_id = msg_id
        return msg_id

    def _next_seq(self, content_related: bool) -> int:
        if content_related:
            seq = self._seq * 2 + 1
            self._seq += 1
            return seq
        return self._seq * 2

    async def _send_raw(self, body: bytes, content_related: bool) -> int:
        msg_id = self._next_msg_id()
        seq = self._next_seq(content_related)
        args = (body, msg_id, seq, self.salt, self.session_id, self.auth_key, self.key_id)

        # Encrypting a 512 KB file part in pure Python would stall the loop.
        if len(body) > 16 * 1024 and not _fast_crypto():
            packet = await asyncio.to_thread(mtproto.pack, *args)
        else:
            packet = mtproto.pack(*args)

        await self.transport.send(packet)
        return msg_id

    async def _resend(self, request: _Request) -> None:
        self._pending.pop(request.msg_id, None)
        try:
            request.msg_id = await self._send_raw(request.query.encode(), True)
        except (OSError, ConnectionError) as e:
            # Stays registered under a stale id; the next reconnect resends it.
            self._pending[request.msg_id] = request
            log.debug("Resend failed, will retry after reconnect: %s", e)
            return
        request.sent_at = time.monotonic()
        self._pending[request.msg_id] = request

    async def _invoke_once(self, query: Struct, timeout: float) -> Any:
        loop = asyncio.get_running_loop()
        request = _Request(query, loop.create_future())

        await self._flush_acks()
        request.msg_id = await self._send_raw(query.encode(), True)
        request.sent_at = time.monotonic()
        self._pending[request.msg_id] = request

        try:
            return await asyncio.wait_for(asyncio.shield(request.future), timeout)
        except asyncio.TimeoutError:
            raise
        finally:
            if request.future.done():
                self._pending.pop(request.msg_id, None)

    async def invoke(self, query: Struct, timeout: float = 30.0) -> Any:
        """Send ``query`` and return its result, surviving reconnects in between."""
        if self._closed:
            raise ConnectionError("Session is stopped")

        if not self._connected.is_set():
            await asyncio.wait_for(self._connected.wait(), timeout)

        loop = asyncio.get_running_loop()
        request = _Request(query, loop.create_future())

        try:
            await self._flush_acks()
            request.msg_id = await self._send_raw(query.encode(), True)
        except (OSError, ConnectionError) as e:
            # Register it anyway: the reconnect resends everything pending.
            request.msg_id = -id(request)
            self._pending[request.msg_id] = request
            asyncio.create_task(self._reconnect(str(e)))
        else:
            request.sent_at = time.monotonic()
            self._pending[request.msg_id] = request

        try:
            return await asyncio.wait_for(asyncio.shield(request.future), timeout)
        finally:
            for key, value in list(self._pending.items()):
                if value is request:
                    del self._pending[key]

    async def _flush_acks(self) -> None:
        if not self._acks or self.transport is None or not self.transport.connected:
            return
        acks, self._acks = self._acks, []
        try:
            await self._send_raw(schema.objects.MsgsAck(msg_ids=acks).encode(), False)
        except (OSError, ConnectionError):
            self._acks = acks + self._acks

    # ------------------------------------------------------------------ #
    # Receiving
    # ------------------------------------------------------------------ #

    async def _recv_loop(self, transport: Transport) -> None:
        while True:
            try:
                packet = await transport.recv()
            except asyncio.CancelledError:
                raise
            except TransportError as e:
                if e.code == 404:
                    log.error("DC%s does not know our auth key any more", self.dc_id)
                self._fail_reconnect(str(e), transport)
                return
            except Exception as e:  # noqa: BLE001
                self._fail_reconnect(str(e) or type(e).__name__, transport)
                return

            try:
                decrypted = mtproto.unpack(packet, self.session_id, self.auth_key, self.key_id)
            except faults.SecurityFault as e:
                log.warning("Dropping packet: %s", e)
                continue

            try:
                await self._process(decrypted.msg_id, decrypted.seq_no, decrypted.body)
            except Exception:  # noqa: BLE001 - one bad message must not kill the loop
                log.exception("Error while processing a message from DC%s", self.dc_id)

    def _fail_reconnect(self, reason: str, transport: Optional[Transport] = None) -> None:
        if self._closed:
            return
        if transport is not None and transport is not self.transport:
            return  # an old connection dying after we already replaced it
        asyncio.create_task(self._reconnect(reason))

    async def _process(self, msg_id: int, seq_no: int, body: bytes) -> None:
        if seq_no % 2:
            self._acks.append(msg_id)
            if len(self._acks) >= self.ACK_FLUSH_SIZE:
                asyncio.create_task(self._flush_acks())

        cid = int.from_bytes(body[:4], "little")

        if cid == CONTAINER_CID:
            container = Container.decode(BytesIO(body[4:]))
            for m in container.envelopes:
                await self._process(m.msg_id, m.seq_no, m.data)
            return

        if cid == GZIP_CID:
            import gzip
            await self._process(msg_id, 0, gzip.decompress(Blob.decode(BytesIO(body[4:]))))
            return

        if cid == RPC_ANSWER_CID:
            self._on_rpc_result(RpcAnswer.decode(BytesIO(body[4:])))
            return

        obj = Struct.decode(BytesIO(body))
        await self._on_service(msg_id, obj)

    def _on_rpc_result(self, result: RpcAnswer) -> None:
        request = self._pending.pop(result.req_msg_id, None)
        if request is None or request.future.done():
            return

        try:
            value = request.query.decode_result(BytesIO(result.data))
        except Exception as e:  # noqa: BLE001
            request.future.set_exception(e)
            return

        if isinstance(value, schema.objects.RpcError):
            request.future.set_exception(
                faults.from_telegram(value.error_code, value.error_message, _query_name(request.query))
            )
        else:
            request.future.set_result(value)

    async def _on_service(self, msg_id: int, obj: Any) -> None:
        if isinstance(obj, schema.objects.Pong):
            request = self._pending.pop(obj.msg_id, None)
            if request is not None and not request.future.done():
                request.future.set_result(obj)
            return

        if isinstance(obj, schema.objects.BadServerSalt):
            self.salt = obj.new_server_salt
            await self._resend_bad(obj.bad_msg_id)
            return

        if isinstance(obj, schema.objects.BadMsgNotification):
            code = obj.error_code
            if code in (16, 17):
                # Our clock is off: take the server's time from its msg_id.
                self.time_offset = (msg_id >> 32) - int(time.time())
                self._last_msg_id = 0
            elif code in (32, 33):
                # seq_no out of step: start a fresh session.
                self.session_id = os.urandom(8)
                self._seq = 0
            log.debug("bad_msg_notification %s for %s", code, obj.bad_msg_id)
            await self._resend_bad(obj.bad_msg_id)
            return

        if isinstance(obj, schema.objects.NewSessionCreated):
            self.salt = obj.server_salt
            if self.on_new_session is not None:
                asyncio.create_task(self.on_new_session())
            return

        if isinstance(obj, (schema.objects.MsgsAck, schema.objects.MsgDetailedInfo,
                            schema.objects.MsgNewDetailedInfo, schema.objects.FutureSalts,
                            schema.objects.MsgsStateInfo, schema.objects.MsgsAllInfo,
                            schema.objects.DestroySessionOk, schema.objects.DestroySessionNone)):
            if isinstance(obj, schema.objects.MsgDetailedInfo):
                self._acks.append(obj.answer_msg_id)
            return

        if isinstance(obj, schema.kinds.Updates):
            if self.on_update is not None:
                await self.on_update(obj)
            return

        log.debug("Unhandled service object: %r", obj)

    async def _resend_bad(self, bad_msg_id: int) -> None:
        request = self._pending.get(bad_msg_id)
        if request is not None and not request.future.done():
            await self._resend(request)

    # ------------------------------------------------------------------ #
    # Keepalive
    # ------------------------------------------------------------------ #

    async def _ping_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.PING_INTERVAL)
            if not self._connected.is_set():
                continue
            try:
                await self.invoke(
                    schema.methods.PingDelayDisconnect(
                        ping_id=int.from_bytes(os.urandom(8), "little", signed=True),
                        disconnect_delay=self.PING_DISCONNECT_DELAY,
                    ),
                    timeout=self.PING_INTERVAL,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("Ping to DC%s failed: %s", self.dc_id, e)
                self._fail_reconnect("ping timed out")


def _query_name(query: Struct) -> str:
    while hasattr(query, "query") and isinstance(getattr(query, "query"), Struct):
        query = query.query
    return getattr(query, "TL_NAME", type(query).__name__)


def _fast_crypto() -> bool:
    from wirelet.crypto.aes import is_accelerated
    return is_accelerated()
