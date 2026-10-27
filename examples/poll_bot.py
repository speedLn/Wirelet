"""Buttons, presses, a conversation and inline mode."""

import os
from collections import Counter

import wirelet
from wirelet import Wire, ui

wire = Wire("poll_bot", app_id=os.environ.get("APP_ID"), app_hash=os.environ.get("APP_HASH"),
            bot=os.environ.get("BOT_TOKEN"))
votes = Counter()


def board():
    return ui.inline([
        ui.btn(f"👍 {votes['up']}", "up", style="success"),
        ui.btn(f"👎 {votes['down']}", "down", style="danger"),
    ])


@wire.command("poll")
async def poll(msg):
    question = await msg.ask("What should we vote on?")
    await msg.say(f"<b>{question.text}</b>", buttons=board())


@wire.on.press(pattern=r"^(up|down)$")
async def vote(press):
    votes[press.data] += 1
    await press.rebutton(board())
    await press.ack("Counted!")


@wire.on.inline()
async def shout(query):
    text = query.text or "nothing"
    await query.offer([
        ui.article("Shout", text.upper(), description=text.upper()),
        ui.article("Whisper", text.lower(), description=text.lower()),
    ], cache=1)


async def main():
    await wire.set_commands({"poll": "Start a vote"})
    await wirelet.forever()


wire.serve(main)
