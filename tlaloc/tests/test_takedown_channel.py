"""A Windows takedown whose SSH login is closed must use the live C2 channel: the
AD DS promotion can disable the built-in Administrator, and the DC's takedowns
then all failed as TransportError while the channel was up (live: cde-2026)."""

from tests.helpers import FakeSSH, make_ctx
from tlaloc.c2 import FakeMythic
from tlaloc.effects import execute


def test_a_windows_takedown_falls_back_to_the_channel(tmp_path):
    ssh = FakeSSH().on(lambda c: c.get("windows") is True, rc="raise")
    ctx = make_ctx(tmp_path / "state", ssh=ssh)
    ctx.world.add_foothold("192.168.1.10", "Administrator", "pw", "root",
                           windows=True)
    ctx.c2 = FakeMythic({"192.168.1.10": {"seen_at": ctx.now(), "agent": "apollo"}},
                        task_output="True")
    result = execute("takedown", {"ip": "192.168.1.10", "service": "smb",
                                  "mode": "stop"}, ctx)
    assert result.ok, result.detail
    assert "via C2" in result.detail
    assert any("tlaloc-impact" in t["command"] for t in ctx.c2.tasks)
