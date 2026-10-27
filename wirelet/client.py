"""The :class:`Client`: one Telegram account (user or bot)."""

from __future__ import annotations

import asyncio
import logging
import platform
import signal
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from wirelet import __version__, errors, raw, utils
from wirelet.dispatcher import Dispatcher
from wirelet.methods import Methods
from wirelet.network.dc import DataCenters
from wirelet.network.handshake import create_auth_key
from wirelet.network.session import Session
from wirelet.network.transport import TRANSPORTS
from wirelet.raw.core import TLObject
from wirelet.storage import FileStorage, MemoryStorage, Storage
from wirelet.updates import UpdatesMixin

log = logging.getLogger("wirelet")

DEFAULT_DC = 2


class Client(Methods, UpdatesMixin):
    """A Telegram client speaking MTProto directly.

    Parameters:
        name: Session name. With file storage this is ``<name>.session`` in
            ``workdir``.
        api_id, api_hash: From https://my.telegram.org. Needed to log in;
            optional afterwards because they are saved in the session.
        bot_token: Log in as a bot.
        phone_number, password: Log in as a user without being asked for them
            (the login code is still asked for, or pass ``phone_code``).
        session_string: Log in from :meth:`export_session_string` output.
        in_memory: Keep the session in memory only.
        parse_mode: Default formatting for text: ``"html"``, ``"markdown"`` or
            None for plain text.
        workers: How many updates are handled concurrently.
        sleep_threshold: Flood waits up to this many seconds are slept through
            automatically; longer ones raise :class:`errors.FloodWait`.
        skip_updates: Ignore updates that arrived while the client was offline.
        no_updates: Don't receive updates at all (scripts that only send).
        proxy: ``{"scheme": "socks5", "hostname": ..., "port": ...,
            "username": ..., "password": ...}``. Needs ``wirelet[proxy]``.
        transport: ``"abridged"`` (default), ``"intermediate"`` or ``"padded"``.
        test_mode: Use Telegram's test servers.
    """

    def __init__(
        self,
        name: str,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        *,
        bot_token: Optional[str] = None,
        phone_number: Optional[str] = None,
        phone_code: Optional[str] = None,
        password: Optional[str] = None,
        session_string: Optional[str] = None,
        in_memory: bool = False,
        workdir: Union[str, Path] = ".",
        parse_mode: Optional[str] = "html",
        workers: int = 8,
        sleep_threshold: int = 10,
        skip_updates: bool = True,
        no_updates: bool = False,
        proxy: Optional[Dict[str, Any]] = None,
        transport: str = "abridged",
        ipv6: bool = False,
        test_mode: bool = False,
        device_model: Optional[str] = None,
        system_version: Optional[str] = None,
        app_version: str = f"wirelet {__version__}",
        lang_code: str = "en",
        system_lang_code: str = "en",
    ) -> None:
        self.name = name
        self.api_id = int(api_id) if api_id else None
        self.api_hash = api_hash
        self.bot_token = bot_token
        self.phone_number = phone_number
        self.phone_code = phone_code
        self.password = password
        self.session_string = session_string
        self.parse_mode = parse_mode
        self.sleep_threshold = sleep_threshold
        self.skip_updates = skip_updates
        self.no_updates = no_updates
        self.proxy = proxy
        self.ipv6 = ipv6
        self.test_mode = test_mode
        self.device_model = device_model or f"{platform.python_implementation()} {platform.python_version()}"
        self.system_version = system_version or f"{platform.system()} {platform.release()}"
        self.app_version = app_version
        self.lang_code = lang_code
        self.system_lang_code = system_lang_code

        if transport not in TRANSPORTS:
            raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}")
        self._transport_cls = TRANSPORTS[transport]

        if in_memory or session_string:
            self.storage: Storage = MemoryStorage()
        else:
            self.storage = FileStorage(Path(workdir) / f"{name}.session")

        self.dcs = DataCenters(test_mode, ipv6)
        self.session: Optional[Session] = None
        self._dc_sessions: Dict[Tuple[int, bool], Session] = {}
        self._dc_lock = asyncio.Lock()
        self.dispatcher = Dispatcher(self, workers)

        self.me: Optional[raw.types.User] = None
        self.is_connected = False
        self.is_initialized = False

        UpdatesMixin.__init__(self)

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #

    @property
    def me_id(self) -> Optional[int]:
        return self.me.id if self.me is not None else self.storage.user_id

    @property
    def me_username(self) -> Optional[str]:
        return utils._username(self.me) if self.me is not None else None

    @property
    def is_bot(self) -> bool:
        return bool(self.storage.is_bot)

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def _transport(self):
        return self._transport_cls(proxy=self.proxy)

    def _init_query(self, query: TLObject) -> TLObject:
        return raw.functions.InvokeWithLayer(
            layer=raw.LAYER,
            query=raw.functions.InitConnection(
                api_id=self.storage.api_id or self.api_id or 0,
                device_model=self.device_model,
                system_version=self.system_version,
                app_version=self.app_version,
                system_lang_code=self.system_lang_code,
                lang_pack="",
                lang_code=self.lang_code,
                query=query,
            ),
        )

    async def _make_auth_key(self, dc_id: int, media: bool = False):
        transport = self._transport()
        host, port = self.dcs.address(dc_id, media)
        await transport.connect(host, port)
        try:
            return await create_auth_key(transport, self.dcs.protocol_id(dc_id, media), self.test_mode)
        finally:
            await transport.close()

    async def _open_session(self, dc_id: int, auth_key: Optional[bytes] = None,
                            media: bool = False, main: bool = False) -> Session:
        salt, offset = 0, 0
        if auth_key is None:
            log.info("Creating an auth key with DC%s%s", dc_id, " (media)" if media else "")
            auth_key, salt, offset = await self._make_auth_key(dc_id, media)

        session = Session(
            dc_id=dc_id,
            auth_key=auth_key,
            server_salt=salt,
            time_offset=offset,
            dcs=self.dcs,
            transport_factory=self._transport,
            init_query=self._init_query,
            media=media,
            on_update=self._on_raw_updates if main else None,
            on_new_session=self._on_new_session if main else None,
        )
        await session.start()
        return session

    async def connect(self) -> bool:
        """Connect to Telegram. Returns True if the session is already logged in."""
        if self.is_connected:
            raise ConnectionError("Client is already connected")

        await self.storage.open()
        if self.session_string:
            self.storage.import_session_string(self.session_string)
            self.test_mode = self.storage.test_mode
            self.dcs = DataCenters(self.test_mode, self.ipv6)

        if self.storage.auth_key is None:
            self.storage.dc_id = DEFAULT_DC
            self.storage.test_mode = self.test_mode
            self.storage.auth_key, salt, offset = await self._make_auth_key(DEFAULT_DC)
            await self.storage.save()

        if self.api_id and not self.storage.api_id:
            self.storage.api_id = self.api_id
            await self.storage.save()

        self.session = await self._open_session(self.storage.dc_id, self.storage.auth_key, main=True)
        self.is_connected = True
        return self.storage.user_id is not None

    async def disconnect(self) -> None:
        if not self.is_connected:
            return
        for s in list(self._dc_sessions.values()):
            await s.stop()
        self._dc_sessions.clear()
        if self.session is not None:
            await self.session.stop()
            self.session = None
        await self.storage.save()
        await self.storage.close()
        self.is_connected = False

    async def _switch_dc(self, dc_id: int) -> None:
        log.info("Switching account DC to DC%s", dc_id)
        if self.session is not None:
            await self.session.stop()
        auth_key, salt, offset = await self._make_auth_key(dc_id)
        self.storage.dc_id = dc_id
        self.storage.auth_key = auth_key
        await self.storage.save()
        self.session = await self._open_session(dc_id, auth_key, main=True)

    async def _session_for(self, dc_id: int, media: bool = True) -> Session:
        """A session for file transfers with ``dc_id``, logged in as this account."""
        key = (dc_id, media)
        async with self._dc_lock:
            existing = self._dc_sessions.get(key)
            if existing is not None:
                return existing

            same_dc = dc_id == self.storage.dc_id
            session = await self._open_session(dc_id, self.storage.auth_key if same_dc else None, media)

            if not same_dc and self.storage.user_id is not None:
                exported = await self.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                await session.invoke(raw.functions.auth.ImportAuthorization(
                    id=exported.id, bytes=exported.bytes))

            self._dc_sessions[key] = session
            return session

    # ------------------------------------------------------------------ #
    # Invoking
    # ------------------------------------------------------------------ #

    async def invoke(self, query: TLObject, *, retries: int = 5, timeout: float = 30.0,
                     sleep_threshold: Optional[int] = None) -> Any:
        """Call any raw API function and return its result.

        Moves the account to another DC when Telegram asks, sleeps through
        short flood waits and retries temporary server errors.
        """
        if self.session is None:
            raise ConnectionError("Client is not connected; call start() or connect() first")

        threshold = self.sleep_threshold if sleep_threshold is None else sleep_threshold
        attempt = 0

        while True:
            try:
                result = await self.session.invoke(query, timeout)
            except (errors.PhoneMigrate, errors.NetworkMigrate, errors.UserMigrate) as e:
                await self._switch_dc(e.value)
                continue
            except (errors.FloodWait, errors.FloodPremiumWait, errors.SlowmodeWait) as e:
                if e.value is None or e.value > threshold:
                    raise
                log.warning("Flood wait of %ss on %s; sleeping", e.value, e.request)
                await asyncio.sleep(e.value + 1)
                continue
            except (errors.InternalServerError, errors.ServiceUnavailable,
                    asyncio.TimeoutError, ConnectionError) as e:
                attempt += 1
                if attempt > retries:
                    raise
                delay = min(2 ** attempt, 20)
                log.warning("%s; retrying in %ss (%d/%d)", e or type(e).__name__, delay, attempt, retries)
                await asyncio.sleep(delay)
                continue

            await self._remember_peers(result)
            return result

    async def _remember_peers(self, result: Any) -> None:
        users = getattr(result, "users", None)
        chats = getattr(result, "chats", None)
        if isinstance(result, list):
            users = [u for u in result if isinstance(u, raw.types.User)]
        if isinstance(result, raw.types.User):
            users = [result]
        if users or chats:
            await self.storage.update_peers(utils.peer_rows(users or [], chats or []))

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> "Client":
        """Connect, log in if needed (asking in the terminal) and start updates."""
        authorized = await self.connect()
        try:
            if not authorized:
                await self._authorize()
            self.me = await self._fetch_me()
            self.storage.user_id = self.me.id
            self.storage.is_bot = bool(self.me.bot)
            await self.storage.save()

            if not self.no_updates:
                await self.dispatcher.start()
                await self._start_updates()
        except BaseException:
            await self.disconnect()
            raise

        self.is_initialized = True
        log.info("Started as %s (id %s)", utils._username(self.me) or self.me.first_name, self.me.id)
        return self

    async def stop(self) -> "Client":
        if not self.no_updates:
            await self._stop_updates()
            await self.dispatcher.stop()
        await self.disconnect()
        self.is_initialized = False
        return self

    async def restart(self) -> "Client":
        await self.stop()
        return await self.start()

    async def _fetch_me(self) -> raw.types.User:
        users = await self.invoke(raw.functions.users.GetUsers(id=[raw.types.InputUserSelf()]))
        return users[0]

    async def __aenter__(self) -> "Client":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    def run(self, main: Optional[Callable[..., Any]] = None) -> None:
        """Start the client and block until Ctrl+C (or until ``main`` returns).

        ``main`` may be a coroutine function taking no arguments.
        """
        async def runner() -> None:
            await self.start()
            try:
                if main is not None:
                    await main()
                else:
                    await idle()
            finally:
                await self.stop()

        try:
            asyncio.run(runner())
        except KeyboardInterrupt:
            pass


async def idle() -> None:
    """Block until Ctrl+C or SIGTERM. Use it to keep several clients running."""
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
