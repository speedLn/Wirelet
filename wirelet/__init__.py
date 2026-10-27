"""wirelet - a modern MTProto framework for Telegram bots and user accounts.

    from wirelet import Client, filters

    app = Client("my_bot", api_id=..., api_hash="...", bot_token="...")

    @app.on_message(filters.command("start"))
    async def start(client, message):
        await message.reply("Hello from wirelet!")

    app.run()
"""

__version__ = "0.1.0"

from . import enums, errors, filters, raw, types  # noqa: E402
from .client import Client, idle  # noqa: E402
from .dispatcher import ContinuePropagation, StopPropagation  # noqa: E402

__all__ = [
    "Client", "ContinuePropagation", "StopPropagation", "enums", "errors", "filters",
    "idle", "raw", "types", "__version__",
]
