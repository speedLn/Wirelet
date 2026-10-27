"""Where a client keeps its auth key, update state and the peers it has seen.

* :class:`FileStorage` - an SQLite file next to your script (the default).
* :class:`MemoryStorage` - nothing touches the disk; pair it with a session
  string to log in without a file.

A session string packs the auth key and DC into one line you can keep in an
environment variable: ``Client("bot", session_string=os.environ["SESSION"])``.
"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PeerRow = Tuple[int, int, str, Optional[str], Optional[str]]  # id, hash, type, username, phone

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dc_id INTEGER NOT NULL,
    api_id INTEGER,
    test_mode INTEGER NOT NULL,
    auth_key BLOB,
    user_id INTEGER,
    is_bot INTEGER,
    date INTEGER
);
CREATE TABLE IF NOT EXISTS peers (
    id INTEGER PRIMARY KEY,
    access_hash INTEGER NOT NULL,
    type TEXT NOT NULL,
    username TEXT,
    phone TEXT,
    updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS peers_username ON peers (username);
CREATE INDEX IF NOT EXISTS peers_phone ON peers (phone);
CREATE TABLE IF NOT EXISTS update_state (
    id INTEGER PRIMARY KEY,
    pts INTEGER,
    qts INTEGER,
    date INTEGER,
    seq INTEGER
);
CREATE TABLE IF NOT EXISTS version (number INTEGER PRIMARY KEY);
"""

VERSION = 1

# >B dc_id, I api_id, ? test_mode, 256s auth_key, q user_id, ? is_bot
_STRING_FORMAT = ">BI?256sq?"


