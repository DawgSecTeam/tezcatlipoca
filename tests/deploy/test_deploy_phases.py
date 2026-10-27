"""deploy() phase-boundary defects, offline: golden hash, resume guard, coverage persistence."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from deploy_lib import estate_inputs as dl_estate_inputs
from guests import converge_ops as dl_coverage  # noqa: E402
from infra import gates as dl_gates  # noqa: E402
from deploy_lib import golden_plan as dl_golden_plan  # noqa: E402
from nakonops.nakon_run_ops import NakonResult
from engine.template_freeze import hash_from_inputs
from multinode.placement_record import golden_vmid_for_slot

ENGINE = 1000
MANAGED_BOXES = [{"name": "dc01", "template": "base-windows-server"},
                 {"name": "web01", "template": "base-ubuntu24.04-fix"}]
UNMANAGED = {"name": "fw01", "template": "pfsense-fix", "unmanaged": True}
BASE_IDS = {"base-windows-server": 9000, "base-ubuntu24.04-fix": 9001, "pfsense-fix": 9300}


def _bundle(tmp):
    # Empty manifest: golden_payload_hash returns None, yet the hash must still be produced.
    d = Path(tmp) / "bundle"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"inventory": [], "plans": {}}))
    return d


class GoldenHashEntries(unittest.TestCase):
    # Feeds phase 1's waves and the phase-4 rebuild gate: must cover EVERY box name.

    def _entries(self, boxes, unbooted=(), golden_machines=None):
        with tempfile.TemporaryDirectory() as d:
            bundle = _bundle(d)
            return dl_golden_plan.golden_hash_entries(
                boxes, BASE_IDS, golden_machines or {}, bundle,
                "boxpw", "medic", "ssh-rsa AAAA", True, unbooted=unbooted)

    def test_every_box_gets_a_hash_entry_including_unmanaged(self):
        boxes = MANAGED_BOXES + [UNMANAGED]
        inputs, hashes = self._entries(
            boxes, golden_machines={"dc01": {"configurations": []},
                                    "web01": {"configurations": []}})
        # Skipping unmanaged fw01 KeyErrors the phase-4 gate on golden_hashes["fw01"].
        self.assertEqual(set(hashes), {"dc01", "web01", "fw01"})
        self.assertEqual(set(inputs), {"dc01", "web01", "fw01"})
        self.assertEqual(inputs["fw01"]["config"]["golden"], "unmanaged")
        self.assertEqual(hashes["fw01"], hash_from_inputs(inputs["fw01"]))




class GoldenRebuildGate(unittest.TestCase):
    def setUp(self):
        self.boxes = MANAGED_BOXES + [UNMANAGED]
        self.destroyed = []
        self.stamp = Mock(return_value=False)
        self._patches = [
            patch.object(dl_golden_plan, "_is_template",
                         lambda node, vid: True),
            patch.object(dl_golden_plan, "template_stamp_current", self.stamp),
            patch.object(dl_golden_plan, "frozen_gate",
                         lambda *a, **k: True),
            patch.object(dl_golden_plan, "destroy_vm_if_exists",
                         lambda node, vid, **kw: self.destroyed.append(vid)),
        ]
        for p_ in self._patches:
            p_.start()
            self.addCleanup(p_.stop)

    def _slot(self, i):
        return golden_vmid_for_slot(ENGINE, 0, i)


    def test_matching_stored_hash_is_left_alone(self):
        hashes = {"dc01": "h-dc", "web01": "h-web", "fw01": "h-fw"}
        inputs = {"dc01": {"config": {}}, "web01": {"config": {}}, "fw01": {"config": {}}}
        # Reuse is decided by the node's stamp vs CURRENT inputs; the stored
        # hash record is never consulted.
        self.stamp.side_effect = lambda node, vid, expected: vid == self._slot(0)
        with contextlib.redirect_stdout(io.StringIO()):
            dl_golden_plan.golden_rebuild_gate(".", "node1", 0, self.boxes, ENGINE, {}, hashes,
                                       inputs, "c1")
        self.assertNotIn(self._slot(0), self.destroyed)
        self.assertIn(self._slot(1), self.destroyed)



class ResumeFromPhaseGuard(unittest.TestCase):
    def test_next_phase_after_last_completed_is_allowed(self):
        dl_gates.guard_resume_from_phase(4, 3, "estate-state.json")


    def test_far_beyond_last_completed_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            dl_gates.guard_resume_from_phase(6, 3, "estate-state.json")
        msg = str(ctx.exception)
        self.assertIn("--from-phase 6", msg)
        self.assertIn("records phase 3", msg)
        self.assertIn("--from-phase 4", msg)
        self.assertIn("--force-from-phase", msg)

    def test_state_without_last_phase_is_treated_as_zero(self):
        # Missing last_phase reads as 0: refuse rather than assume anything completed.
        with self.assertRaises(SystemExit) as ctx:
            dl_gates.guard_resume_from_phase(2, None, "estate-state.json")
        self.assertIn("records phase 0", str(ctx.exception))




class CoveragePersistence(unittest.TestCase):
    def _machines(self):
        return [{"name": "web01-team101",
                 "configurations": [{"name": "systemd-system-masked"}]}]

    def _green_result(self):
        return NakonResult([], [{"name": "web01-team101", "steps": []}])

    def test_cleared_coverage_entry_is_persisted(self):
        state = {"nakon_failed_steps": [],
                 "plant_coverage_failed": {"web01-team101": ["smb-v1"]}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "estate-state.json"
            dl_coverage.record_stage(
                state, self._machines(), self._green_result(),
                lambda: path.write_text(json.dumps(state)), "final")
            self.assertEqual(state.get("plant_coverage_failed"), {})
            on_disk = json.loads(path.read_text())
            self.assertEqual(on_disk.get("plant_coverage_failed"), {})




SECRET_WRITES = [
    ("teams.json", '{"team1": {"identifier": "120", "password": "hunter2"}}'),
    ("estate-state.json",
     '{"secrets": {"box_password": "hunter2", "teams": {}}, "identity": {}}'),
    ("credentials.txt", "team1 / hunter2\nbox-credlist-linux-admin  hunter2\n"),
]


class AtomicSecretWrites(unittest.TestCase):
    # Secret writes must get 0600 at CREATION (os.open), not by chmod after the umask
    # already exposed the file; the temp must not survive the rename.

    def test_mode_0600_at_creation_and_no_tmp_survives(self):
        for name, text in SECRET_WRITES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                seen = {}
                real_replace = os.replace

                def spy(src, dst):
                    # The swap instant is what a round-trip test cannot see: the temp must
                    # already hold the complete content at 0600.
                    seen["tmp_mode"] = stat.S_IMODE(Path(src).stat().st_mode)
                    seen["tmp_text"] = Path(src).read_text()
                    return real_replace(src, dst)

                with patch("os.replace", side_effect=spy):
                    dl_estate_inputs.write_text_atomic(path, text)

                self.assertEqual(seen["tmp_mode"], 0o600)
                self.assertEqual(seen["tmp_text"], text)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                # No .tmp left behind (the signature of an aborted/torn write).
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [name])

if __name__ == "__main__":
    unittest.main()
