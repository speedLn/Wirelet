"""Inline buttons, callback queries and an inline-mode answer."""

import os

from wirelet import Client, filters
from wirelet.types import InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle

app = Client("keyboard_bot", api_id=os.environ.get("API_ID"), api_hash=os.environ.get("API_HASH"),
             bot_token=os.environ.get("BOT_TOKEN"))

COUNTER = InlineKeyboardMarkup([[
    InlineKeyboardButton("−", "dec", style="danger"),
    InlineKeyboardButton("+", "inc", style="success"),
]])


@app.on_message(filters.command("counter"))
async def counter(client, message):
    await message.reply("Count: <b>0</b>", reply_markup=COUNTER)


@app.on_callback_query(filters.regex("^(inc|dec)$"))
async def change(client, query):
    value = int(query.message.text.split()[-1]) + (1 if query.data == "inc" else -1)
    await query.edit_message_text(f"Count: <b>{value}</b>", reply_markup=COUNTER)
    await query.answer()


@app.on_inline_query()
async def inline(client, query):
    text = query.query or "nothing"
    await query.answer([
        InlineQueryResultArticle("Shout it", text.upper(), description=text.upper()),
        InlineQueryResultArticle("Whisper it", text.lower(), description=text.lower()),
    ], cache_time=1)


async def main():
    await app.set_bot_commands({"counter": "A counter with buttons"})
    from wirelet import idle
    await idle()


app.run(main)
