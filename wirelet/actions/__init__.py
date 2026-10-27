from .auth import AuthActions, CodeTicket
from .bots import BotActions
from .files import FileActions
from .messaging import Activity, MessagingActions
from .peers import PeerActions


class Actions(AuthActions, BotActions, FileActions, MessagingActions, PeerActions):
    """Every high-level action of :class:`wirelet.Wire`."""


__all__ = ["Actions", "Activity", "CodeTicket"]
