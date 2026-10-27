# Wirelet

**Telegram over MTProto, for bots and personal accounts — in Python.**

Wirelet connects straight to Telegram with MTProto, the protocol the official
apps speak. No Bot API server in between: personal accounts work, files go up
to 2 GB (4 GB with Premium), and every method Telegram has is one call away.

```python
from wirelet import Wire

wire = Wire("my_bot", app_id=12345, app_hash="0123456789abcdef", bot="123:ABC...")


@wire.command("start")
async def start(msg):
    name = await msg.ask("Hi! What's your name?")
    await msg.quote(f"Nice to meet you, <b>{name.text}</b>.")


wire.serve()
```

- **Layer 232**: all 1,756 objects and 869 methods, generated from Telegram's own schema
- **Bots and people**: bot tokens, phone login, two-step verification, portable login keys
- **Conversations built in**: `await msg.ask("...")` waits for the answer
- **Listeners with plain keywords**: `chat="dm"`, `by="me"`, `has="photo"` — no filter objects to import
- **One `send` for everything**: text, photos, videos, voice, files, buttons, scheduling
- **Holds up**: reconnects, resends, follows data-center moves, waits out short flood limits, recovers missed updates

> Status: 0.2, early. The protocol is tested end to end against an in-process
> MTProto server; please report anything odd you see on the real network.

## Install

```bash
pip install "git+https://github.com/speedLn/Wirelet"
# faster crypto for big files + SOCKS/HTTP proxies:
pip install "wirelet[fast,proxy] @ git+https://github.com/speedLn/Wirelet"
```

Python 3.10+. Get `app_id` and `app_hash` at <https://my.telegram.org>; MTProto needs them for bots too.

## Listening

`wire.on` has one decorator per kind of update. Listeners take **one argument**.

| Decorator | Gets |
|---|---|
| `@wire.on.message(...)` | a new `Message` |
| `@wire.on.edit(...)` | an edited `Message` |
| `@wire.on.delete(...)` | `Deleted` (`.ids`, `.chat_id`) |
| `@wire.on.press(data)` | a `Press` on one of your buttons |
| `@wire.on.inline(...)` | an `Inline` query (`@yourbot text`) |
| `@wire.on.raw(...)` | `(update, users, chats)` schema objects |
| `@wire.on.error` | `(error, update)` when a listener raises |
| `@wire.command(name)` | a `Message` with `.cmd`, `.args`, `.rest` |

Say *which* updates with keywords — all given ones must hold:

```python
@wire.on.message(chat="dm", has="photo")          # photos in private chats
@wire.on.message(by="me", pattern=r"^\.(\w+)")    # your own messages starting with .word
@wire.on.message(chat=["@mychannel", -1001234567890])
@wire.command("ban", chat="group", by=[ADMIN_ID])
@wire.on.message(when=lambda m: len(m.text) > 500)   # anything else: a function (sync or async)
```

`chat`: `"dm" "bot" "group" "supergroup" "channel"`, ids, `@handles` ·
`by`: `"me" "bot" "human"`, ids, `@handles` · `text`: exact text ·
`pattern`: regex (sets `.match`) · `has`: `"media" "text" "reply" "forward" "buttons"` or a media kind ·
`mine`: sent by you or not · `when`: your own check.

**Order:** the highest `priority=` goes first, and the first match handles the update.
Return `NEXT` to hand it on to the next match. `watch=True` listeners see every
match without taking it away (logging, stats):

```python
from wirelet import NEXT

@wire.on.message(watch=True)
async def log(msg):
    print(msg.chat.name, msg.text)

@wire.on.message(priority=10, by="@spammer")
async def drop(msg):
    await msg.erase()          # handled; lower-priority listeners never see it
```

## A personal account

```python
from wirelet import Wire

wire = Wire("me", app_id=12345, app_hash="0123456789abcdef")


@wire.command("ping", prefix=".", by="me")
async def ping(msg):
    await msg.rewrite("pong 🏓")


wire.serve()   # asks for your number, the code and your 2FA password the first time
```

## Messages

```python
await msg.say("to the same chat")
await msg.quote("as a reply")
await msg.rewrite("edited text")
await msg.erase()
await msg.relay("@friend")                    # forward
await msg.relay("@friend", anonymous=True)    # copy without the header
path = await msg.save()                       # download its file
answer = await msg.ask("Sure? yes/no", timeout=60)
original = await msg.replied()

async with msg.typing():                      # "typing…" while you work
    ...
```

Reading one: `msg.text` (text, or the caption), `msg.media.kind` (`photo video round gif audio voice sticker file`, or `poll`, `geo`…), `msg.author`, `msg.chat.kind`, `msg.html`, `msg.link`, `msg.sent_at`, `msg.from_me`.

## Sending

```python
await wire.send("@friend", "Hello <i>there</i>")
await wire.send(chat_id, "Holiday!", file="beach.jpg")              # kind guessed: photo
await wire.send(chat_id, file="talk.ogg", kind="voice")
await wire.send(chat_id, file="report.pdf", progress=lambda done, total: print(done, total))
await wire.send(chat_id, "Later", at=datetime(2026, 12, 31, 23, 59))
await wire.send(chat_id, file=msg.media)                           # re-send, no re-upload
```

## Buttons

```python
from wirelet import ui

await msg.say("Vote:", buttons=ui.inline(
    [ui.btn("👍", "up", style="success"), ui.btn("👎", "down", style="danger")],
    [ui.url("Rules", "https://example.com/rules")],
))


@wire.on.press(pattern=r"^(up|down)$")
async def vote(press):
    await press.ack(f"You voted {press.data}")
    await press.rewrite(f"Thanks, {press.author.first}!")
```

Also `ui.app`, `ui.share`, `ui.copy`, `ui.profile`; reply keyboards with
`ui.keyboard(["Yes", "No"], [ui.key("📍 Location", location=True)], once=True)`,
`ui.hide_keyboard()`, `ui.force_reply()`; inline results with `ui.article(...)`.

## Everything else: the schema

Each Telegram method is in `schema.methods`, each object in `schema.objects`:

```python
from wirelet import schema

config = await wire.call(schema.methods.help.GetConfig())
peer = await wire.resolve("@durov")
```

## Faults

Telegram's errors keep Telegram's names:

```python
from wirelet import faults

try:
    await wire.send(chat, "hi")
except faults.FLOOD_WAIT as e:
    print("wait", e.amount, "s")
except faults.CHAT_WRITE_FORBIDDEN:
    ...
except faults.Denied:          # any 403
    ...
```

## Logins

The login lives in `<name>.wire` next to your script. Treat it like a password.
For servers, export a key once and keep it in an environment variable:

```python
print(wire.export_key())
wire = Wire(key=os.environ["WIRE_KEY"])
```

## Options

```python
Wire(
    "name",
    format="markdown",          # how text is read: "html" (default), "markdown", None
    concurrency=8,              # updates handled at once
    auto_wait=10,               # wait out flood limits up to this many seconds
    backlog=True,               # also deliver what arrived while offline
    listen=False,               # send-only scripts
    proxy="socks5://user:pass@127.0.0.1:1080",
    test_servers=True,
)
```

Several at once:

```python
import wirelet
wirelet.run(Wire("bot_one", bot=...), Wire("bot_two", bot=...))
```

## New Telegram layer?

Drop the new `api.tl` into `compiler/schema/` and run `python -m compiler`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

`tests/mtproto_server.py` plays Telegram's side of MTProto: the real handshake,
encryption, salts and updates. So login, conversations, buttons, reconnects and
faults are all tested without a network or an account.

## License

MIT
