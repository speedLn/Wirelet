"""Use the schema directly from a short script."""

import asyncio
import os

from wirelet import Wire, schema


async def main():
    async with Wire("me", app_id=os.environ.get("APP_ID"), app_hash=os.environ.get("APP_HASH"),
                    listen=False) as wire:
        print("Signed in as", wire.me.name)

        config = await wire.call(schema.methods.help.GetConfig())
        print("DC", config.this_dc, "· group limit", config.chat_size_max)

        async for chat in wire.chats(limit=5):
            print(chat.kind, chat.name)


asyncio.run(main())
