"""dpatch: patch one name in every deploy_lib/infra module (and the `deploy` facade) that binds it.

The deploy pipeline is split across many small modules, each of which imports the names it
uses (`from infra.ownership import destroy_vm_if_exists`), so a test cannot know -- and should
not care -- which module's binding the code under test resolves. dpatch("destroy_vm_if_exists")
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

# Every package a deploy phase can bind a step name from. `estate`, `guests`, `engine`,
# `golden`, `nakonops`, `multinode` and `net` hold most of the real implementations now; walking
# only deploy_lib/infra silently missed them.
_PACKAGES = ("deploy_lib", "infra", "estate", "golden", "engine", "guests", "nakonops",
             "multinode", "net", "red", "portal")


def _modules():
    mods = [importlib.import_module("deploy")]
    for name in _PACKAGES:
        pkg = importlib.import_module(name)
        for info in pkgutil.walk_packages(pkg.__path__, f"{pkg.__name__}."):
            mods.append(importlib.import_module(info.name))
        mods.append(pkg)
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


class FakeNakonResult:
    """The shape a nakon run returns: `.failed` lines plus per-machine failed configs.

    `record_stage` reads `failed_configs()`, so a bare SimpleNamespace/MagicMock fake makes
    the coverage bookkeeping raise (or silently record nonsense).
    """

    def __init__(self, failed=(), failed_configs_map=None):
        self.failed = list(failed)
        self.machines = []
        self._failed_configs = dict(failed_configs_map or {})

    def failed_configs(self):
        return dict(self._failed_configs)


def fake_nakon_result(failed=(), failed_configs_map=None):
    return FakeNakonResult(failed, failed_configs_map)
