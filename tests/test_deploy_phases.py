"""deploy() phase-boundary defects, offline: the M4 golden hash loop (every box must get
an entry, unmanaged included), the --from-phase resume guard against `last_phase`, and the
always-persist rule for phase 6's coverage repair."""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import config_ops
import deploy
from nakon_ops import NakonResult
from nodes_ops import golden_vmid_for_slot

ENGINE = 1000
MANAGED_BOXES = [{"name": "dc01", "template": "base-windows-server"},
                 {"name": "web01", "template": "base-ubuntu24.04-fix"}]
UNMANAGED = {"name": "fw01", "template": "pfsense-fix", "unmanaged": True}
BASE_IDS = {"base-windows-server": 9000, "base-ubuntu24.04-fix": 9001, "pfsense-fix": 9300}


def _bundle(tmp):
    """A nakon bundle dir whose manifest names no golden machines (golden_payload_hash
    returns None — the hash still has to be produced and stable)."""
    d = Path(tmp) / "bundle"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"inventory": [], "plans": {}}))
    return d


class GoldenHashEntries(unittest.TestCase):
    """D1: the loop that feeds phase 1's waves, the phase-4 rebuild gate and
    .template-hashes.json. It must cover EVERY box name in boxes.json."""

    def _entries(self, boxes, unbooted=(), golden_machines=None):
        with tempfile.TemporaryDirectory() as d:
            # golden_bundle is read through golden_payload_hash; keep it a real dir.
            bundle = _bundle(d)
            return deploy.golden_hash_entries(
                boxes, BASE_IDS, golden_machines or {}, bundle,
                "boxpw", "medic", "ssh-rsa AAAA", True, unbooted=unbooted)

    def test_every_box_gets_a_hash_entry_including_unmanaged(self):
        boxes = MANAGED_BOXES + [UNMANAGED]
        inputs, hashes = self._entries(
            boxes, golden_machines={"dc01": {"configurations": []},
                                    "web01": {"configurations": []}})
        # The pre-fix loop `continue`d on unmanaged before building its entry, so fw01
        # was missing here and the phase-4 gate KeyError'd on golden_hashes["fw01"].
        self.assertEqual(set(hashes), {"dc01", "web01", "fw01"})
        self.assertEqual(set(inputs), {"dc01", "web01", "fw01"})
        self.assertEqual(inputs["fw01"]["config"]["golden"], "unmanaged")
        self.assertEqual(hashes["fw01"], deploy.hash_from_inputs(inputs["fw01"]))

    def test_unmanaged_entry_is_stable_across_calls(self):
        # .template-hashes.json reuse depends on the value not drifting run to run.
        _i1, h1 = self._entries([UNMANAGED])
        _i2, h2 = self._entries([UNMANAGED])
        self.assertEqual(h1["fw01"], h2["fw01"])

    def test_unbooted_box_gets_an_entry_without_a_golden_machine(self):
        # The unbooted (domain-controller) branch must not consult
        # golden_machines_by_box — an unbooted box has no golden machine to key on.
        boxes = [MANAGED_BOXES[0]]
        inputs, hashes = self._entries(boxes, unbooted={"dc01"}, golden_machines={})
        self.assertEqual(inputs["dc01"]["config"]["golden"], "unbooted")
        self.assertIn("dc01", hashes)


