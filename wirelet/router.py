"""Delivers updates to listeners.

How an update travels:

1. If someone is waiting for it (``msg.ask``, ``wire.wait_message``), it goes
   to them and nowhere else.
2. Every matching *watcher* (``watch=True``) sees it - for logging, stats...
3. Listeners are tried from the highest ``priority`` down (ties in the order
   they were added). The first one that matches handles it. If that listener
   returns :data:`NEXT`, the next matching one gets it too.

Errors in listeners go to ``@wire.on.error`` listeners if there are any, or
to the log.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .match import Command, Constraints

if TYPE_CHECKING:
    from wirelet.wire import Wire

log = logging.getLogger(__name__)

EVENTS = ("message", "edit", "delete", "press", "inline", "raw")


class _Next:
    def __repr__(self) -> str:
        return "wirelet.NEXT"


NEXT = _Next()
"""Return this from a listener to pass the update on to the next matching one."""


class Listener:
    __slots__ = ("event", "func", "checks", "priority", "watch", "order")

    def __init__(self, event: str, func: Callable, checks: List, priority: int, watch: bool, order: int) -> None:
        self.event = event
        self.func = func
        self.checks = checks
        self.priority = priority
        self.watch = watch
        self.order = order

    async def matches(self, wire: "Wire", update: Any) -> bool:
        for check in self.checks:
            if not await check(wire, update):
                return False
        return True

    def __repr__(self) -> str:
        return f"<Listener {self.event} {getattr(self.func, '__qualname__', self.func)}>"


class Router:
    def __init__(self, wire: "Wire", concurrency: int) -> None:
        self.wire = wire
        self.concurrency = max(1, concurrency)
        self.listeners: Dict[str, List[Listener]] = {e: [] for e in EVENTS}
        self.error_listeners: List[Callable] = []
        self._waiters: List[Tuple[Constraints, asyncio.Future]] = []
        self._by_func: Dict[Callable, List[Listener]] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._tasks: List[asyncio.Task] = []
        self._count = 0

    # -- registration -------------------------------------------------- #

    def add(self, event: str, func: Callable, checks: List, priority: int = 0, watch: bool = False) -> Listener:
        if event not in self.listeners:
            raise ValueError(f"unknown event {event!r}; use one of {', '.join(EVENTS)}")
        self._count += 1
        listener = Listener(event, func, checks, priority, watch, self._count)
        group = self.listeners[event]
        group.append(listener)
        group.sort(key=lambda x: (-x.priority, x.order))
        self._by_func.setdefault(func, []).append(listener)
        return listener

    def remove(self, target) -> None:
        """Remove a :class:`Listener`, or every listener a function was registered as."""
        found = [target] if isinstance(target, Listener) else self._by_func.pop(target, [])
        if not found:
            raise ValueError(f"{target!r} is not a registered listener")
        for listener in found:
            self.listeners[listener.event].remove(listener)

    def wants(self, event: str) -> bool:
        return bool(self.listeners[event]) or (event == "message" and bool(self._waiters))

    # -- waiting ------------------------------------------------------- #

    async def wait(self, constraints: Constraints, timeout: Optional[float]) -> Any:
        future = asyncio.get_running_loop().create_future()
        entry = (constraints, future)
        self._waiters.append(entry)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            if entry in self._waiters:
                self._waiters.remove(entry)

    # -- running ------------------------------------------------------- #

    async def start(self) -> None:
        for i in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._work(), name=f"wirelet-worker-{i}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        for _, future in self._waiters:
            if not future.done():
                future.cancel()
        self._waiters.clear()

    def feed(self, event: str, update: Any, *extra: Any) -> None:
        try:
            self._queue.put_nowait((event, update, extra))
        except asyncio.QueueFull:
            log.warning("Dropping a %s update: listeners are not keeping up", event)

    async def _work(self) -> None:
        while True:
            event, update, extra = await self._queue.get()
            try:
                await self._deliver(event, update, extra)
            except Exception:  # noqa: BLE001
                log.exception("Unexpected error while delivering an update")

    async def _deliver(self, event: str, update: Any, extra: tuple) -> None:
        wire = self.wire

        if event == "message" and self._waiters:
            for constraints, future in list(self._waiters):
                if future.done():
                    continue
                try:
                    hit = await constraints(wire, update)
                except Exception:  # noqa: BLE001
                    log.exception("A wait condition raised")
                    continue
                if hit:
                    future.set_result(update)
                    return

        handled = False
        for listener in self.listeners[event]:
            if handled and not listener.watch:
                continue
            try:
                if not await listener.matches(wire, update):
                    continue
            except Exception as e:  # noqa: BLE001
                await self._error(e, update, listener)
                continue

            try:
                result = listener.func(update, *extra)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as e:  # noqa: BLE001
                await self._error(e, update, listener)
                result = None

            if not listener.watch and result is not NEXT:
                handled = True

    async def _error(self, error: Exception, update: Any, listener: Listener) -> None:
        if not self.error_listeners:
            log.error("%r raised", listener, exc_info=(type(error), error, error.__traceback__))
            return
        for func in self.error_listeners:
            try:
                result = func(error, update)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("An error listener raised")


class Listeners:
    """``wire.on``: decorators that register listeners.

    Each takes the keyword constraints from :mod:`wirelet.match`, plus
    ``priority`` (higher runs first) and ``watch`` (see every match, never
    stop others).
    """

    def __init__(self, router: Router) -> None:
        self._router = router

    def _decorator(self, event: str, extra_checks: List, priority: int, watch: bool, constraints: dict):
        checks = list(extra_checks)
        if any(v is not None for v in constraints.values()):
            checks.append(Constraints(**constraints))

        def register(func: Callable) -> Callable:
            self._router.add(event, func, checks, priority, watch)
            return func

        return register

    def message(self, *, chat=None, by=None, text=None, pattern=None, has=None, mine=None,
                when=None, priority: int = 0, watch: bool = False):
        """New messages: ``async def listener(msg)``."""
        return self._decorator("message", [], priority, watch, dict(
            chat=chat, by=by, text=text, pattern=pattern, has=has, mine=mine, when=when))

    def edit(self, *, chat=None, by=None, text=None, pattern=None, has=None, mine=None,
             when=None, priority: int = 0, watch: bool = False):
        """Edited messages: ``async def listener(msg)``."""
        return self._decorator("edit", [], priority, watch, dict(
            chat=chat, by=by, text=text, pattern=pattern, has=has, mine=mine, when=when))

    def delete(self, *, when=None, priority: int = 0, watch: bool = False):
        """Deleted messages: ``async def listener(deleted)`` with ``deleted.ids``."""
        return self._decorator("delete", [], priority, watch, dict(when=when))

    def press(self, data=None, *, chat=None, by=None, pattern=None, when=None,
              priority: int = 0, watch: bool = False):
        """Button presses: ``async def listener(press)``. ``data`` is the exact button data."""
        return self._decorator("press", [], priority, watch, dict(
            data=data, chat=chat, by=by, pattern=pattern, when=when))

    def inline(self, *, by=None, text=None, pattern=None, when=None, priority: int = 0, watch: bool = False):
        """Inline queries: ``async def listener(query)``."""
        return self._decorator("inline", [], priority, watch, dict(
            by=by, text=text, pattern=pattern, when=when))

    def raw(self, *, when=None, priority: int = 0, watch: bool = False):
        """Every schema update: ``async def listener(update, users, chats)``."""
        return self._decorator("raw", [], priority, watch, dict(when=when))

    def error(self, func: Callable) -> Callable:
        """Errors from listeners: ``async def on_error(error, update)``."""
        self._router.error_listeners.append(func)
        return func

    def command_listener(self, names, prefix, case, priority, watch, constraints):
        return self._decorator("message", [Command(names, prefix, case)], priority, watch, constraints)
