"""Things only bots do."""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Sequence, Tuple, Union

from wirelet import schema


class BotActions:
    async def ack(self, press_id: int, text: Optional[str] = None, *, alert: bool = False,
                  url: Optional[str] = None, cache: int = 0) -> bool:
        """Answer a button press (see :meth:`Press.ack`)."""
        return await self.call(schema.methods.messages.SetBotCallbackAnswer(
            query_id=press_id, message=text, alert=alert or None, url=url, cache_time=cache))

    async def offer(self, query_id: int, results: Sequence, *, cache: int = 300, personal: bool = False,
                    next_offset: str = "") -> bool:
        """Answer an inline query (see :meth:`Inline.offer`)."""
        return await self.call(schema.methods.messages.SetInlineBotResults(
            query_id=query_id, results=[await r.encode(self) for r in results], cache_time=cache,
            private=personal or None, next_offset=next_offset or None))

    async def set_commands(self, commands: Union[Dict[str, str], Iterable[Tuple[str, str]]], *,
                           lang: str = "", scope: Optional["schema.kinds.BotCommandScope"] = None) -> bool:
        """The command menu: ``{"start": "Start", "help": "How to use me"}``."""
        pairs = list(commands.items()) if isinstance(commands, dict) else list(commands)
        return await self.call(schema.methods.bots.SetBotCommands(
            scope=scope or schema.objects.BotCommandScopeDefault(), lang_code=lang,
            commands=[schema.objects.BotCommand(command=c.lstrip("/"), description=d) for c, d in pairs]))

    async def clear_commands(self, *, lang: str = "", scope: Optional["schema.kinds.BotCommandScope"] = None) -> bool:
        return await self.call(schema.methods.bots.ResetBotCommands(
            scope=scope or schema.objects.BotCommandScopeDefault(), lang_code=lang))
