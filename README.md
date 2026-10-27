# Wirelet

**A modern MTProto framework for Telegram — bots and user accounts — in Python.**

Wirelet talks to Telegram directly over MTProto, the same protocol the official
apps use. No Bot API server in between, so you get user accounts (userbots),
files up to 2 GB (4 GB with Premium), and every method Telegram has.

- **Latest API layer (232)**: all 1,756 types and 869 methods, generated straight from Telegram's schema
- **Bots and users**: bot tokens, phone login, two-step verification (SRP), session strings
- **Async** from top to bottom, with handlers, filters and HTML/Markdown formatting
- **Reliable updates**: tracks `pts` and fetches anything lost in transit
- **Survives the real world**: reconnects, resends, follows DC migrations, sleeps through short flood waits
- **Typed**: every raw object has a typed constructor, and `isinstance` works against abstract types

> Status: early (0.1). The core protocol is tested end to end against an
> in-process MTProto server; please report anything that misbehaves on the real network.

## Install

```bash
pip install "git+https://github.com/speedLn/Wirelet"
# faster encryption for big files, and SOCKS/HTTP proxy support:
pip install "wirelet[fast,proxy] @ git+https://github.com/speedLn/Wirelet"
```

Python 3.9+. You need an `api_id` and `api_hash` from <https://my.telegram.org> — for bots too, since MTProto logs in as an app.

## A bot in 10 lines

```python
from wirelet import Client, filters

app = Client("my_bot", api_id=12345, api_hash="0123456789abcdef", bot_token="123:ABC...")


@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply(f"Hello, <b>{message.from_user.first_name}</b>!")


app.run()
```

The first run creates `my_bot.session`. Later runs reuse it, so `api_id`, `api_hash` and `bot_token` are optional from then on. Keep `.session` files private: they are logins.

## A userbot

```python
from wirelet import Client, filters

app = Client("me", api_id=12345, api_hash="0123456789abcdef")


@app.on_message(filters.me & filters.command("ping", prefixes="."))
async def ping(client, message):
    await message.edit_text("pong 🏓")


app.run()  # asks for your phone number, login code and 2FA password the first time
```

## Buttons

```python
from wirelet.types import InlineKeyboardMarkup, InlineKeyboardButton


@app.on_message(filters.command("menu"))
async def menu(client, message):
    await message.reply("Pick one:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("👍 Yes", "yes", style="success"),
         InlineKeyboardButton("👎 No", "no", style="danger")],
        [InlineKeyboardButton("Website", url="https://example.com")],
    ]))


@app.on_callback_query(filters.regex("^(yes|no)$"))
async def answer(client, query):
    await query.answer(f"You said {query.data}")
    await query.edit_message_text(f"Answer: <b>{query.data}</b>")
```

## Files

```python
await app.send_photo("me", "cat.jpg", caption="A <i>cat</i>")
await app.send_document(chat_id, "report.pdf", progress=lambda done, total: print(done, total))
await app.send_video(chat_id, "https://example.com/clip.mp4")

path = await message.download()                      # -> "downloads/..."
data = await app.download_media(message, in_memory=True)  # -> BytesIO
await app.send_photo(other_chat, message.photo.file_id)    # re-send without re-uploading
```

## Handlers and filters

| Decorator | Gets |
|---|---|
| `on_message` / `on_edited_message` | `Message` |
| `on_callback_query` | `CallbackQuery` |
| `on_inline_query` | `InlineQuery` |
| `on_deleted_messages` | `DeletedMessages` |
| `on_raw_update` | `(update, users, chats)` raw objects |

Filters combine with `&`, `|`, `~`: `filters.text`, `photo`, `video`, `document`, `private`, `group`, `channel`, `incoming`, `outgoing`, `me`, `reply`, `forwarded`, `command(...)`, `regex(...)`, `user(...)`, `chat(...)`, and your own with `filters.create(func)`.

Handlers run in groups: `@app.on_message(filters.text, group=1)`. In each group, the first matching handler runs. Raise `StopPropagation` to stop there, or `ContinuePropagation` to let the next one try.

## The whole API, raw

Anything without a friendly method is one `invoke` away:

```python
from wirelet import raw

result = await app.invoke(raw.functions.messages.GetDialogFilters())
peer = await app.resolve_peer("@durov")
full = await app.invoke(raw.functions.users.GetFullUser(id=raw.types.InputUser(
    user_id=peer.user_id, access_hash=peer.access_hash)))
```

Errors are classes named after Telegram's error, even ones newer than this release:

```python
from wirelet import errors

try:
    await app.send_message(chat_id, "hi")
except errors.FloodWait as e:      # also an errors.Flood
    print("wait", e.value, "seconds")
except errors.ChatWriteForbidden:  # also an errors.Forbidden
    ...
```

## Sessions

```python
string = app.export_session_string()          # one line, e.g. for an env variable
app = Client("bot", session_string=string)    # logs straight in, no file
```

## More options

```python
Client(
    "name",
    parse_mode="markdown",   # or "html" (default) or None
    workers=8,               # concurrent handlers
    sleep_threshold=10,      # auto-sleep flood waits up to this many seconds
    skip_updates=False,      # also process what arrived while offline
    no_updates=True,         # scripts that only send
    proxy={"scheme": "socks5", "hostname": "127.0.0.1", "port": 1080},
    test_mode=True,          # Telegram's test servers
)
```

Several clients at once:

```python
from wirelet import idle

async def main():
    async with Client("bot1", bot_token=...) as a, Client("bot2", bot_token=...) as b:
        await idle()
```

## Updating to a new Telegram layer

The API is generated from Telegram's schema in `compiler/schema/`. Drop in the new `api.tl` and run:

```bash
python -m compiler
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests include a small MTProto server (`tests/mtproto_server.py`) that performs the real handshake and encryption from Telegram's side. That lets login, updates, reconnects and errors be tested without a network or an account.

## License

MIT
