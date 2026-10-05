"""verifier.firewall (standing in-path gate) and the automatic clean-comp misconfig SKIP."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verifier.context as v_context  # noqa: E402
import verifier.firewall as v_firewall  # noqa: E402
import verifier.misconfig as v_misconfig  # noqa: E402
from verifier.model import CheckError, Status  # noqa: E402

TEAMS = {"team1": {"identifier": 141}, "team2": {"identifier": 142}}
FW_BOX = {"name": "fw01", "template": "pfsense", "unmanaged": True, "in_path": True}


def _proc(rc=0, out=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


def _engine(held=(), routed=("141", "142"), alive=("141", "142"), exc=None):
    """Fake ssh_to_engine modelling a cut-over engine, with per-team drift knobs."""
    def fake(ctx, cmd, timeout=30):
        if exc:
            raise exc
        if cmd.startswith("ip route get"):
            tid = cmd.split(".")[2]
            return _proc(0, f"192.168.{tid}.1 via 172.31.{tid}.2 dev ens22\n" if tid in routed
                         else f"192.168.{tid}.1 dev ens19 src 192.168.{tid}.1\n")
        if "/dev/tcp/" in cmd:
            tid = cmd.split("172.31.")[1].split(".")[0]
            return _proc(0, "UP\n" if tid in alive else "")
        if cmd.startswith("ip -4"):
            return _proc(0, "".join(f"3: ens19 inet 192.168.{t}.1/24 scope global\n" for t in held))
        raise AssertionError(cmd)
    return fake


def _comp(boxes):
    d = Path(tempfile.mkdtemp())
    (d / "boxes.json").write_text(json.dumps(boxes))
    return d


class FirewallGateTests(unittest.TestCase):
    def _gate(self, comp, **kw):
        with patch.object(v_context, "ssh_to_engine", _engine(**kw)):
            return v_firewall.check_firewall_in_path({}, comp, TEAMS)

    def test_skip_without_in_path_firewall(self):
        g = self._gate(_comp([{"name": "web01", "template": "x"}]))
        self.assertIs(g.status, Status.SKIP_UNAVAILABLE)
        self.assertFalse(g.gating)

    def test_pass_when_cut_over(self):
        self.assertTrue(self._gate(_comp([FW_BOX])).passed)

    def test_fail_when_engine_routes_directly(self):
        g = self._gate(_comp([FW_BOX]), routed=("141",))
        self.assertIs(g.status, Status.FAIL)
        self.assertIn("team 142", g.detail)

    def test_fail_when_firewall_does_not_answer(self):
        g = self._gate(_comp([FW_BOX]), alive=("142",))
        self.assertIs(g.status, Status.FAIL)
        self.assertIn("172.31.141.2", g.detail)

    def test_fail_when_engine_regained_the_gateway(self):
        g = self._gate(_comp([FW_BOX]), held=("141",))
        self.assertIs(g.status, Status.FAIL)
        self.assertIn("192.168.141.1", g.detail)

    def test_unreachable_engine_is_skip_not_pass(self):
        g = self._gate(_comp([FW_BOX]), exc=CheckError("no ssh"))
        self.assertIs(g.status, Status.SKIP_UNAVAILABLE)
        g = self._gate(_comp([FW_BOX]), exc=subprocess.TimeoutExpired("ssh", 30))
        self.assertIs(g.status, Status.SKIP_UNAVAILABLE)


class CleanCompTests(unittest.TestCase):
    def _dir(self, vulns):
        d = Path(tempfile.mkdtemp())
        if vulns is not None:
            (d / "box_vulns.json").write_text(json.dumps(vulns))
        return d

    def test_empty_vulns_and_no_machine_misconfigs_is_clean(self):
        boxes = [{"name": "web01-team1", "ip": "1.1.1.1", "configurations": ["apache"]}]
        self.assertTrue(v_misconfig.comp_is_clean(self._dir({"web01": []}), boxes))

    def test_pinned_vulns_are_never_clean(self):
        self.assertFalse(v_misconfig.comp_is_clean(self._dir({"web01": ["suid-find"]}), []))

    def test_missing_box_vulns_is_not_clean(self):
        self.assertFalse(v_misconfig.comp_is_clean(self._dir(None), []))

    def test_windows_only_pins_are_unverifiable_not_failing(self):
        boxes = [{"name": "dc01-team1", "ip": "1.1.1.1",
                  "configurations": ["local-user-win", "weak-password-policy-win"]}]
        vulns = {"dc01": [{"name": "local-user-win"}, "weak-password-policy-win"]}
        self.assertTrue(v_misconfig.misconfigs_unverifiable(self._dir(vulns), boxes))

    def test_pin_that_never_reached_a_machine_stays_a_fail(self):
        boxes = [{"name": "dc01-team1", "ip": "1.1.1.1", "configurations": []}]
        self.assertFalse(v_misconfig.misconfigs_unverifiable(
            self._dir({"dc01": ["local-user-win"]}), boxes))

    def test_a_verifiable_pin_is_checked_not_skipped(self):
        boxes = [{"name": "w", "ip": "1.1.1.1", "configurations": ["suid-find"]}]
        self.assertFalse(v_misconfig.misconfigs_unverifiable(
            self._dir({"web01": ["suid-find"]}), boxes))

    def test_machine_carrying_a_misconfig_is_not_clean(self):
        boxes = [{"name": "w", "ip": "1.1.1.1", "configurations": ["suid-find"]}]
        self.assertFalse(v_misconfig.comp_is_clean(self._dir({"web01": []}), boxes))


if __name__ == "__main__":
    unittest.main()
