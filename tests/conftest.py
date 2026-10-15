"""Test-process isolation for the one deliberate process-global: `estate.config`.

The config is a process singleton on purpose — multi-node activation points the whole
process at the engine's host and stays there for the deploy — but tests share one process,
so an activation in one test would silently re-target every later one. This resets it
around each test. It is the file's only fixture: the suite is otherwise standalone
`unittest` classes with no shared state.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _isolated_config():
    from estate import config
    config.reset()
    yield
    config.reset()
