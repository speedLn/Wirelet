"""Commands for your own account, typed with a dot."""

import os
import time

from wirelet import Wire

wire = Wire("me", app_id=os.environ.get("APP_ID"), app_hash=os.environ.get("APP_HASH"))


@wire.command("ping", prefix=".", by="me")
async def ping(msg):
    started = time.perf_counter()
    await msg.rewrite("…")
    await msg.rewrite(f"pong in <code>{(time.perf_counter() - started) * 1000:.0f} ms</code>")


@wire.command("id", prefix=".", by="me")
async def ids(msg):
    target = await msg.replied() or msg
    await msg.rewrite(f"chat <code>{msg.chat.id}</code> · user <code>{target.author.id}</code>")


@wire.command("save", prefix=".", by="me")
async def save(msg):
    original = await msg.replied()
    if original and original.media:
        path = await original.save()
        await msg.rewrite(f"saved to <code>{path}</code>")


wire.serve()
