"""One preflight implementation: single-node is a one-share plan, multi-node is N shares, and
both classify "ours" through ownership.ownership_verdict (the same proof destroy uses)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
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

    def test_satellite_slot_has_jump_vm_and_shifted_goldens(self):
        share = _share(True, 1, False)
        vmids = {s.vmid for s in expected_slots(share, _plan(share))}
        self.assertIn(1131, vmids)
        self.assertIn(1160, vmids)
        self.assertNotIn(1000, vmids)

    def test_transit_bridges_only_on_slot_zero(self):
        fw = BOXES + [{"name": "fw", "template": "t", "in_path": True}]
        self.assertEqual(expected_bridges(_share(), fw), ["vmbr101", "vmbrW101"])
        self.assertEqual(expected_bridges(_share(True, 1, False), fw), ["vmbr101"])


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
        from red.badauto_config_ops import default_red_vmid
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

    def test_the_tools_own_default_identifier_is_permitted(self):
        # The default block (101) must never be refused by the reserved-span check, or the
        # tool cannot deploy without an override.
        from multinode.nodes_config import MAX_SATELLITES
        from multinode.placement_record import reserved_vmids
        for engine in self.ENGINES:
            placement = {"engine_vmid": engine,
                         "slots": {f"n{s}": s for s in range(MAX_SATELLITES + 1)},
                         "satellites": [{"jump_vmid": engine + 130 + s}
                                        for s in range(1, MAX_SATELLITES + 1)]}
            self.assertIn(101, self._permitted_identifiers(reserved_vmids(placement)))


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

    def test_a_live_occupant_is_skipped(self):
        ip, _unchecked = self._scan({100: ["10.0.0.240"]})
        self.assertEqual(ip, "10.0.0.241")

    def test_the_legacy_defaults_are_never_selected(self):
        # .198 is what every other path still falls back to; the band excludes .196-.199.
        from infra.collisions import RED01_LEGACY_IPS
        self.assertEqual(RED01_LEGACY_IPS,
                         ("10.0.0.196", "10.0.0.197", "10.0.0.198", "10.0.0.199"))

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

    def test_a_consistent_object_passes(self):
        check_placement(self._placement())
        check_placement(None)

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

    def test_a_kept_golden_is_adopted_by_competition_and_not_by_dir_name(self):
        with tempfile.TemporaryDirectory() as td:
            share = _share()
            plan = PreflightPlan(self._workspace(td), BOXES, 1000, [share], our_run_tag=RUN)
            base = "tezcatlipoca,{tag},run-practice,golden"
            with patch("infra.collisions.proxmox_api", return_value={"data": []}), \
                    patch.object(ownership, "has_clone_marker", return_value=False):
                check_collisions(share, plan, [dict(_vm(1150, base.format(tag="comp-scrim-one-light")),
                                                    template=1)])
                with self.assertRaises(SystemExit):
                    check_collisions(share, plan,
                                     [dict(_vm(1150, base.format(tag="comp-other")), template=1)])


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