class GoldenRebuildGate(unittest.TestCase):
    """D1's second half: the gate must SKIP a slot with no hash entry (the state the old
    dead-code loop left unmanaged boxes in) instead of raising KeyError."""

    def setUp(self):
        self.boxes = MANAGED_BOXES + [UNMANAGED]
        self.destroyed = []
        self._orig = (deploy._is_template, deploy.stored_template_hash,
                      deploy.frozen_gate, deploy.destroy_vm_if_exists)
        deploy._is_template = lambda node, vid: True       # every slot holds a template
        deploy.stored_template_hash = lambda node, vid: "stale-on-node"
        deploy.frozen_gate = lambda *a, **k: True          # rebuild permitted
        deploy.destroy_vm_if_exists = lambda node, vid, **kw: self.destroyed.append(vid)

    def tearDown(self):
        (deploy._is_template, deploy.stored_template_hash,
         deploy.frozen_gate, deploy.destroy_vm_if_exists) = self._orig

    def _slot(self, i):
        return golden_vmid_for_slot(ENGINE, 0, i)

    def test_slot_with_no_hash_entry_is_skipped_not_a_keyerror(self):
        # Exactly what the pre-fix loop produced: managed entries present, fw01 absent.
        # The old gate did golden_hashes["fw01"] unconditionally -> KeyError.
        hashes = {"dc01": "h-dc", "web01": "h-web"}
        inputs = {"dc01": {"config": {}}, "web01": {"config": {}}}
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.golden_rebuild_gate(".", "node1", 0, self.boxes, ENGINE, {}, hashes,
                                       inputs, "c1")
        self.assertEqual(self.destroyed, [self._slot(0), self._slot(1)])  # fw01 skipped

    def test_all_boxes_present_means_no_missing_entry_at_all(self):
        # End-to-end with the fixed producer: a lineup carrying an unmanaged box can run
        # the gate for a whole slot without a KeyError, and the unmanaged slot's stale
        # template is rebuilt like any other drifted golden.
        with tempfile.TemporaryDirectory() as d:
            bundle = _bundle(d)
            inputs, hashes = deploy.golden_hash_entries(
                self.boxes, BASE_IDS,
                {"dc01": {"configurations": []}, "web01": {"configurations": []}},
                bundle, "boxpw", "medic", "ssh-rsa AAAA", True)
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.golden_rebuild_gate(".", "node1", 0, self.boxes, ENGINE, {}, hashes,
                                       inputs, "c1")
        self.assertEqual(self.destroyed, [self._slot(i) for i in range(len(self.boxes))])

    def test_matching_stored_hash_is_left_alone(self):
        hashes = {"dc01": "h-dc", "web01": "h-web"}
        inputs = {"dc01": {"config": {}}, "web01": {"config": {}}}
        deploy.stored_template_hash = lambda node, vid: (
            "h-dc" if vid == self._slot(0) else "other")
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.golden_rebuild_gate(".", "node1", 0, self.boxes, ENGINE, {}, hashes,
                                       inputs, "c1")
        self.assertNotIn(self._slot(0), self.destroyed)   # hash matches -> kept
        self.assertIn(self._slot(1), self.destroyed)


class ResumeFromPhaseGuard(unittest.TestCase):
    """D2: --from-phase must not skip phases .deploy_state.json never saw complete."""

    def test_next_phase_after_last_completed_is_allowed(self):
        deploy.guard_resume_from_phase(4, 3, ".deploy_state.json")  # no raise

    def test_resuming_earlier_than_last_completed_is_allowed(self):
        deploy.guard_resume_from_phase(2, 6, ".deploy_state.json")

    def test_far_beyond_last_completed_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            deploy.guard_resume_from_phase(6, 3, ".deploy_state.json")
        msg = str(ctx.exception)
        self.assertIn("--from-phase 6", msg)          # names the requested phase
        self.assertIn("records phase 3", msg)         # and what actually completed
        self.assertIn("--from-phase 4", msg)          # and the safe target
        self.assertIn("--force-from-phase", msg)

    def test_state_without_last_phase_is_treated_as_zero(self):
        # Missing last_phase reads as 0 — nothing is recorded complete, so even
        # --from-phase 2 would skip phase 1. Refuse rather than assume.
        with self.assertRaises(SystemExit) as ctx:
            deploy.guard_resume_from_phase(2, None, ".deploy_state.json")
        self.assertIn("records phase 0", str(ctx.exception))

    def test_non_integer_last_phase_is_treated_as_zero(self):
        with self.assertRaises(SystemExit):
            deploy.guard_resume_from_phase(2, "three", ".deploy_state.json")

    def test_force_overrides_the_refusal(self):
        deploy.guard_resume_from_phase(6, 3, ".deploy_state.json", force=True)


