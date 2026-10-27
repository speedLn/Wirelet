"""Routes parsed updates to the handlers registered on a client.

Handlers live in numbered groups (default 0). Within a group the first handler
whose filters match runs and the group is done; then the next group gets the
same update. Raise :class:`StopPropagation` in a handler to skip the remaining
groups, or :class:`ContinuePropagation` to let the next handler in the same
group try too.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:
    from wirelet import Client
    from wirelet.filters import Filter

log = logging.getLogger(__name__)


class StopPropagation(Exception):
    """Raise in a handler to stop later groups from seeing this update."""


class ContinuePropagation(Exception):
    """Raise in a handler to let the next handler of the same group run too."""


# Kinds of handler.
MESSAGE = "message"
EDITED_MESSAGE = "edited_message"
DELETED_MESSAGES = "deleted_messages"
CALLBACK_QUERY = "callback_query"
INLINE_QUERY = "inline_query"
RAW_UPDATE = "raw_update"


class Handler:
    def __init__(self, kind: str, callback: Callable, filters: Optional["Filter"] = None) -> None:
        self.kind = kind
        self.callback = callback
        self.filters = filters

    async def check(self, client: "Client", update: Any) -> bool:
        if self.filters is None:
            return True
        result = self.filters(client, update)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def __repr__(self) -> str:
        return f"<Handler {self.kind} {getattr(self.callback, '__name__', self.callback)}>"


class Dispatcher:
    def __init__(self, client: "Client", workers: int) -> None:
        self.client = client
        self.workers = max(1, workers)
        self.groups: Dict[int, List[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._tasks: List[asyncio.Task] = []

    # -- registration --------------------------------------------------- #

    def add(self, handler: Handler, group: int = 0) -> Handler:
        self.groups[group].append(handler)
        return handler

    def remove(self, handler: Handler, group: int = 0) -> None:
        self.groups[group].remove(handler)

    def has(self, kind: str) -> bool:
        return any(h.kind == kind for hs in self.groups.values() for h in hs)

    # -- running ------------------------------------------------------- #

    async def start(self) -> None:
        for i in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(), name=f"wirelet-handler-{i}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    def feed(self, kind: str, update: Any, *raw_args: Any) -> None:
        try:
            self._queue.put_nowait((kind, update, raw_args))
        except asyncio.QueueFull:
            log.warning("Dropping a %s update: handlers are not keeping up", kind)

    async def _worker(self) -> None:
        while True:
            kind, update, raw_args = await self._queue.get()
            try:
                await self._dispatch(kind, update, raw_args)
            except Exception:  # noqa: BLE001
                log.exception("Unhandled error while dispatching an update")

    async def _dispatch(self, kind: str, update: Any, raw_args: tuple) -> None:
        for group in sorted(self.groups):
            try:
                for handler in list(self.groups[group]):
                    if handler.kind != kind:
                        continue
                    try:
                        if not await handler.check(self.client, update):
                            continue
                    except Exception:  # noqa: BLE001
                        log.exception("Filter of %r raised", handler)
                        continue

                    try:
                        args = (update, *raw_args) if kind == RAW_UPDATE else (update,)
                        result = handler.callback(self.client, *args)
                        if inspect.isawaitable(result):
                            await result
                    except ContinuePropagation:
                        continue
                    except StopPropagation:
                        raise
                    except Exception:  # noqa: BLE001
                        log.exception("Handler %r raised", handler)
                    break
            except StopPropagation:
                return
