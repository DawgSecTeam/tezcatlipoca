"""One preflight implementation: single-node is a one-share plan, multi-node is N shares, and
both classify "ours" through ownership.ownership_verdict (the same proof destroy uses)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from infra import ownership  # noqa: E402
from infra.collisions import (check_collisions, check_mgmt_bridge, expected_bridges,  # noqa: E402
                              check_placement, expected_slots, select_red_ip)
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
        single = {s.vmid for s in expected_slots(_share(), _plan(_share()))}
        multi = {s.vmid for s in expected_slots(_share(multi=True), _plan(_share(multi=True)))}
        self.assertEqual(single, multi)
        self.assertIn(1000, single)          # engine
        self.assertIn(1140, single)          # engine template
        self.assertIn(1150, single)          # golden 0
        self.assertIn(1210, single)          # team 101 box 0




class SlotBandsAreDisjoint(unittest.TestCase):
    # The property test §6.5 asks for: for every (engine, identifier, boxes, slots) a
    # placement can produce, the engine, engine-template, golden-per-slot, jump-per-slot,
    # red and team bands are pairwise disjoint, and red sits below the lowest team block.
    ENGINES = (1000, 1300, 9000)

    def _permitted_identifiers(self, reserved):
        from common.constants import MAX_BOXES_PER_TEAM
        from common.targets import vm_id_for
        out = []
        for identifier in range(1, 255):
            block = {vm_id_for(identifier, i) for i in range(MAX_BOXES_PER_TEAM)}
            if not block & reserved:
                out.append(identifier)
        return out

    def test_every_permitted_placement_is_pairwise_disjoint(self):
        from common.constants import MAX_BOXES_PER_TEAM
        from common.targets import vm_id_for
        from multinode.nodes_config import JUMP_VMID_OFFSET, MAX_SATELLITES
        from multinode.placement_record import (engine_template_vmid, golden_slot_span,
                                                reserved_vmids)
        from red.red01_ops import default_red_vmid
        for engine in self.ENGINES:
            for slots in range(MAX_SATELLITES + 1):
                placement = {
                    "engine_vmid": engine,
                    "slots": {f"n{s}": s for s in range(slots + 1)},
                    "satellites": [{"jump_vmid": engine + JUMP_VMID_OFFSET + s}
                                   for s in range(1, slots + 1)],
                }
                reserved = reserved_vmids(placement)
                red = default_red_vmid(engine, slots)
                bands = {"engine": {engine}, "template": {engine_template_vmid(engine)},
                         "red": {red}}
                for s in range(slots + 1):
                    bands[f"golden{s}"] = set(golden_slot_span(engine, s))
                for s in range(1, slots + 1):
                    bands[f"jump{s}"] = {engine + JUMP_VMID_OFFSET + s}
                permitted = self._permitted_identifiers(reserved)
                self.assertTrue(permitted, f"engine {engine} slots {slots}: nothing permitted")
                for identifier in permitted:
                    team = {vm_id_for(identifier, i) for i in range(MAX_BOXES_PER_TEAM)}
                    seen = {}
                    for name, band in list(bands.items()) + [("team", team)]:
                        for vmid in band:
                            self.assertNotIn(
                                vmid, seen,
                                f"engine {engine} slots {slots} id {identifier}: vmid {vmid} "
                                f"in both {seen.get(vmid)} and {name}")
                            seen[vmid] = name
                    if min(team) > max(reserved):
                        # The positional claim ("red below the lowest team block") holds for
                        # team blocks ABOVE the engine's own span, which is all a permitted
                        # block above it can be. It cannot hold in general: team vmids are
                        # absolute (200+id*10) while red is engine-relative, so a low
                        # identifier's block legitimately sits below red (engine 1300, id 101
                        # -> 1210) and one below the span is still permitted and disjoint.
                        # Disjointness above is the invariant that keeps both safe; this
                        # mismatch is a design-document error, reported rather than hidden.
                        self.assertLess(red, min(team),
                                        f"engine {engine} slots {slots} id {identifier}: red "
                                        f"{red} is not below the team block {min(team)}")



class RedAddressSelection(unittest.TestCase):
    def _scan(self, addrs_by_vmid, claimed=()):
        vms = [{"vmid": v, "node": "pve", "status": "running", "tags": ""}
               for v in addrs_by_vmid]
        with patch("infra.collisions.guest_ipv4s",
                   side_effect=lambda vm: addrs_by_vmid[vm["vmid"]]):
            return select_red_ip(vms, claimed)

    def test_claimed_addresses_are_never_selected(self):
        # The engine's own address and its gateway look free to a scan taken before that VM
        # exists; they are the placement's claims and are excluded by construction.
        ip, _unchecked = self._scan({100: ["10.0.0.240"], 101: ["10.0.0.241"]},
                                    claimed={"10.0.0.240", "10.0.0.241", "10.0.0.240"})
        self.assertEqual(ip, "10.0.0.242")



    def test_nothing_provably_free_refuses(self):
        taken = {v: [f"10.0.0.{240 + i}"] for i, v in enumerate(range(200, 210))}
        ip, _unchecked = self._scan(taken)
        self.assertIsNone(ip)


class PlacementSelfProof(unittest.TestCase):
    def _placement(self, **over):
        placement = {"version": 2, "engine_vmid": 1000, "engine_template_vmid": 1140,
                     "golden_vmids": {"n1": list(range(1150, 1160)),
                                      "n2": list(range(1160, 1170))},
                     "slots": {"n1": 0, "n2": 1},
                     "satellites": [{"name": "n2", "slot": 1, "jump_vmid": 1131}],
                     "team_identifiers": {"team1": "101"},
                     "red01": {"vmid": 1170}}
        placement.update(over)
        return placement


    def test_a_vmid_in_two_of_this_runs_bands_is_refused(self):
        with self.assertRaises(SystemExit) as cm:
            check_placement(self._placement(golden_vmids={"n1": [1131]}))  # also the jump
        self.assertIn("two of this run's own bands", str(cm.exception))

    def test_a_team_block_over_a_band_is_refused(self):
        # engine 1000, red01 1170: identifier 97 maps to vmids 1170..1179.
        with self.assertRaises(SystemExit):
            check_placement(self._placement(team_identifiers={"team1": "97"}))


class CompetitionIdentity(unittest.TestCase):
    def _workspace(self, td):
        comp = Path(td) / "run-abc123" / "comp"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name scrim-one-light\nscenario x\ndifficulty 1\n")
        return comp

    def test_a_run_comp_workspace_tags_the_competition_not_the_container_dir(self):
        with tempfile.TemporaryDirectory() as td:
            plan = PreflightPlan(self._workspace(td), BOXES, 1000, [_share()], our_run_tag=RUN)
            self.assertIn("comp-scrim-one-light", plan.our_tags)
            self.assertNotIn("comp-comp", plan.our_tags)



class MgmtBridge(unittest.TestCase):
    def test_refuses_a_bridge_the_node_does_not_own(self):
        with patch("infra.collisions.proxmox_api",
                   return_value={"data": [{"iface": "vmbr100"}]}):
            with self.assertRaises(SystemExit) as cm:
                check_mgmt_bridge("pve", "vmbr0")
            check_mgmt_bridge("pve", "vmbr100")
        self.assertIn("vmbr0 does not exist on node 'pve'", str(cm.exception))


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
        # the deploy no way to clean up after itself (caught live:
        # 'collides with VMs already on node: engine template vmid 2340').
        self._run(_share(), [dict(_vm(1140, "cloud-init;template"), name="engine-template")])

    def test_another_competitions_range_in_a_template_slot_is_a_leftover(self):
        with self.assertRaises(SystemExit):
            self._run(_share(), [_vm(1150, "tezcatlipoca;comp-other;run-zzz")])


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



if __name__ == "__main__":
    os.environ.setdefault("TEZ_PROXMOX_NODE", "pve")
    unittest.main()
