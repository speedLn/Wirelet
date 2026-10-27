"""Echo every private text message back. Set API_ID, API_HASH and BOT_TOKEN."""

import os

from wirelet import Client, filters

app = Client(
    "echo_bot",
    api_id=os.environ.get("API_ID"),
    api_hash=os.environ.get("API_HASH"),
    bot_token=os.environ.get("BOT_TOKEN"),
)


@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply("Send me anything and I'll send it back.")


@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def echo(client, message):
    await message.reply(message.text, parse_mode=None)


@app.on_message(filters.private & filters.media)
async def echo_media(client, message):
    await message.copy(message.chat.id)


app.run()
