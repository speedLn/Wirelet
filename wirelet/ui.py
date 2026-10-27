"""Buttons, keyboards and inline-mode results.

Build them with small functions::

    from wirelet import ui

    buttons = ui.inline(
        [ui.btn("👍 Yes", "yes", style="success"), ui.btn("👎 No", "no", style="danger")],
        [ui.url("Website", "https://example.com")],
    )
    await wire.send(chat, "Pick one", buttons=buttons)

    await wire.send(chat, "Share?", buttons=ui.keyboard(
        [ui.key("📞 My number", contact=True)], ["Cancel"], once=True))

Uses layer 232's button model: every button is one ``keyboardButton`` /
``keyboardInlineButton`` with a ``type``, and may have a colour ``style``
(``"primary"``, ``"success"`` or ``"danger"``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Sequence, Union

from wirelet import schema

if TYPE_CHECKING:
    from wirelet.wire import Wire

STYLES = {"primary": "bg_primary", "danger": "bg_danger", "success": "bg_success"}


def _style(style: Optional[str]):
    if style is None:
        return None
    if style not in STYLES:
        raise ValueError(f"style must be one of {', '.join(STYLES)}")
    return schema.objects.KeyboardButtonStyle(**{STYLES[style]: True})


# --------------------------------------------------------------------------- #
# Buttons under a message
# --------------------------------------------------------------------------- #

class Button:
    """One inline button. Made by :func:`btn`, :func:`url`, :func:`app`,
    :func:`share`, :func:`copy` or :func:`profile`."""

    __slots__ = ("text", "action", "value", "style", "here")

    def __init__(self, text: str, action: str, value, style: Optional[str] = None, here: bool = False) -> None:
        self.text = text
        self.action = action      # "data" | "url" | "app" | "share" | "copy" | "profile"
        self.value = value
        self.style = style
        self.here = here

    @property
    def data(self) -> Union[str, bytes, None]:
        return self.value if self.action == "data" else None

    def __repr__(self) -> str:
        return f"ui.Button({self.text!r}, {self.action}={self.value!r})"

    async def encode(self, wire: "Wire"):
        o = schema.objects
        if self.action == "data":
            raw = self.value.encode() if isinstance(self.value, str) else bytes(self.value)
            if len(raw) > 64:
                raise ValueError(f"button {self.text!r}: data is {len(raw)} bytes, the limit is 64")
            kind = o.InlineButtonTypeCallback(data=raw)
        elif self.action == "url":
            kind = o.InlineButtonTypeUrl(url=self.value)
        elif self.action == "app":
            kind = o.InlineButtonTypeWebView(url=self.value)
        elif self.action == "share":
            kind = o.InlineButtonTypeSwitchInline(query=self.value, same_peer=self.here or None)
        elif self.action == "copy":
            kind = o.InlineButtonTypeCopy(copy_text=self.value)
        elif self.action == "profile":
            from wirelet.utils import to_input_user
            kind = o.InputInlineButtonTypeUserProfile(user_id=to_input_user(await wire.resolve(self.value)))
        else:
            raise ValueError(f"unknown button action {self.action!r}")
        return o.KeyboardInlineButton(text=self.text, type=kind, style=_style(self.style))


def btn(text: str, data: Union[str, bytes], *, style: Optional[str] = None) -> Button:
    """A button that sends ``data`` (max 64 bytes) back; handle it with ``wire.on.press``."""
    return Button(text, "data", data, style)


def url(text: str, link: str, *, style: Optional[str] = None) -> Button:
    """A button that opens a link."""
    return Button(text, "url", link, style)


def app(text: str, link: str, *, style: Optional[str] = None) -> Button:
    """A button that opens a Mini App."""
    return Button(text, "app", link, style)


def share(text: str, query: str = "", *, here: bool = False, style: Optional[str] = None) -> Button:
    """A button that starts an inline query, in another chat (or this one with ``here``)."""
    return Button(text, "share", query, style, here)


def copy(text: str, value: str, *, style: Optional[str] = None) -> Button:
    """A button that copies ``value`` to the clipboard."""
    return Button(text, "copy", value, style)


def profile(text: str, user: Union[int, str], *, style: Optional[str] = None) -> Button:
    """A button that opens someone's profile."""
    return Button(text, "profile", user, style)


Row = Union[Button, Sequence[Button]]


class Grid:
    """Rows of inline buttons, as made by :func:`inline`."""

    def __init__(self, rows: List[List[Button]]) -> None:
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, index: int) -> List[Button]:
        return self.rows[index]

    def buttons(self) -> List[Button]:
        return [b for row in self.rows for b in row]

    def find(self, text: str) -> Optional[Button]:
        return next((b for b in self.buttons() if b.text == text), None)

    def __repr__(self) -> str:
        return f"ui.Grid({self.rows!r})"

    async def encode(self, wire: "Wire"):
        o = schema.objects
        return o.ReplyInlineMarkup(rows=[
            o.KeyboardInlineButtonRow(buttons=[await b.encode(wire) for b in row]) for row in self.rows
        ])


def inline(*rows: Row) -> Grid:
    """Buttons under a message. Each argument is a row: a list of buttons, or one button."""
    return Grid([[r] if isinstance(r, Button) else list(r) for r in rows])


