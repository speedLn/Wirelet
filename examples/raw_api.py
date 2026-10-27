"""Call any Telegram method directly, as a short script without handlers."""

import asyncio
import os

from wirelet import Client, raw


async def main():
    async with Client("my_account", api_id=os.environ.get("API_ID"),
                      api_hash=os.environ.get("API_HASH"), no_updates=True) as app:
        me = await app.get_me()
        print(f"Logged in as {me.full_name} (id {me.id})")

        config = await app.invoke(raw.functions.help.GetConfig())
        print("This DC:", config.this_dc, "- max group size:", config.chat_size_max)

        async for chat in app.get_dialogs(limit=5):
            print(chat.type, chat.name)

        print("Session string:", app.export_session_string()[:20] + "…")


asyncio.run(main())
