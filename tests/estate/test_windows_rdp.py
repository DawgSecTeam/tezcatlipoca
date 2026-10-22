"""Every CCDC blue team administers Windows over RDP. The bootstrap enabled the Remote Desktop
firewall rules but never RDP itself, so on a competition without an "RDP misconfig" plant the
scored Tcp dial — and a human's mstsc — was filtered (live: cde-2026 ad01/ftp01, 3389 closed)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

import inspect

from common import windows_ops


def test_the_bootstrap_enables_rdp_itself_and_its_rules():
    src = inspect.getsource(windows_ops.bootstrap_windows_box)
    assert "fDenyTSConnections -Value 0" in src
    assert "Set-Service -Name TermService" in src
    assert "RemoteDesktop-UserMode-In-TCP" in src
