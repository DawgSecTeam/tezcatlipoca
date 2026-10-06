"""One preflight implementation: single-node is a one-share plan, multi-node is N shares, and
both classify "ours" through vm_ownership.ownership_verdict (the same proof destroy uses)."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import vm_ownership  # noqa: E402
from preflight.clashes import check_collisions, expected_bridges, expected_slots  # noqa: E402
from preflight.plan import NodeShare, PreflightPlan  # noqa: E402

RUN = "run-abc123"
BOXES = [{"name": "web01", "template": "t"}, {"name": "db01", "template": "t"}]
TEAMS = {"team1": {"identifier": "101"}}


def _share(multi=False, slot=0, is_engine=True):
    return NodeShare(name="n1" if multi else "", node="pve", multi=multi, slot=slot,
                     is_engine=is_engine, teams=TEAMS, num_teams=1, datastore="ds",
                     fetch_vms=lambda: [])


def _plan(share, run=RUN):
    return PreflightPlan(Path("competitions/c1"), BOXES, 1000, [share], our_run_tag=run)


def _vm(vmid, tags):
    return {"vmid": vmid, "name": f"vm{vmid}", "tags": tags, "node": "pve"}


class OwnershipVerdict(unittest.TestCase):
    EXPECT = {"tezcatlipoca", "comp-c1", RUN}

    def test_verdicts(self):
        with patch.object(vm_ownership, "has_clone_marker", return_value=True):
            self.assertEqual(vm_ownership.ownership_verdict(
                "n", 1, f"tezcatlipoca;comp-c1;{RUN}", self.EXPECT)[0], vm_ownership.OWNED)
            self.assertEqual(vm_ownership.ownership_verdict(
                "n", 1, "", self.EXPECT)[0], vm_ownership.MARKER)
        with patch.object(vm_ownership, "has_clone_marker", return_value=False):
            self.assertEqual(vm_ownership.ownership_verdict(
                "n", 1, None, self.EXPECT)[0], vm_ownership.UNPROVEN)
        verdict, missing = vm_ownership.ownership_verdict(
            "n", 1, "tezcatlipoca,comp-c1", self.EXPECT)
        self.assertEqual((verdict, missing), (vm_ownership.MISSING, {RUN}))


class SlotMath(unittest.TestCase):
    def test_single_and_multi_slot0_cover_the_same_vmids(self):
        single = {s.vmid for s in expected_slots(_share(), BOXES, 1000)}
        multi = {s.vmid for s in expected_slots(_share(multi=True), BOXES, 1000)}
        self.assertEqual(single, multi)
        self.assertIn(1000, single)          # engine
        self.assertIn(1140, single)          # engine template
        self.assertIn(1150, single)          # golden 0
        self.assertIn(1210, single)          # team 101 box 0

    def test_satellite_slot_has_jump_vm_and_shifted_goldens(self):
        vmids = {s.vmid for s in expected_slots(_share(True, 1, False), BOXES, 1000)}
        self.assertIn(1131, vmids)
        self.assertIn(1160, vmids)
        self.assertNotIn(1000, vmids)

    def test_transit_bridges_only_on_slot_zero(self):
        fw = BOXES + [{"name": "fw", "template": "t", "in_path": True}]
        self.assertEqual(expected_bridges(_share(), fw), ["vmbr101", "vmbrW101"])
        self.assertEqual(expected_bridges(_share(True, 1, False), fw), ["vmbr101"])


class Collisions(unittest.TestCase):
    def _run(self, share, vms, bridges=()):
        nets = {"data": [{"iface": b} for b in bridges]}
        with patch("preflight.clashes.proxmox_api", return_value=nets), \
                patch.object(vm_ownership, "has_clone_marker", return_value=False):
            check_collisions(share, _plan(share), vms)

    def test_foreign_vm_refused_both_modes(self):
        for multi in (False, True):
            with self.assertRaises(SystemExit) as cm:
                self._run(_share(multi=multi), [_vm(1000, "other")])
            self.assertIn("scoring engine vmid 1000", str(cm.exception))

    def test_other_run_same_comp_is_called_out(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(_share(), [_vm(1000, "tezcatlipoca,comp-c1")])
        self.assertIn("ANOTHER worktree", str(cm.exception))

    def test_ours_passes_and_tolerates_own_bridge(self):
        self._run(_share(), [_vm(1000, f"tezcatlipoca;comp-c1;{RUN}")], bridges=["vmbr101"])

    def test_foreign_bridge_alone_is_fatal(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(_share(), [], bridges=["vmbr101"])
        self.assertIn("bridge vmbr101", str(cm.exception))

    def test_persistent_engine_template_is_not_a_leftover_in_single_mode(self):
        # ours ET alone must not license a pre-existing bridge (single-node semantics)
        with self.assertRaises(SystemExit):
            self._run(_share(), [_vm(1140, f"tezcatlipoca;comp-c1;{RUN}")], bridges=["vmbr101"])


class UnpinnedBoxWarning(unittest.TestCase):
    """A managed box with zero pins is advisory (printed), never a refusal."""

    def _comp(self, services, vulns, baseline=None):
        import json
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "box_services.json").write_text(json.dumps(services))
        (d / "box_vulns.json").write_text(json.dumps(vulns))
        if baseline is not None:
            (d / "box_baseline.json").write_text(json.dumps(baseline))
        return d

    def test_unpinned_managed_box_is_named_and_unmanaged_is_skipped(self):
        from preflight import pins
        boxes = BOXES + [{"name": "fw01", "unmanaged": True}]
        d = self._comp({"web01": [{"name": "nginx"}], "db01": []}, {"web01": [], "db01": []})
        self.assertEqual(pins.unpinned_managed_boxes(d, boxes), ["db01"])

    def test_any_pin_source_counts(self):
        from preflight import pins
        d = self._comp({"web01": [], "db01": []}, {"web01": [], "db01": [{"name": "v"}]},
                       baseline={"web01": [{"name": "b"}]})
        self.assertEqual(pins.unpinned_managed_boxes(d, BOXES), [])

    def test_gate_warns_without_refusing(self):
        import io
        from contextlib import redirect_stdout
        from preflight import pins
        d = self._comp({"web01": [], "db01": []}, {"web01": [], "db01": []})
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(pins.warn_unpinned_boxes(d, BOXES), ["web01", "db01"])
        self.assertIn("zero pins", buf.getvalue())
        self.assertIn("strict passes reconcile the no-op", buf.getvalue())

    def test_missing_pin_files_do_not_raise(self):
        import tempfile
        from preflight import pins
        self.assertEqual(pins.unpinned_managed_boxes(Path(tempfile.mkdtemp()), BOXES), [])


if __name__ == "__main__":
    os.environ.setdefault("TF_VAR_proxmox_node", "pve")
    unittest.main()
