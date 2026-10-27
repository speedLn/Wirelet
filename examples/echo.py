"""Echo whatever people send in private. Needs APP_ID, APP_HASH and BOT_TOKEN."""

import os

from wirelet import Wire

wire = Wire("echo", app_id=os.environ.get("APP_ID"), app_hash=os.environ.get("APP_HASH"),
            bot=os.environ.get("BOT_TOKEN"))


@wire.command("start")
async def start(msg):
    await msg.say("Send me anything and it comes right back.")


@wire.on.message(chat="dm", has="text")
async def echo_text(msg):
    await msg.say(msg.text, format=None)


@wire.on.message(chat="dm", has="media")
async def echo_media(msg):
    await msg.relay(msg.chat.id, anonymous=True)


wire.serve()
