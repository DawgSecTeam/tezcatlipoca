"""A red01 deployed by a run that never stamped must still be reclaimable at teardown.

Regression for the 2026-10-14 e2e finding: stage_red died reading `<deploy.ssh_key>.pub` AFTER
bad-auto had created red01, so no deploy stamp was ever written. `badauto destroy` then exited 0
having removed nothing (its identity guard is the stamp), the teardown aborted with
"red01 is STILL PRESENT", and the range was left with a red box holding the LLM key and its
beacon tasking — the same leak class the scale8 soak hit with red01 998.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from scrim import teardown_stage as ts  # noqa: E402


def _vm(vmid=999, name="bad-auto-red01", node="proxmox"):
    return {"vmid": vmid, "name": name, "node": node}


# The narrow-reclaim cases live in SharedReclaimHelper below: one implementation
# (infra/red01.reclaim_unstamped_red) now serves both teardown paths.


class TeardownRedUsesTheReclaim(unittest.TestCase):
    def test_still_present_triggers_the_reclaim_before_failing(self):
        """The proof must be re-run after the reclaim: a teardown that reclaims and still finds
        the VM must fail loudly (the message now says so)."""
        src = (_REPO / "scrim" / "teardown_stage.py").read_text()
        self.assertIn("_reclaim_unstamped_red(vmid, comp_path)", src)
        self.assertIn("EVEN AFTER the unstamped-red01 reclaim", src)


class SharedReclaimHelper(unittest.TestCase):
    """One implementation (infra/red01.reclaim_unstamped_red) serves both teardown paths."""

    def _helper(self, vm, marker=None, comp_name="scrim-one"):
        from infra import red01
        killed = []
        with patch("infra.red01.vm_at", side_effect=[vm, None] if vm else [None]), \
                patch("infra.red01._marker_of", return_value=marker), \
                patch("infra.ownership.destroy_vm_if_exists",
                      side_effect=lambda node, v: killed.append((node, v))), \
                patch("infra.red01.print", create=True):
            ok = red01.reclaim_unstamped_red(999, comp_name=comp_name, log=lambda *a, **k: None)
        return ok, killed

    def test_unstamped_bad_auto_vm_is_reclaimed_and_verified_gone(self):
        ok, killed = self._helper({"vmid": 999, "node": "proxmox", "name": "bad-auto-red01"})
        self.assertTrue(ok)
        self.assertEqual(killed, [("proxmox", 999)])

    def test_another_competitions_red01_is_never_touched(self):
        ok, killed = self._helper({"vmid": 999, "node": "proxmox", "name": "bad-auto-red01"},
                                  marker="other-comp")
        self.assertFalse(ok)
        self.assertEqual(killed, [])

    def test_its_own_marked_red01_is_reclaimed(self):
        ok, killed = self._helper({"vmid": 999, "node": "proxmox", "name": "bad-auto-red01"},
                                  marker="scrim-one")
        self.assertTrue(ok)
        self.assertEqual(killed, [("proxmox", 999)])

    def test_a_non_red01_name_is_refused(self):
        ok, killed = self._helper({"vmid": 999, "node": "proxmox", "name": "team1-web01"})
        self.assertFalse(ok)
        self.assertEqual(killed, [])


class StandaloneTeardownFailsOnALeak(unittest.TestCase):
    def test_a_refused_refusal_reclaim_that_fails_is_now_a_FAILED_step(self):
        """'warned' hid the leak until the verification; it must be a failure."""
        src = (Path(__file__).resolve().parents[1] / "teardown" / "steps.py").read_text()
        self.assertIn("reclaim_unstamped_red(vmid", src)
        self.assertIn('StepResult(self.name, "failed"', src)


if __name__ == "__main__":
    unittest.main()
