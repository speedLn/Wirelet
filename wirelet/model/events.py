"""Button presses, inline queries and deletions."""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Union

from .base import Model
from .message import Message
from .people import Chat, User


class Press(Model):
    """Someone pressed a button made with :func:`wirelet.ui.btn`.

    Always answer with :meth:`ack` (with or without text), or their app shows
    a spinner on the button for a while.
    """

    def __init__(self, *, id: int, author: User, data: Union[str, bytes, None],
                 msg: Optional[Message] = None, inline_id=None, chat_instance: int = 0,
                 game: Optional[str] = None, raw=None) -> None:
        self.id = id
        self.author = author
        self.data = data
        self.msg = msg
        self.inline_id = inline_id
        self.chat_instance = chat_instance
        self.game = game
        self.raw = raw
        self.match: Optional[re.Match] = None

    @property
    def chat(self) -> Optional[Chat]:
        return self.msg.chat if self.msg is not None else None

    async def ack(self, text: Optional[str] = None, *, alert: bool = False,
                  url: Optional[str] = None, cache: int = 0) -> bool:
        """Answer the press: nothing, a short toast, or an ``alert`` dialog."""
        return await self.wire.ack(self.id, text, alert=alert, url=url, cache=cache)

    async def rewrite(self, text: str, **options) -> Optional[Message]:
        """Edit the message the button is under."""
        if self.inline_id is not None:
            await self.wire.rewrite_inline(self.inline_id, text, **options)
            return None
        return await self.wire.rewrite(self.msg.chat.id, self.msg.id, text, **options)

    async def rebutton(self, buttons=None) -> Optional[Message]:
        return await self.wire.rebutton(self.msg.chat.id, self.msg.id, buttons)


class Inline(Model):
    """Someone is typing ``@yourbot text`` in a chat."""

    def __init__(self, *, id: int, author: User, text: str, offset: str,
                 chat_kind: Optional[str] = None, raw=None) -> None:
        self.id = id
        self.author = author
        self.text = text
        self.offset = offset
        self.chat_kind = chat_kind
        self.raw = raw
        self.match: Optional[re.Match] = None

    async def offer(self, results: Sequence, *, cache: int = 300, personal: bool = False,
                    next_offset: str = "") -> bool:
        """Show these results (from :func:`wirelet.ui.article`)."""
        return await self.wire.offer(self.id, results, cache=cache, personal=personal, next_offset=next_offset)


class Deleted(Model):
    """Messages were deleted. ``chat_id`` is only known in channels and supergroups;
    Telegram doesn't say which private chat or basic group they were in."""

    def __init__(self, chat_id: Optional[int], ids: List[int]) -> None:
        self.chat_id = chat_id
        self.ids = ids
