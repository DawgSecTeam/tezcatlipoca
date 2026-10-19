"""Resume existence gates: team_vmids reads the resource inventory; guard_resume_existence refuses a resume whose skipped phases left nothing built."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from _deploy_patch import dpatch  # noqa: E402
from infra import gates as dl_gates  # noqa: E402
from estate import state as estate_state  # noqa: E402

STATE_RESOURCES = [
    {"type": "proxmox_virtual_environment_vm", "name": "scoring_engine",
     "instances": [{"attributes": {"id": "200", "vm_id": 200}}]},
    {"type": "proxmox_virtual_environment_vm", "name": "team_box",
     "instances": [{"attributes": {"id": "221", "vm_id": 221}},
                   {"attributes": {"id": "222", "vm_id": 222}}]},
    {"type": "proxmox_virtual_environment_vm", "name": "team_box_sat1",
     "instances": [{"attributes": {"id": "225", "vm_id": 225}}]},
    {"type": "null_resource", "name": "team_nics",
     "instances": [{"attributes": {"id": "221"}}]},
    {"type": "proxmox_network_linux_bridge", "name": "team_bridge",
     "instances": [{"attributes": {"id": "221"}}]},
]
STATE_JSON = json.dumps({"version": 4, "serial": 14, "resources": STATE_RESOURCES})

# team_box address keys are box names, not vmids; the vmid lives in instance attributes.


class InventoryReading(unittest.TestCase):
    def _comp(self, tmp, rows=()):
        comp = Path(tmp) / "competitions" / "c1"
        comp.mkdir(parents=True)
        document = estate_state.EstateState.load(comp)
        for name, vmid in rows:
            document.record("team_box", "pve", name, vmid=vmid)
        document.record("bridge", "pve", "vmbr101")
        document.save()
        return comp

    def test_reads_every_recorded_box_including_satellites(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d, [("team1-web01", 221), ("team1-db01", 222),
                                  ("130-web01", 225)])
            self.assertEqual(estate_state.team_vmids(comp), [221, 222, 225])


    def test_a_corrupt_document_raises_instead_of_reporting_nothing(self):
        # "no machines" and "could not ask" must not look alike, or the gate hard-fails a healthy range.
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d, [("team1-web01", 221)])
            estate_state.state_path(comp).write_text("{not json")
            with self.assertRaises(SystemExit) as raised:
                estate_state.team_vmids(comp)
        self.assertIn("corrupt", str(raised.exception))



class ResumeExistenceGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.comp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _state(self, **kw):
        return {"scoring_vm_id": 1900, **kw}

    def _guard(self, from_phase, *, vmids=(221, 222), live=(221, 222, 1900),
               state=None, **kw):
        dl_gates.guard_resume_existence(
            from_phase, self.comp, self._state(**(state or {})),
            vmids_from_state=lambda: list(vmids),
            vm_exists=lambda: set(live),
            **kw)


    def test_resuming_past_the_engine_requires_a_live_engine(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(3, vmids=(), live=())
        self.assertIn("1900", str(raised.exception))
        self.assertIn("--from-phase 2", str(raised.exception))

    def test_engine_present_but_boxes_missing_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(5, vmids=(221, 222), live=(1900,))
        msg = str(raised.exception)
        self.assertIn("2 of 2", msg)
        self.assertIn("--from-phase 4", msg)

    def test_no_boxes_recorded_at_all_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(5, vmids=(), live=(1900,))
        self.assertIn("no team boxes at all", str(raised.exception))

    def test_a_few_missing_boxes_still_refuses(self):
        # Partial infra is not a resume target: downstream phases address every box by computed vmid.
        with self.assertRaises(SystemExit):
            self._guard(5, vmids=(221, 222, 225), live=(1900, 221, 222))


    def test_phase_6_resume_is_accepted_without_a_sweep_marker(self):
        # Regression: .postclone-swept lands only at the end of phase 6, so a phase-6
        # failure must still accept the --from-phase 6 its own hint prints.
        self.assertFalse((self.comp / ".postclone-swept").exists())
        self._guard(6)

    def test_unreadable_state_refuses_rather_than_resuming_blind(self):
        def boom():
            raise RuntimeError("could not read the estate inventory")
        with self.assertRaises(SystemExit) as raised:
            dl_gates.guard_resume_existence(
                5, self.comp, self._state(), vmids_from_state=boom,
                vm_exists=lambda: {1900})
        self.assertIn("cannot be verified", str(raised.exception))

    def test_unreachable_api_refuses_rather_than_calling_everything_gone(self):
        def boom():
            raise RuntimeError("could not list the cluster's VMs")
        with self.assertRaises(SystemExit) as raised:
            dl_gates.guard_resume_existence(
                3, self.comp, self._state(), vmids_from_state=lambda: [],
                vm_exists=boom)
        self.assertIn("Refusing to resume blind", str(raised.exception))




if __name__ == "__main__":
    unittest.main()
