"""The :class:`Wire`: one Telegram account (a bot or a person) connected over MTProto."""

from __future__ import annotations

import asyncio
import logging
import platform
import signal
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union
from urllib.parse import unquote, urlparse

from wirelet import __version__, faults, schema, utils
from wirelet.actions import Actions
from wirelet.model import User
from wirelet.model.people import user_from
from wirelet.network.dc import DataCenters
from wirelet.network.handshake import create_auth_key
from wirelet.network.session import Session
from wirelet.network.transport import TRANSPORTS
from wirelet.router import Listeners, Router
from wirelet.schema.core import Struct
from wirelet.storage import FileStorage, MemoryStorage, Storage
from wirelet.updates import UpdateFeed

log = logging.getLogger("wirelet")

FIRST_DC = 2


def parse_proxy(proxy: Union[str, Dict[str, Any], None]) -> Optional[Dict[str, Any]]:
    """``"socks5://user:pass@host:1080"`` -> the dict the transport expects."""
    if proxy is None or isinstance(proxy, dict):
        return proxy
    url = urlparse(proxy)
    if url.scheme not in ("socks4", "socks5", "http") or not url.hostname or not url.port:
        raise ValueError("proxy must look like socks5://[user:pass@]host:port (socks4, socks5 or http)")
    return {
        "scheme": url.scheme, "hostname": url.hostname, "port": url.port,
        "username": unquote(url.username) if url.username else None,
        "password": unquote(url.password) if url.password else None,
    }


