from __future__ import annotations

from typing import Callable, Optional

from wirelet.dispatcher import (
    CALLBACK_QUERY, DELETED_MESSAGES, EDITED_MESSAGE, INLINE_QUERY, MESSAGE, RAW_UPDATE, Handler,
)


class Decorators:
    def add_handler(self, kind: str, callback: Callable, filters=None, group: int = 0) -> Handler:
        """Register ``callback`` for updates of ``kind`` (see the ``on_*`` decorators)."""
        return self.dispatcher.add(Handler(kind, callback, filters), group)

    def remove_handler(self, handler: Handler, group: int = 0) -> None:
        self.dispatcher.remove(handler, group)

    def _decorator(self, kind: str, filters, group: int):
        def wrap(func: Callable) -> Callable:
            self.add_handler(kind, func, filters, group)
            return func
        return wrap

    def on_message(self, filters=None, group: int = 0):
        """New messages: ``async def handler(client, message)``."""
        return self._decorator(MESSAGE, filters, group)

    def on_edited_message(self, filters=None, group: int = 0):
        return self._decorator(EDITED_MESSAGE, filters, group)

    def on_deleted_messages(self, filters=None, group: int = 0):
        """``async def handler(client, deleted)``, where ``deleted.message_ids`` lists ids."""
        return self._decorator(DELETED_MESSAGES, filters, group)

    def on_callback_query(self, filters=None, group: int = 0):
        return self._decorator(CALLBACK_QUERY, filters, group)

    def on_inline_query(self, filters=None, group: int = 0):
        return self._decorator(INLINE_QUERY, filters, group)

    def on_raw_update(self, group: int = 0, filters: Optional[object] = None):
        """Every raw update: ``async def handler(client, update, users, chats)``."""
        return self._decorator(RAW_UPDATE, filters, group)
