"""Test-process isolation for the two deliberate process-globals: `estate.config` and the `TEZ_*` environment."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Locks live under ~/.tezcatlipoca/locks and would contend with a REAL deploy on a
# shared ops host; point home at a temp dir instead.
_HOME = tempfile.mkdtemp(prefix="tez-tests-home-")
Path.home = classmethod(lambda cls: Path(_HOME))

# Must capture before any test module imports: load_dotenv() at module level writes the
# live identity during collection, before fixtures exist. TEZ_TEST_OFFLINE makes those
# loaders skip .env; the ambient TEZ_* set is stripped so no test inherits a live estate.
_AMBIENT = {k: v for k, v in os.environ.items() if k.startswith("TEZ_")}
os.environ["TEZ_TEST_OFFLINE"] = "1"
for _key in [k for k in os.environ if k.startswith("TEZ_")]:
    del os.environ[_key]


@pytest.fixture(autouse=True)
def _isolated_config():
    from estate import config
    config.reset()
    yield
    for key in [k for k in os.environ
                if k.startswith("TEZ_") and k != "TEZ_TEST_OFFLINE"]:
        del os.environ[key]
    config.reset()
