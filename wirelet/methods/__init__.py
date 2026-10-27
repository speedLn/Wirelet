from .auth import Auth, SentCode
from .bots import Bots
from .chats import Chats
from .decorators import Decorators
from .media import Media_
from .messages import Messages


class Methods(Auth, Bots, Chats, Decorators, Media_, Messages):
    """Every high-level method of :class:`wirelet.Client`."""


__all__ = ["Methods", "SentCode"]
