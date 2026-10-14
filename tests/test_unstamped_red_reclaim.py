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


class ReclaimUnstampedRed(unittest.TestCase):
    def _run(self, vms, vmid=999, placement=None):
        calls = []
        with patch("infra.client.proxmox_api", return_value={"data": vms}), \
                patch("infra.ownership.destroy_vm_if_exists",
                      side_effect=lambda node, v: calls.append((node, v))), \
                patch.object(ts, "read_placement", return_value=placement), \
                patch.object(ts, "activate_placement", lambda p: None), \
                patch.object(ts, "log"):
            result = ts._reclaim_unstamped_red(vmid, "/comp")
        return result, calls

    def test_reclaims_a_bad_auto_red01_in_the_reserved_slot(self):
        ok, calls = self._run([_vm()])
        self.assertTrue(ok)
        self.assertEqual(calls, [("proxmox", 999)])

    def test_refuses_a_vmid_that_is_not_a_red01(self):
        ok, calls = self._run([_vm(name="team1-web01")])
        self.assertFalse(ok)
        self.assertEqual(calls, [], "a non-red01 VM must never be destroyed here")

    def test_vmid_absent_is_not_an_error(self):
        ok, calls = self._run([_vm(vmid=1000)])
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_a_failing_api_call_never_raises(self):
        with patch("infra.client.proxmox_api", side_effect=RuntimeError("api down")), \
                patch.object(ts, "read_placement", return_value=None), \
                patch.object(ts, "log"):
            self.assertFalse(ts._reclaim_unstamped_red(999, "/comp"))


class TeardownRedUsesTheReclaim(unittest.TestCase):
    def test_still_present_triggers_the_reclaim_before_failing(self):
        """The proof must be re-run after the reclaim: a teardown that reclaims and still finds
        the VM must fail loudly (the message now says so)."""
        src = (_REPO / "scrim" / "teardown_stage.py").read_text()
        self.assertIn("_reclaim_unstamped_red(vmid, comp_path)", src)
        self.assertIn("EVEN AFTER the unstamped-red01 reclaim", src)


if __name__ == "__main__":
    unittest.main()