def grid_from(markup) -> Optional[Grid]:
    """Turn received ``replyInlineMarkup`` back into a :class:`Grid`."""
    o = schema.objects
    if not isinstance(markup, o.ReplyInlineMarkup):
        return None
    rows = []
    for row in markup.rows:
        out = []
        for b in row.buttons:
            kind = b.type
            if isinstance(kind, o.InlineButtonTypeCallback):
                try:
                    value = kind.data.decode()
                except UnicodeDecodeError:
                    value = kind.data
                out.append(Button(b.text, "data", value))
            elif isinstance(kind, o.InlineButtonTypeUrl):
                out.append(Button(b.text, "url", kind.url))
            elif isinstance(kind, o.InlineButtonTypeWebView):
                out.append(Button(b.text, "app", kind.url))
            elif isinstance(kind, o.InlineButtonTypeSwitchInline):
                out.append(Button(b.text, "share", kind.query, here=bool(kind.same_peer)))
            elif isinstance(kind, o.InlineButtonTypeCopy):
                out.append(Button(b.text, "copy", kind.copy_text))
            elif isinstance(kind, o.InlineButtonTypeUserProfile):
                out.append(Button(b.text, "profile", kind.user_id))
            else:
                out.append(Button(b.text, type(kind).__name__.removeprefix("InlineButtonType").lower(), None))
        rows.append(out)
    return Grid(rows)


# --------------------------------------------------------------------------- #
# Reply keyboards
# --------------------------------------------------------------------------- #

class Key:
    """A key on a reply keyboard, made by :func:`key`."""

    __slots__ = ("text", "contact", "location", "app", "style")

    def __init__(self, text: str, contact: bool = False, location: bool = False,
                 app: Optional[str] = None, style: Optional[str] = None) -> None:
        self.text = text
        self.contact = contact
        self.location = location
        self.app = app
        self.style = style

    def encode(self):
        o = schema.objects
        if self.contact:
            kind = o.ButtonTypeRequestPhone()
        elif self.location:
            kind = o.ButtonTypeRequestGeoLocation()
        elif self.app:
            kind = o.ButtonTypeSimpleWebView(url=self.app)
        else:
            kind = o.ButtonTypeDefault()
        return o.KeyboardButton(text=self.text, type=kind, style=_style(self.style))


def key(text: str, *, contact: bool = False, location: bool = False,
        app: Optional[str] = None, style: Optional[str] = None) -> Key:
    """A reply-keyboard key; it can ask for the user's number or location, or open a Mini App."""
    return Key(text, contact, location, app, style)


class Keyboard:
    def __init__(self, rows, resize: bool, once: bool, placeholder: Optional[str],
                 persistent: bool, selective: bool) -> None:
        self.rows = rows
        self.resize = resize
        self.once = once
        self.placeholder = placeholder
        self.persistent = persistent
        self.selective = selective

    async def encode(self, wire: "Wire"):
        o = schema.objects
        rows = [o.KeyboardButtonRow(buttons=[(Key(k) if isinstance(k, str) else k).encode() for k in row])
                for row in self.rows]
        return o.ReplyKeyboardMarkup(
            rows=rows, resize=self.resize or None, single_use=self.once or None,
            persistent=self.persistent or None, selective=self.selective or None,
            placeholder=self.placeholder,
        )


def keyboard(*rows: Union[str, Key, Sequence[Union[str, Key]]], resize: bool = True, once: bool = False,
             placeholder: Optional[str] = None, persistent: bool = False, selective: bool = False) -> Keyboard:
    """A keyboard that replaces the user's own. Rows are lists of strings or :func:`key`."""
    return Keyboard([[r] if isinstance(r, (str, Key)) else list(r) for r in rows],
                    resize, once, placeholder, persistent, selective)


class _Simple:
    def __init__(self, build) -> None:
        self._build = build

    async def encode(self, wire: "Wire"):
        return self._build()


def hide_keyboard(*, selective: bool = False) -> _Simple:
    """Remove a reply keyboard."""
    return _Simple(lambda: schema.objects.ReplyKeyboardHide(selective=selective or None))


def force_reply(*, placeholder: Optional[str] = None, selective: bool = False) -> _Simple:
    """Make the user's app open a reply to this message."""
    return _Simple(lambda: schema.objects.ReplyKeyboardForceReply(
        placeholder=placeholder, selective=selective or None))


# --------------------------------------------------------------------------- #
# Inline-mode results
# --------------------------------------------------------------------------- #

class Article:
    """A text result for an inline query, made by :func:`article`."""

    def __init__(self, title: str, text: str, *, id: Optional[str], description: Optional[str],
                 link: Optional[str], thumb: Optional[str], buttons: Optional[Grid],
                 format: Optional[str], preview: bool) -> None:
        import uuid

        self.id = id or uuid.uuid4().hex
        self.title = title
        self.text = text
        self.description = description
        self.link = link
        self.thumb = thumb
        self.buttons = buttons
        self.format = format
        self.preview = preview

    async def encode(self, wire: "Wire"):
        o = schema.objects
        text, entities = await wire._format(self.text, self.format, None)
        thumb = (o.InputWebDocument(url=self.thumb, size=0, mime_type="image/jpeg", attributes=[])
                 if self.thumb else None)
        return o.InputBotInlineResult(
            id=self.id, type="article", title=self.title, description=self.description,
            url=self.link, thumb=thumb,
            send_message=o.InputBotInlineMessageText(
                message=text, entities=entities or None, no_webpage=(not self.preview) or None,
                reply_markup=await self.buttons.encode(wire) if self.buttons else None,
            ),
        )


def article(title: str, text: str, *, description: Optional[str] = None, link: Optional[str] = None,
            thumb: Optional[str] = None, buttons: Optional[Grid] = None, format: Optional[str] = "default",
            preview: bool = True, id: Optional[str] = None) -> Article:
    """A result that sends ``text`` when picked."""
    return Article(title, text, id=id, description=description, link=link, thumb=thumb,
                   buttons=buttons, format=format, preview=preview)
