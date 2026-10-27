from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple, Union

from wirelet import raw


class Bots:
    async def answer_callback_query(self, callback_query_id: int, text: Optional[str] = None,
                                    show_alert: bool = False, url: Optional[str] = None,
                                    cache_time: int = 0) -> bool:
        return await self.invoke(raw.functions.messages.SetBotCallbackAnswer(
            query_id=callback_query_id, message=text, alert=show_alert or None,
            url=url, cache_time=cache_time,
        ))

    async def answer_inline_query(self, inline_query_id: int, results: list, cache_time: int = 300,
                                  is_personal: bool = False, next_offset: str = "") -> bool:
        return await self.invoke(raw.functions.messages.SetInlineBotResults(
            query_id=inline_query_id,
            results=[await r.write(self) for r in results],
            cache_time=cache_time,
            private=is_personal or None,
            next_offset=next_offset or None,
        ))

    async def set_bot_commands(
        self,
        commands: Union[Dict[str, str], Sequence[Tuple[str, str]]],
        language_code: str = "",
        scope: Optional["raw.base.BotCommandScope"] = None,
    ) -> bool:
        """Set the command menu, e.g. ``{"start": "Start the bot", "help": "Help"}``."""
        pairs: List[Tuple[str, str]] = list(commands.items()) if isinstance(commands, dict) else list(commands)
        return await self.invoke(raw.functions.bots.SetBotCommands(
            scope=scope or raw.types.BotCommandScopeDefault(),
            lang_code=language_code,
            commands=[raw.types.BotCommand(command=c.lstrip("/"), description=d) for c, d in pairs],
        ))

    async def delete_bot_commands(self, language_code: str = "",
                                  scope: Optional["raw.base.BotCommandScope"] = None) -> bool:
        return await self.invoke(raw.functions.bots.ResetBotCommands(
            scope=scope or raw.types.BotCommandScopeDefault(), lang_code=language_code))