class Storage:
    """In-memory storage; also the base class with the full interface."""

    def __init__(self) -> None:
        self.dc_id: int = 2
        self.api_id: Optional[int] = None
        self.test_mode: bool = False
        self.auth_key: Optional[bytes] = None
        self.user_id: Optional[int] = None
        self.is_bot: Optional[bool] = None
        self._peers: Dict[int, PeerRow] = {}
        self._usernames: Dict[str, int] = {}
        self._phones: Dict[str, int] = {}
        # channel id 0 is the common update box
        self._state: Dict[int, Tuple[int, int, int, int]] = {}

    async def open(self) -> None:
        pass

    async def save(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def delete(self) -> None:
        self.auth_key = None
        self.user_id = None

    # -- peers -------------------------------------------------------------- #

    async def update_peers(self, peers: List[PeerRow]) -> None:
        for row in peers:
            peer_id, _, _, username, phone = row
            self._peers[peer_id] = row
            if username:
                self._usernames[username.lower()] = peer_id
            if phone:
                self._phones[phone] = peer_id

    async def get_peer_by_id(self, peer_id: int) -> Optional[PeerRow]:
        return self._peers.get(peer_id)

    async def get_peer_by_username(self, username: str) -> Optional[PeerRow]:
        peer_id = self._usernames.get(username.lower())
        return self._peers.get(peer_id) if peer_id is not None else None

    async def get_peer_by_phone(self, phone: str) -> Optional[PeerRow]:
        peer_id = self._phones.get(phone)
        return self._peers.get(peer_id) if peer_id is not None else None

    # -- update state -------------------------------------------------------- #

    async def get_state(self, box: int = 0) -> Optional[Tuple[int, int, int, int]]:
        return self._state.get(box)

    async def set_state(self, box: int, pts: int, qts: int = 0, date: int = 0, seq: int = 0) -> None:
        self._state[box] = (pts, qts, date, seq)

    # -- session strings ------------------------------------------------------ #

    def export_session_string(self) -> str:
        if self.auth_key is None:
            raise ValueError("Not logged in yet")
        packed = struct.pack(
            _STRING_FORMAT, self.dc_id, self.api_id or 0, self.test_mode,
            self.auth_key, self.user_id or 0, bool(self.is_bot),
        )
        return base64.urlsafe_b64encode(packed).decode().rstrip("=")

    def import_session_string(self, value: str) -> None:
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(data) != struct.calcsize(_STRING_FORMAT):
            raise ValueError("This is not a wirelet session string")
        dc_id, api_id, test_mode, auth_key, user_id, is_bot = struct.unpack(_STRING_FORMAT, data)
        self.dc_id, self.api_id, self.test_mode = dc_id, api_id or None, test_mode
        self.auth_key, self.user_id, self.is_bot = auth_key, user_id or None, is_bot


MemoryStorage = Storage


class FileStorage(Storage):
    """SQLite-backed storage. Writes go through a thread so the loop never blocks."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._db: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def open(self) -> None:
        def _open() -> None:
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.executescript(SCHEMA)
            if self._db.execute("SELECT number FROM version").fetchone() is None:
                self._db.execute("INSERT INTO version VALUES (?)", (VERSION,))
            row = self._db.execute(
                "SELECT dc_id, api_id, test_mode, auth_key, user_id, is_bot FROM session"
            ).fetchone()
            if row is not None:
                (self.dc_id, self.api_id, test_mode, self.auth_key, self.user_id, is_bot) = row
                self.test_mode = bool(test_mode)
                self.is_bot = None if is_bot is None else bool(is_bot)
            for box, pts, qts, date, seq in self._db.execute(
                "SELECT id, pts, qts, date, seq FROM update_state"
            ):
                self._state[box] = (pts, qts, date, seq)
            self._db.commit()

        await self._run(_open)

    async def save(self) -> None:
        def _save() -> None:
            self._db.execute(
                "INSERT OR REPLACE INTO session (id, dc_id, api_id, test_mode, auth_key, user_id, is_bot, date)"
                " VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (self.dc_id, self.api_id, int(self.test_mode), self.auth_key,
                 self.user_id, None if self.is_bot is None else int(self.is_bot), int(time.time())),
            )
            self._db.commit()

        if self._db is not None:
            await self._run(_save)

    async def close(self) -> None:
        if self._db is not None:
            db, self._db = self._db, None
            await self._run(db.close)

    async def delete(self) -> None:
        await super().delete()
        await self.close()
        self.path.unlink(missing_ok=True)

    async def update_peers(self, peers: List[PeerRow]) -> None:
        if not peers or self._db is None:
            return
        now = int(time.time())

        def _write() -> None:
            self._db.executemany(
                "INSERT OR REPLACE INTO peers (id, access_hash, type, username, phone, updated)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(i, h, t, u.lower() if u else None, p, now) for i, h, t, u, p in peers],
            )
            self._db.commit()

        await self._run(_write)

    async def _one(self, where: str, value) -> Optional[PeerRow]:
        if self._db is None:
            return None

        def _query():
            return self._db.execute(
                f"SELECT id, access_hash, type, username, phone FROM peers WHERE {where} = ?"
                " ORDER BY updated DESC LIMIT 1",
                (value,),
            ).fetchone()

        return await self._run(_query)

    async def get_peer_by_id(self, peer_id: int) -> Optional[PeerRow]:
        return await self._one("id", peer_id)

    async def get_peer_by_username(self, username: str) -> Optional[PeerRow]:
        return await self._one("username", username.lower())

    async def get_peer_by_phone(self, phone: str) -> Optional[PeerRow]:
        return await self._one("phone", phone)

    async def set_state(self, box: int, pts: int, qts: int = 0, date: int = 0, seq: int = 0) -> None:
        await super().set_state(box, pts, qts, date, seq)
        if self._db is None:
            return

        def _write() -> None:
            self._db.execute(
                "INSERT OR REPLACE INTO update_state (id, pts, qts, date, seq) VALUES (?, ?, ?, ?, ?)",
                (box, pts, qts, date, seq),
            )
            self._db.commit()

        await self._run(_write)
