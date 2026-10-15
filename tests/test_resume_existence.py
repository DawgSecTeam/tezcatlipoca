"""Resume existence gates (C1) — scale8 soak 2026-10-02.

Attempts 9-11 of that soak resumed at `--from-phase 5/6` after a phase-4 death. Apply
#2 had never run, so no team box existed; the phase-6 pre-domain pass is `strict=False`,
so it "succeeded" against 32 nonexistent machines and wrote `last_phase=5`. The next
resume read that stamp as proof the machines were there, every downstream wait raced a
ghost, and the run spent ~40 minutes in `wait_for_windows_sshd` before anyone worked out
why. `guard_resume_from_phase` could not catch it: it reads the checkpoint, and the
checkpoint was the lie.

These tests pin the two answers that fix it:
  - `estate.state.team_vmids` reads the one artifact that records what was built (the
    resource inventory), and refuses to report "nothing there" when it could not read it;
  - `guard_resume_existence` refuses a resume whose skipped phases left nothing behind,
    and names the phase to actually use.

Offline: no Proxmox, no network — temp competition dirs and a fake API only.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
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

# The real team_box address keys are BOX NAMES, not vmids — the vmid lives in the
# instance attributes (live-found 2026-10-03: an address-shape parser read a fully
# built range as "no team boxes at all" and failed the phase-4 checkpoint).


class InventoryReading(unittest.TestCase):
# The resume gate's "is the work there" probe reads the recorded inventory.


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

    def test_an_entry_without_a_vmid_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d, [("team1-web01", 221), ("team1-db01", None)])
            self.assertEqual(estate_state.team_vmids(comp), [221])

    def test_a_corrupt_document_raises_instead_of_reporting_nothing(self):
        # The distinction the whole gate rests on: "no machines" and "could not ask" must
        # not look alike, or the gate hard-fails a healthy range.
        with tempfile.TemporaryDirectory() as d:
            comp = self._comp(d, [("team1-web01", 221)])
            estate_state.state_path(comp).write_text("{not json")
            with self.assertRaises(SystemExit) as raised:
                estate_state.team_vmids(comp)
        self.assertIn("corrupt", str(raised.exception))

    def test_no_document_is_an_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d) / "competitions" / "c1"
            comp.mkdir(parents=True)
            self.assertEqual(estate_state.team_vmids(comp), [])


class ResumeExistenceGuard(unittest.TestCase):
# The gate itself, with every probe injected (no Proxmox).

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

    def test_early_resume_needs_nothing(self):
        # --from-phase 2 rebuilds the engine itself; there is nothing to verify.
        self._guard(2, vmids=(), live=())

    def test_resuming_past_the_engine_requires_a_live_engine(self):
        with self.assertRaises(SystemExit) as raised:
            self._guard(3, vmids=(), live=())
        self.assertIn("1900", str(raised.exception))
        self.assertIn("--from-phase 2", str(raised.exception))

    def test_engine_present_but_boxes_missing_is_refused(self):
        # The soak's exact shape: 32 machines recorded, none of them real.
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
        # Partial infrastructure is not a resume target: the phases downstream address
        # every box by computed vmid.
        with self.assertRaises(SystemExit):
            self._guard(5, vmids=(221, 222, 225), live=(1900, 221, 222))

    def test_boxes_present_allows_phase_5(self):
        self._guard(5)

    def test_phase_6_resume_is_accepted_without_a_sweep_marker(self):
        # Regression: .postclone-swept is written only at the end of phase 6, so a phase-6
        # failure leaves it absent. The old live-box probe refused the --from-phase 6 the
        # failure hint tells the operator to run; the engine and boxes are the real check.
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

    def test_force_from_phase_still_escalates(self):
        # The operator escape hatch survives: what C1 removes is the SILENT skip.
        self._guard(6, vmids=(), live=(), force=True)

    def test_probe_defaults_come_from_the_artifact_probe_seam(self):
        # The seam that makes this testable must be the one production uses.
        calls = []
        with dpatch("_artifact_probes", lambda comp_dir: {
                "vmids_from_state": lambda: calls.append("state") or [221],
                "vm_exists": lambda: calls.append("live") or {1900, 221}}):
            dl_gates.guard_resume_existence(5, self.comp, self._state())
        self.assertEqual(calls, ["live", "state"])


if __name__ == "__main__":
    unittest.main()
