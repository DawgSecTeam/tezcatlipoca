"""Shared pytest fixtures + the hermeticity guard.

The guard is the mechanical version of the plan's rule: "no test may spawn ssh
or network". It fails loud rather than relying on reviewers, because a test that
silently dials a range address is how the earlier agent's destroy tests looked
green while they really talked to the club LAN.
"""

import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers import DEFAULT_COMP, FakeSSH  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, request):
    """No process spawn, no socket, unless the test asks for `net`."""
    if "net" in request.keywords:
        return

    def boom(*args, **kwargs):
        raise AssertionError(
            "test attempted real I/O — use tests/helpers.py fakes, or mark the "
            "test with @pytest.mark.net to opt in")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


@pytest.fixture
def comp_dir():
    return DEFAULT_COMP


@pytest.fixture
def ssh():
    return FakeSSH()
