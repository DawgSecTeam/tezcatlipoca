"""The team /24 arrives as a subnet route on the engine; a participant that enrolls without
--accept-routes sees the router in the netmap but reaches nothing (validated on the headscale
VPS: `tailscale ping 192.168.107.1` -> "no matching peer", then TCP worked after the flag)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from net.remote_access_ops import enrollment_markdown


def test_the_enrollment_command_accepts_subnet_routes():
    md = enrollment_markdown("cde-2026", "https://headscale.example",
                             [{"person": "alice", "team": "team1", "key": "abc"}])
    assert "tailscale up --login-server https://headscale.example --authkey abc --accept-routes" in md
