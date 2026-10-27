"""Telegram's complete API, generated from its TL schema.

* ``schema.methods`` - every API method, e.g. ``schema.methods.messages.SendMessage``
* ``schema.objects`` - every object, e.g. ``schema.objects.User``
* ``schema.kinds``   - abstract types for ``isinstance``, e.g. ``schema.kinds.InputPeer``

Call any method with :meth:`wirelet.Wire.call`.
"""

from . import core
from . import kinds, objects, methods
from .registry import LAYER

__all__ = ["core", "kinds", "objects", "methods", "LAYER"]
