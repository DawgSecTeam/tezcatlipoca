"""Test-process isolation for the two deliberate process-globals: `estate.config` and the
`TEZ_*` environment a driver's `main()` loads out of the checkout's `.env`.

The config is a process singleton on purpose — multi-node activation points the whole
process at the engine's host and stays there for the deploy — but tests share one process,
so an activation in one test would silently re-target every later one. This resets it
around each test.

`load_dotenv` writes the real estate identity into `os.environ` and never removes it, so a
test that drives a driver's `main()` handed every later test the LIVE endpoint: an "offline"
suite made real HTTPS requests to the production host (measured 2026-10-16: 13 requests,
and the affected modules ran 100x slower than when truly offline). The ambient TEZ_* set is
restored around each test for the same reason as the config.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Every cross-process lock the pipeline takes (engine, scrim, storage queue) lives under
# Path.home()/.tezcatlipoca/locks. A test that exercises a locked path would otherwise contend
# with, or be blocked by, a REAL deploy on the same host — and on a shared ops host that is a
# live run. Nothing in the suite needs the developer's own lock directory.
_HOME = tempfile.mkdtemp(prefix="tez-tests-home-")
Path.home = classmethod(lambda cls: Path(_HOME))

# Captured before any test module is imported: driver scripts call load_dotenv() at module
# level, so importing one for a CLI test writes the real identity into os.environ during
# collection — before any fixture runs and too early to be seen as non-ambient.
_AMBIENT = {k: v for k, v in os.environ.items() if k.startswith("TEZ_")}


@pytest.fixture(autouse=True)
def _isolated_config():
    from estate import config
    config.reset()
    yield
    for key in [k for k in os.environ if k.startswith("TEZ_") and k not in _AMBIENT]:
        del os.environ[key]
    os.environ.update(_AMBIENT)
    config.reset()
