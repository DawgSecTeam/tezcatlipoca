"""dpatch: patch one name in every deploy_lib module (and the `deploy` facade) that binds it.

The deploy pipeline is split across many small modules, each of which imports the names it
uses (`from range_ops import destroy_vm_if_exists`), so a test cannot know -- and should not
care -- which module's binding the code under test resolves. dpatch("destroy_vm_if_exists")
replaces the binding everywhere it exists, with ONE shared mock, and restores it on exit.
Usable as a context manager, via ExitStack.enter_context, or with start()/stop().
"""

import importlib
import pkgutil
import sys
from pathlib import Path
from unittest.mock import DEFAULT, patch

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import deploy_lib  # noqa: E402


def _modules():
    mods = [importlib.import_module("deploy")]
    for info in pkgutil.walk_packages(deploy_lib.__path__, "deploy_lib."):
        mods.append(importlib.import_module(info.name))
    mods.append(deploy_lib)
    return mods


class dpatch:  # noqa: N801 - reads like patch.object at the call site
    def __init__(self, name, new=DEFAULT, **kwargs):
        self.name, self.new, self.kwargs = name, new, kwargs
        self._patchers = []
        self.mock = None

    def start(self):
        holders = [m for m in _modules() if self.name in vars(m)]
        if not holders:
            raise AttributeError(f"no deploy_lib module binds {self.name!r}")
        first = patch.object(holders[0], self.name, self.new, **self.kwargs)
        self.mock = first.start()
        self._patchers = [first]
        for mod in holders[1:]:
            extra = patch.object(mod, self.name, new=self.mock)
            extra.start()
            self._patchers.append(extra)
        return self.mock

    def stop(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._patchers = []

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