class Wire(Actions, UpdateFeed):
    """One Telegram account, connected directly over MTProto.

    Args:
        session: Name of the ``<session>.wire`` file that keeps the login.
        app_id, app_hash: Your app's credentials from https://my.telegram.org.
            Needed to log in, remembered afterwards.
        bot: A bot token from @BotFather; logs in as that bot.
        phone, code, password: Log in as a person without being asked
            (anything left out is asked for in the terminal).
        key: A login key from :meth:`export_key`; no file is used.
        memory: Keep the login in memory only.
        folder: Where the ``.wire`` file lives.
        format: How text is read when sending: ``"html"`` (default),
            ``"markdown"`` or ``None`` for plain text.
        concurrency: How many updates are handled at the same time.
        auto_wait: Flood waits up to this many seconds are waited out
            silently; longer ones raise ``faults.FLOOD_WAIT``.
        backlog: Also deliver updates that arrived while offline.
        listen: Receive updates at all. Turn off for send-only scripts.
        proxy: ``"socks5://user:pass@host:1080"`` (needs ``wirelet[proxy]``).
        framing: ``"abridged"`` (default), ``"intermediate"`` or ``"padded"``.
        test_servers: Use Telegram's test environment.
    """

    def __init__(
        self,
        session: str = "wirelet",
        *,
        app_id: Optional[int] = None,
        app_hash: Optional[str] = None,
        bot: Optional[str] = None,
        phone: Optional[str] = None,
        code: Optional[str] = None,
        password: Optional[str] = None,
        key: Optional[str] = None,
        memory: bool = False,
        folder: Union[str, Path] = ".",
        format: Optional[str] = "html",
        concurrency: int = 8,
        auto_wait: int = 10,
        backlog: bool = False,
        listen: bool = True,
        proxy: Union[str, Dict[str, Any], None] = None,
        framing: str = "abridged",
        ipv6: bool = False,
        test_servers: bool = False,
        device: Optional[str] = None,
        system: Optional[str] = None,
        version: str = f"wirelet {__version__}",
        language: str = "en",
    ) -> None:
        self.session_name = session
        self.app_id = int(app_id) if app_id else None
        self.app_hash = app_hash
        self.bot = bot
        self.phone = phone
        self.code = code
        self.password = password
        self.key = key
        self.format = format
        self.auto_wait = auto_wait
        self.backlog = backlog
        self.listen = listen
        self.proxy = parse_proxy(proxy)
        self.ipv6 = ipv6
        self.test_servers = test_servers
        self.device = device or f"{platform.python_implementation()} {platform.python_version()}"
        self.system = system or f"{platform.system()} {platform.release()}"
        self.version = version
        self.language = language

        if framing not in TRANSPORTS:
            raise ValueError(f"framing must be one of {', '.join(TRANSPORTS)}")
        self._transport_cls = TRANSPORTS[framing]

        if memory or key:
            self.storage: Storage = MemoryStorage()
        else:
            self.storage = FileStorage(Path(folder) / f"{session}.wire")

        self.dcs = DataCenters(test_servers, ipv6)
        self.session: Optional[Session] = None
        self._sessions: Dict[Tuple[int, bool], Session] = {}
        self._sessions_lock = asyncio.Lock()

        self._router = Router(self, concurrency)
        self.on = Listeners(self._router)
        """Decorators for listeners: ``on.message``, ``on.edit``, ``on.delete``,
        ``on.press``, ``on.inline``, ``on.raw``, ``on.error``."""

        self._me_raw: Optional[schema.objects.User] = None
        self.connected = False
        self.running = False
        UpdateFeed.__init__(self)

    def __repr__(self) -> str:
        who = f"@{self.me_handle}" if self.me_handle else self.me_id
        return f"<Wire {self.session_name!r} {who or 'not signed in'}>"

    # ------------------------------------------------------------------ #
    # Who we are
    # ------------------------------------------------------------------ #

    @property
    def me(self) -> Optional[User]:
        """This account, once started."""
        return user_from(self, self._me_raw) if self._me_raw is not None else None

    @property
    def me_id(self) -> Optional[int]:
        return self._me_raw.id if self._me_raw is not None else self.storage.user_id

    @property
    def me_handle(self) -> Optional[str]:
        return utils.handle_of(self._me_raw) if self._me_raw is not None else None

    @property
    def is_bot(self) -> bool:
        return bool(self.storage.is_bot)

    async def whoami(self) -> User:
        """Fetch this account fresh from Telegram."""
        self._me_raw = (await self.call(schema.methods.users.GetUsers(id=[schema.objects.InputUserSelf()])))[0]
        return self.me

    # ------------------------------------------------------------------ #
    # Listener shortcut
    # ------------------------------------------------------------------ #

    def command(self, name, *, prefix="/", case: bool = False, chat=None, by=None, has=None,
                mine=None, when=None, priority: int = 0, watch: bool = False):
        """Listen for a command: ``@wire.command("start")`` or ``@wire.command(["help", "h"], prefix="!")``.

        The listener gets the message with ``msg.cmd`` (the name), ``msg.args``
        (words after it; "quoted words" stay together) and ``msg.rest`` (the
        raw text after it). ``/start@OtherBot`` is ignored. Takes the same
        constraints as ``wire.on.message``.
        """
        return self.on.command_listener(name, prefix, case, priority, watch, dict(
            chat=chat, by=by, has=has, mine=mine, when=when))

    def remove(self, listener) -> None:
        """Stop a listener: pass the decorated function."""
        self._router.remove(listener)

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _transport(self):
        return self._transport_cls(proxy=self.proxy)

    def _wrap_first(self, method: Struct) -> Struct:
        return schema.methods.InvokeWithLayer(
            layer=schema.LAYER,
            query=schema.methods.InitConnection(
                api_id=self.storage.api_id or self.app_id or 0,
                device_model=self.device, system_version=self.system, app_version=self.version,
                system_lang_code=self.language, lang_pack="", lang_code=self.language, query=method,
            ),
        )

    async def _new_key(self, dc_id: int, media: bool = False):
        transport = self._transport()
        host, port = self.dcs.address(dc_id, media)
        await transport.connect(host, port)
        try:
            return await create_auth_key(transport, self.dcs.protocol_id(dc_id, media), self.test_servers)
        finally:
            await transport.close()

    async def _open(self, dc_id: int, auth_key: Optional[bytes] = None, media: bool = False,
                    main: bool = False) -> Session:
        salt, offset = 0, 0
        if auth_key is None:
            log.info("Agreeing on a key with DC%s%s", dc_id, " (media)" if media else "")
            auth_key, salt, offset = await self._new_key(dc_id, media)
        session = Session(
            dc_id=dc_id, auth_key=auth_key, server_salt=salt, time_offset=offset, dcs=self.dcs,
            transport_factory=self._transport, init_query=self._wrap_first, media=media,
            on_update=self._on_raw_updates if main else None,
            on_new_session=self._on_new_session if main else None,
        )
        await session.start()
        return session

    async def connect(self) -> bool:
        """Connect without logging in. Returns whether this login is already signed in."""
        if self.connected:
            raise ConnectionError("this wire is already connected")

        await self.storage.open()
        if self.key:
            self.storage.import_key(self.key)
            self.test_servers = self.storage.test_mode
            self.dcs = DataCenters(self.test_servers, self.ipv6)

        if self.storage.auth_key is None:
            self.storage.dc_id = FIRST_DC
            self.storage.test_mode = self.test_servers
            self.storage.auth_key, _, _ = await self._new_key(FIRST_DC)
            await self.storage.save()

        if self.app_id and not self.storage.api_id:
            self.storage.api_id = self.app_id
            await self.storage.save()

        self.session = await self._open(self.storage.dc_id, self.storage.auth_key, main=True)
        self.connected = True
        return self.storage.user_id is not None

    async def disconnect(self) -> None:
        if not self.connected:
            return
        for s in list(self._sessions.values()):
            await s.stop()
        self._sessions.clear()
        if self.session is not None:
            await self.session.stop()
            self.session = None
        await self.storage.save()
        await self.storage.close()
        self.connected = False

    async def _move_to(self, dc_id: int) -> None:
        log.info("Telegram moved this account to DC%s", dc_id)
        if self.session is not None:
            await self.session.stop()
        auth_key, _, _ = await self._new_key(dc_id)
        self.storage.dc_id, self.storage.auth_key = dc_id, auth_key
        await self.storage.save()
        self.session = await self._open(dc_id, auth_key, main=True)

    async def _session_for(self, dc_id: int, media: bool = True) -> Session:
        """A session with ``dc_id`` for file transfers, signed in as this account."""
        key = (dc_id, media)
        async with self._sessions_lock:
            found = self._sessions.get(key)
            if found is not None:
                return found
            home = dc_id == self.storage.dc_id
            session = await self._open(dc_id, self.storage.auth_key if home else None, media)
            if not home and self.storage.user_id is not None:
                exported = await self.call(schema.methods.auth.ExportAuthorization(dc_id=dc_id))
                await session.invoke(schema.methods.auth.ImportAuthorization(id=exported.id, bytes=exported.bytes))
            self._sessions[key] = session
            return session

    # ------------------------------------------------------------------ #
    # Calling the API
    # ------------------------------------------------------------------ #

    async def call(self, method: Struct, *, timeout: float = 30.0, retries: int = 5,
                   auto_wait: Optional[int] = None) -> Any:
        """Call any schema method, e.g. ``await wire.call(schema.methods.help.GetConfig())``.

        Follows the account to another data center when Telegram says so,
        waits out short flood limits and retries temporary server trouble.
        """
        if self.session is None:
            raise ConnectionError("not connected: use 'await wire.start()' or 'async with wire:'")

        limit = self.auto_wait if auto_wait is None else auto_wait
        attempt = 0
        while True:
            try:
                result = await self.session.invoke(method, timeout)
            except (faults.PHONE_MIGRATE, faults.NETWORK_MIGRATE, faults.USER_MIGRATE) as e:
                await self._move_to(e.amount)
                continue
            except (faults.FLOOD_WAIT, faults.FLOOD_PREMIUM_WAIT, faults.SLOWMODE_WAIT) as e:
                if e.amount is None or e.amount > limit:
                    raise
                log.warning("Waiting %ss for a flood limit on %s", e.amount, e.method)
                await asyncio.sleep(e.amount + 1)
                continue
            except (faults.ServerTrouble, asyncio.TimeoutError, ConnectionError) as e:
                attempt += 1
                if attempt > retries:
                    raise
                pause = min(2 ** attempt, 20)
                log.warning("%s; retrying in %ss (%d/%d)", e or type(e).__name__, pause, attempt, retries)
                await asyncio.sleep(pause)
                continue

            await self._remember_peers(result)
            if isinstance(result, schema.kinds.Updates):
                await self._absorb(result)
            return result

    async def _remember_peers(self, result: Any) -> None:
        users = getattr(result, "users", None)
        chats = getattr(result, "chats", None)
        if isinstance(result, list):
            users = [u for u in result if isinstance(u, schema.objects.User)]
        elif isinstance(result, schema.objects.User):
            users = [result]
        if users or chats:
            await self.storage.update_peers(utils.peer_rows(users or [], chats or []))

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> "Wire":
        """Connect, sign in (asking in the terminal if needed) and start listening."""
        signed_in = await self.connect()
        try:
            if not signed_in:
                await self._login()
            await self.whoami()
            self.storage.user_id, self.storage.is_bot = self._me_raw.id, bool(self._me_raw.bot)
            await self.storage.save()
            if self.listen:
                await self._router.start()
                await self._start_feed()
        except BaseException:
            await self.disconnect()
            raise
        self.running = True
        log.info("Wire up as %s (%s)", self.me_handle or self._me_raw.first_name, self._me_raw.id)
        return self

    async def stop(self) -> "Wire":
        if self.listen:
            await self._stop_feed()
            await self._router.stop()
        await self.disconnect()
        self.running = False
        return self

    async def __aenter__(self) -> "Wire":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    def serve(self, main: Optional[Callable[[], Awaitable[Any]]] = None) -> None:
        """Start, then block until Ctrl+C - or until ``main()`` returns, if given."""
        run(self, main=main)


async def forever() -> None:
    """Wait until Ctrl+C or SIGTERM."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows
            pass
    try:
        await stop.wait()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass


def run(*wires: Wire, main: Optional[Callable[[], Awaitable[Any]]] = None) -> None:
    """Start every wire and keep them running until Ctrl+C (or until ``main()`` returns)."""
    async def runner() -> None:
        started = []
        try:
            for w in wires:
                started.append(await w.start())
            await (main() if main is not None else forever())
        finally:
            for w in reversed(started):
                await w.stop()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
