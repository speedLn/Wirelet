"""A userbot: commands you type yourself, starting with a dot."""

import os
import time

from wirelet import Client, filters

app = Client("my_account", api_id=os.environ.get("API_ID"), api_hash=os.environ.get("API_HASH"))


@app.on_message(filters.me & filters.command("ping", prefixes="."))
async def ping(client, message):
    start = time.perf_counter()
    await message.edit_text("…")
    await message.edit_text(f"pong in <code>{(time.perf_counter() - start) * 1000:.0f} ms</code>")


@app.on_message(filters.me & filters.command("id", prefixes="."))
async def ids(client, message):
    target = await message.get_reply_message() or message
    await message.edit_text(
        f"chat: <code>{message.chat.id}</code>\nuser: <code>{target.from_user.id}</code>")


app.run()
