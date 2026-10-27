"""wirelet - Telegram over MTProto, for bots and personal accounts.

    from wirelet import Wire

    wire = Wire("my_bot", app_id=123, app_hash="...", bot="123:ABC")

    @wire.command("start")
    async def start(msg):
        await msg.quote("Hello from wirelet!")

    wire.serve()
"""

__version__ = "0.2.0"

from . import faults, schema, ui  # noqa: E402
from .model import Chat, Deleted, Inline, Media, Message, Press, User  # noqa: E402
from .router import NEXT  # noqa: E402
from .wire import Wire, forever, run  # noqa: E402

__all__ = [
    "Chat", "Deleted", "Inline", "Media", "Message", "NEXT", "Press", "User", "Wire",
    "__version__", "faults", "forever", "run", "schema", "ui",
]
