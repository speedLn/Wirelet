"""Generate wirelet.raw from the TL schema whenever the package is built."""

import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        sys.path.insert(0, str(root))
        try:
            from compiler.tl import build
            build()
        finally:
            sys.path.remove(str(root))