class CoveragePersistence(unittest.TestCase):
    """D3: phase 6's coverage repair must reach disk even when the tally is empty."""

    def _machines(self):
        return [{"name": "web01-team101",
                 "configurations": [{"name": "systemd-system-masked"}]}]

    def _green_result(self):
        return NakonResult([], [{"name": "web01-team101", "steps": []}])

    def test_cleared_coverage_entry_is_persisted(self):
        state = {"nakon_failed_steps": [],
                 "plant_coverage_failed": {"web01-team101": ["smb-v1"]}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".deploy_state.json"
            deploy.record_stage_coverage(
                state, self._machines(), self._green_result(),
                lambda: path.write_text(json.dumps(state)))
            self.assertEqual(state.get("plant_coverage_failed"), {})   # in memory
            on_disk = json.loads(path.read_text())                     # and on disk
            self.assertEqual(on_disk.get("plant_coverage_failed"), {})

    def test_save_happens_even_when_there_was_nothing_to_clear(self):
        calls = []
        state = {"plant_coverage_failed": {}}
        deploy.record_stage_coverage(state, self._machines(), self._green_result(),
                                     lambda: calls.append(1))
        self.assertEqual(calls, [1])

    def test_failed_steps_are_still_recorded_and_saved(self):
        state = {"plant_coverage_failed": {}}
        result = NakonResult([], [{"name": "web01-team101",
                                   "steps": [{"name": "smb-v1", "rc": 1}]}])
        deploy.record_stage_coverage(state, self._machines(), result, lambda: None)
        self.assertEqual(state["plant_coverage_failed"],
                         {"web01-team101": ["smb-v1"]})


class SharedStateWriter(unittest.TestCase):
    """D4: deploy's .deploy_state.json writer is the shared config_ops helper, not a
    fourth hand-rolled atomic writer (the guarantee held at 1 of 4 call sites before)."""

    def test_deploy_writer_is_the_shared_config_ops_helper(self):
        self.assertIs(deploy.write_state, config_ops.write_state)

    def test_save_state_routes_through_the_helper(self):
        src = (_REPO / "deploy.py").read_text()
        self.assertIn("write_state(state_path, state)", src)
        # The old hand-rolled body must be gone: write_text() then chmod() left the
        # only copy of the box passwords world-readable for a window.
        self.assertNotIn("state_path.with_name", src)
        self.assertNotIn("os.chmod(state_path, 0o600)", src)


SECRET_WRITES = [
    ("teams.json", '{"team1": {"identifier": "120", "password": "hunter2"}}'),
    ("terraform.tfvars.json", '{"box_password": "hunter2", "teams": {}}'),
    ("credentials.txt", "team1 / hunter2\nbox-credlist-linux-admin  hunter2\n"),
]


class AtomicSecretWrites(unittest.TestCase):
    """Every secret-bearing write in deploy.py must go through
    config_ops.write_text_atomic: 0600 is applied at CREATION (os.open), not by a chmod
    after the process umask already exposed the file, and the content lands via a temp
    file that never survives the rename. These are the exact shapes deploy writes."""

    def test_deploy_uses_the_shared_atomic_write_helper(self):
        self.assertIs(deploy.write_text_atomic, config_ops.write_text_atomic)

    def test_mode_0600_at_creation_and_no_tmp_survives(self):
        for name, text in SECRET_WRITES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                seen = {}
                real_replace = os.replace

                def spy(src, dst):
                    # At the instant of the swap the temp already holds the COMPLETE
                    # content AND already carries 0600 — this is what a plain
                    # write_text-then-chmod round-trip test cannot see.
                    seen["tmp_mode"] = stat.S_IMODE(Path(src).stat().st_mode)
                    seen["tmp_text"] = Path(src).read_text()
                    return real_replace(src, dst)

                with patch("os.replace", side_effect=spy):
                    deploy.write_text_atomic(path, text)

                self.assertEqual(seen["tmp_mode"], 0o600)
                self.assertEqual(seen["tmp_text"], text)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                # No .tmp left behind (the signature of an aborted/torn write).
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [name])

    def test_deploy_has_no_hand_rolled_secret_writer_left(self):
        src = (_REPO / "deploy.py").read_text()
        self.assertNotIn("os.chmod", src)          # no chmod-after-write anywhere
        self.assertNotIn('"teams.json").write_text', src)
        self.assertNotIn("tfvars_path.write_text", src)
        self.assertNotIn("cred_path.write_text", src)


if __name__ == "__main__":
    unittest.main()
