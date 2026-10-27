"""Reply markups: inline keyboards, reply keyboards and friends.

Built on layer 232's button model, where every button is one
``keyboardButton``/``keyboardInlineButton`` carrying a ``type``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Sequence, Union

from wirelet import raw
from .object import Object

if TYPE_CHECKING:
    from wirelet import Client


def _style(style: Optional[str]):
    """``"primary"``, ``"danger"`` or ``"success"`` button colours."""
    if not style:
        return None
    flags = {"primary": "bg_primary", "danger": "bg_danger", "success": "bg_success"}
    if style not in flags:
        raise ValueError("style must be 'primary', 'danger' or 'success'")
    return raw.types.KeyboardButtonStyle(**{flags[style]: True})


class InlineKeyboardButton(Object):
    """One button under a message. Give exactly one action."""

    def __init__(self, text: str, callback_data: Union[str, bytes, None] = None, *,
                 url: Optional[str] = None, web_app_url: Optional[str] = None,
                 switch_inline_query: Optional[str] = None,
                 switch_inline_query_current_chat: Optional[str] = None,
                 user_id: Optional[int] = None, copy_text: Optional[str] = None,
                 style: Optional[str] = None) -> None:
        self.text = text
        self.callback_data = callback_data
        self.url = url
        self.web_app_url = web_app_url
        self.switch_inline_query = switch_inline_query
        self.switch_inline_query_current_chat = switch_inline_query_current_chat
        self.user_id = user_id
        self.copy_text = copy_text
        self.style = style

    async def write(self, client: "Client"):
        t = raw.types
        if self.callback_data is not None:
            data = self.callback_data.encode() if isinstance(self.callback_data, str) else self.callback_data
            if len(data) > 64:
                raise ValueError("callback_data can be at most 64 bytes")
            kind = t.InlineButtonTypeCallback(data=data)
        elif self.url is not None:
            kind = t.InlineButtonTypeUrl(url=self.url)
        elif self.web_app_url is not None:
            kind = t.InlineButtonTypeWebView(url=self.web_app_url)
        elif self.switch_inline_query is not None:
            kind = t.InlineButtonTypeSwitchInline(query=self.switch_inline_query)
        elif self.switch_inline_query_current_chat is not None:
            kind = t.InlineButtonTypeSwitchInline(query=self.switch_inline_query_current_chat, same_peer=True)
        elif self.user_id is not None:
            from wirelet.utils import to_input_user
            kind = t.InputInlineButtonTypeUserProfile(
                user_id=to_input_user(await client.resolve_peer(self.user_id)))
        elif self.copy_text is not None:
            kind = t.InlineButtonTypeCopy(copy_text=self.copy_text)
        else:
            raise ValueError(f"Button {self.text!r} needs an action (callback_data, url, ...)")
        return t.KeyboardInlineButton(text=self.text, type=kind, style=_style(self.style))

    @staticmethod
    def _parse(button) -> "InlineKeyboardButton":
        t = raw.types
        kind = button.type
        b = InlineKeyboardButton(button.text)
        if isinstance(kind, t.InlineButtonTypeCallback):
            try:
                b.callback_data = kind.data.decode()
            except UnicodeDecodeError:
                b.callback_data = kind.data
        elif isinstance(kind, t.InlineButtonTypeUrl):
            b.url = kind.url
        elif isinstance(kind, t.InlineButtonTypeWebView):
            b.web_app_url = kind.url
        elif isinstance(kind, t.InlineButtonTypeSwitchInline):
            if kind.same_peer:
                b.switch_inline_query_current_chat = kind.query
            else:
                b.switch_inline_query = kind.query
        elif isinstance(kind, t.InlineButtonTypeUserProfile):
            b.user_id = kind.user_id
        elif isinstance(kind, t.InlineButtonTypeCopy):
            b.copy_text = kind.copy_text
        return b


class InlineKeyboardMarkup(Object):
    def __init__(self, inline_keyboard: Sequence[Sequence[InlineKeyboardButton]]) -> None:
        self.inline_keyboard = [list(row) for row in inline_keyboard]

    async def write(self, client: "Client"):
        rows = []
        for row in self.inline_keyboard:
            rows.append(raw.types.KeyboardInlineButtonRow(
                buttons=[await b.write(client) for b in row]))
        return raw.types.ReplyInlineMarkup(rows=rows)

    @staticmethod
    def _parse(markup) -> Optional["InlineKeyboardMarkup"]:
        if not isinstance(markup, raw.types.ReplyInlineMarkup):
            return None
        return InlineKeyboardMarkup([
            [InlineKeyboardButton._parse(b) for b in row.buttons] for row in markup.rows
        ])


class KeyboardButton(Object):
    """A button on the reply keyboard that replaces the user's keyboard."""

    def __init__(self, text: str, *, request_contact: bool = False,
                 request_location: bool = False, web_app_url: Optional[str] = None,
                 style: Optional[str] = None) -> None:
        self.text = text
        self.request_contact = request_contact
        self.request_location = request_location
        self.web_app_url = web_app_url
        self.style = style

    def write(self):
        t = raw.types
        if self.request_contact:
            kind = t.ButtonTypeRequestPhone()
        elif self.request_location:
            kind = t.ButtonTypeRequestGeoLocation()
        elif self.web_app_url:
            kind = t.ButtonTypeSimpleWebView(url=self.web_app_url)
        else:
            kind = t.ButtonTypeDefault()
        return t.KeyboardButton(text=self.text, type=kind, style=_style(self.style))


class ReplyKeyboardMarkup(Object):
    def __init__(self, keyboard: Sequence[Sequence[Union[KeyboardButton, str]]], *,
                 resize_keyboard: bool = True, one_time_keyboard: bool = False,
                 selective: bool = False, placeholder: Optional[str] = None,
                 is_persistent: bool = False) -> None:
        self.keyboard = [list(row) for row in keyboard]
        self.resize_keyboard = resize_keyboard
        self.one_time_keyboard = one_time_keyboard
        self.selective = selective
        self.placeholder = placeholder
        self.is_persistent = is_persistent

    async def write(self, client: "Client"):
        rows: List = []
        for row in self.keyboard:
            buttons = [(KeyboardButton(b) if isinstance(b, str) else b).write() for b in row]
            rows.append(raw.types.KeyboardButtonRow(buttons=buttons))
        return raw.types.ReplyKeyboardMarkup(
            rows=rows,
            resize=self.resize_keyboard or None,
            single_use=self.one_time_keyboard or None,
            selective=self.selective or None,
            persistent=self.is_persistent or None,
            placeholder=self.placeholder,
        )


class ReplyKeyboardRemove(Object):
    def __init__(self, selective: bool = False) -> None:
        self.selective = selective

    async def write(self, client: "Client"):
        return raw.types.ReplyKeyboardHide(selective=self.selective or None)


class ForceReply(Object):
    def __init__(self, selective: bool = False, placeholder: Optional[str] = None) -> None:
        self.selective = selective
        self.placeholder = placeholder

    async def write(self, client: "Client"):
        return raw.types.ReplyKeyboardForceReply(
            selective=self.selective or None, placeholder=self.placeholder)


ReplyMarkup = Union[InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply]
