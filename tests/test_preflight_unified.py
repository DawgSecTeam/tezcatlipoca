"""One preflight implementation: single-node is a one-share plan, multi-node is N shares, and
both classify "ours" through ownership.ownership_verdict (the same proof destroy uses)."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from infra import ownership  # noqa: E402
from infra.collisions import check_collisions, expected_bridges, expected_slots  # noqa: E402
from infra.plan import NodeShare, PreflightPlan  # noqa: E402

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
        with patch.object(ownership, "has_clone_marker", return_value=True):
            self.assertEqual(ownership.ownership_verdict(
                "n", 1, f"tezcatlipoca;comp-c1;{RUN}", self.EXPECT)[0], ownership.OWNED)
            self.assertEqual(ownership.ownership_verdict(
                "n", 1, "", self.EXPECT)[0], ownership.MARKER)
        with patch.object(ownership, "has_clone_marker", return_value=False):
            self.assertEqual(ownership.ownership_verdict(
                "n", 1, None, self.EXPECT)[0], ownership.UNPROVEN)
        verdict, missing = ownership.ownership_verdict(
            "n", 1, "tezcatlipoca,comp-c1", self.EXPECT)
        self.assertEqual((verdict, missing), (ownership.MISSING, {RUN}))


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
        with patch("infra.collisions.proxmox_api", return_value=nets), \
                patch.object(ownership, "has_clone_marker", return_value=False):
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

    def test_our_own_leftover_is_refused_not_tolerated(self):
        # A fresh deploy never tears a range down, its own included: resume that run, or
        # reclaim it with destroy-competition.py. A pre-existing bridge goes the same way.
        with self.assertRaises(SystemExit) as cm:
            self._run(_share(), [_vm(1000, f"tezcatlipoca;comp-c1;{RUN}")], bridges=["vmbr101"])
        self.assertIn("destroy-competition.py", str(cm.exception))

    def test_kept_templates_are_adopted_not_refused_multi_node(self):
        # Multi-node takes the same path: the engine template is kept by --keep-templates in
        # a nodes.json deploy too, so its slot is a template slot there as well (gating on
        # persistent_template alone — False when share.multi — refused our own kept template).
        kept = [dict(_vm(1140, "tezcatlipoca,comp-c1,run-practice,engine-template"),
                     template=1)]
        self._run(_share(multi=True), kept)      # must not raise

    def test_kept_templates_are_adopted_not_refused(self):
        # The --teams-only loop: a practice run's engine template + goldens are kept on
        # purpose and still carry the PRACTICE run id, while a fresh workspace mints a
        # new one. They are cached build artifacts, not a leftover range — phase 2/4
        # adopt or rebuild them by hash.
        kept = [dict(_vm(1140, "tezcatlipoca,comp-c1,run-practice,engine-template"),
                     template=1),
                dict(_vm(1150, "tezcatlipoca,comp-c1,run-practice,tezcatlipoca-golden"),
                     template=1)]
        self._run(_share(), kept)          # must not raise

    def test_our_own_dead_template_build_is_reclaimed_by_phase_2(self):
        # A template build whose clone task failed leaves an untagged, unconverted clone on
        # the reserved template vmid wearing the base image's own tags. It is this run's
        # wreckage, not a leftover range: phase 2 adopts and rebuilds it. Refusing here left
        # the deploy no way to clean up after itself (live 2026-10-15, run-bc3452dd:
        # 'collides with VMs already on node: engine template vmid 2340').
        self._run(_share(), [dict(_vm(1140, "cloud-init;template"), name="engine-template")])

    def test_another_competitions_range_in_a_template_slot_is_a_leftover(self):
        with self.assertRaises(SystemExit):
            self._run(_share(), [_vm(1150, "tezcatlipoca;comp-other;run-zzz")])

    def test_an_untemplated_vm_in_a_template_slot_is_a_leftover(self):
        # Tagged into THIS competition and this run, so it is a running range's box, not a
        # dead build attempt the deploy may help itself to.
        with self.assertRaises(SystemExit):
            self._run(_share(), [_vm(1140, f"tezcatlipoca;comp-c1;{RUN}")], bridges=["vmbr101"])

    def test_an_interrupted_clone_is_left_to_phase_1(self):
        # Untagged, but the clone POST's description carries our marker: no run owns it,
        # so it is not a leftover for the operator to reclaim — phase 1 destroys it.
        with patch("infra.collisions.proxmox_api", return_value={"data": []}), \
                patch.object(ownership, "has_clone_marker", return_value=True):
            check_collisions(_share(), _plan(_share()), [dict(_vm(1000, ""), lock="clone")])

    def test_a_failed_bridge_list_raises_instead_of_skipping_the_check(self):
        with patch("infra.collisions.proxmox_api", side_effect=RuntimeError("500 network")), \
                patch.object(ownership, "has_clone_marker", return_value=False):
            with self.assertRaises(RuntimeError):
                check_collisions(_share(), _plan(_share()), [])

    def test_foreign_bridge_alone_is_fatal(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(_share(), [], bridges=["vmbr101"])
        self.assertIn("bridge vmbr101", str(cm.exception))


if __name__ == "__main__":
    os.environ.setdefault("TEZ_PROXMOX_NODE", "pve")
    unittest.main()
