"""One preflight implementation: single-node is a one-share plan, multi-node is N shares, and
both classify "ours" through ownership.ownership_verdict (the same proof destroy uses)."""

import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from infra import ownership  # noqa: E402
from infra.collisions import check_collisions, expected_bridges, expected_slots  # noqa: E402
from infra.plan import NodeShare, PreflightPlan, single_node_plan  # noqa: E402
from infra.resources import check_share_templates  # noqa: E402

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

    def test_ours_passes_and_tolerates_own_bridge(self):
        self._run(_share(), [_vm(1000, f"tezcatlipoca;comp-c1;{RUN}")], bridges=["vmbr101"])

    def test_a_failed_bridge_list_raises_instead_of_skipping_the_check(self):
        with patch("infra.collisions.proxmox_api", side_effect=RuntimeError("500 network")), \
                patch.object(ownership, "has_clone_marker", return_value=False):
            with self.assertRaises(RuntimeError):
                check_collisions(_share(), _plan(_share()), [])

    def test_foreign_bridge_alone_is_fatal(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(_share(), [], bridges=["vmbr101"])
        self.assertIn("bridge vmbr101", str(cm.exception))

    def test_persistent_engine_template_is_not_a_leftover_in_single_mode(self):
        # ours ET alone must not license a pre-existing bridge (single-node semantics)
        with self.assertRaises(SystemExit):
            self._run(_share(), [_vm(1140, f"tezcatlipoca;comp-c1;{RUN}")], bridges=["vmbr101"])


class EngineBaseImage(unittest.TestCase):
    @contextlib.contextmanager
    def _env(self, template_vm_id=None):
        # clear=True: the ambient env must not answer for a variable this test drops
        env = {"TF_VAR_proxmox_node": "pve"}
        if template_vm_id is not None:
            env["TF_VAR_template_vm_id"] = template_vm_id
        with patch.dict(os.environ, env, clear=True):
            yield

    def _gate(self, comp, template_vm_id=None):
        with self._env(template_vm_id):
            plan = single_node_plan(comp, BOXES, 1, TEAMS, 1000, True, None, RUN)
            plan.shares[0].check_templates = False   # this case is the engine base, not box templates
            check_share_templates(plan.shares[0], BOXES, [])

    def test_blank_vmid_refuses_naming_the_variable(self):
        # a fresh estate's env ships it empty; the raw int() was "ValueError: invalid
        # literal for int() with base 10: ''" from inside the preflight
        with self.assertRaises(SystemExit) as cm:
            self._gate(Path("competitions/c1"), "")
        self.assertIn("TF_VAR_template_vm_id", str(cm.exception))

    def test_unset_vmid_refuses_naming_the_variable(self):
        with self.assertRaises(SystemExit) as cm:
            self._gate(Path("competitions/c1"))
        self.assertIn("TF_VAR_template_vm_id", str(cm.exception))

    def test_valid_vmid_still_resolves_to_that_int(self):
        with self._env("955"):
            plan = single_node_plan(Path("competitions/c1"), BOXES, 1, TEAMS, 1000, True,
                                    None, RUN)
            self.assertEqual(plan.shares[0].engine_base(), 955)


if __name__ == "__main__":
    os.environ.setdefault("TF_VAR_proxmox_node", "pve")
    unittest.main()
