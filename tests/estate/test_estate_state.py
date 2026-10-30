"""estate/state.py: the one per-competition state document."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from estate import state  # noqa: E402


class EstateStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = self.root / "comp-a"
        self.comp.mkdir()


    def test_round_trip_records_and_filters(self):
        loaded = state.EstateState.load(self.comp)
        loaded.record("bridge", "pve", "vmbr101", bridge="vmbr101", tags=["comp-a"])
        loaded.record("team_box", "pve", "team1-web01", vmid=1234, tags=["comp-a", "run-1"])
        loaded.record("team_box", "pve", "team2-web01", vmid=1244, tags=["comp-a", "run-1"])
        loaded.save()

        reloaded = state.EstateState.load(self.comp)
        self.assertEqual(len(reloaded.resources()), 3)
        self.assertEqual([e["vmid"] for e in reloaded.resources(kind="team_box")], [1234, 1244])
        self.assertEqual([e["name"] for e in reloaded.resources(kind="bridge")], ["vmbr101"])
        self.assertEqual([e["bridge"] for e in reloaded.resources(node="pve", kind="bridge")],
                         ["vmbr101"])
        self.assertEqual(reloaded.resources(node="pve2"), [])


    def test_forget_by_vmid_and_bridge(self):
        loaded = state.EstateState.load(self.comp)
        loaded.record("team_box", "pve", "team1-web01", vmid=1234)
        loaded.record("team_box", "pve", "team1-db01", vmid=1235)
        loaded.record("bridge", "pve", "vmbr101", bridge="vmbr101")

        self.assertEqual(loaded.forget("team_box", vmid=1235), 1)
        self.assertEqual(loaded.forget("bridge", bridge="vmbr101"), 1)
        self.assertEqual(loaded.forget("team_box", vmid=9999), 0)
        self.assertEqual([e["vmid"] for e in loaded.resources()], [1234])


    def test_corrupt_file_is_refused(self):
        state.state_path(self.comp).write_text("{ not json")
        with self.assertRaises(SystemExit) as caught:
            state.EstateState.load(self.comp)
        self.assertIn("estate-state.json", str(caught.exception))

    def test_newer_schema_version_is_refused(self):
        state.state_path(self.comp).write_text(json.dumps({"schema_version": 99,
                                                           "identity": {}}))
        with self.assertRaises(SystemExit) as caught:
            state.EstateState.load(self.comp)
        message = str(caught.exception)
        self.assertIn("estate-state.json", message)
        self.assertIn("v99", message)
        self.assertIn("v2", message)



class FlatBridgeTest(unittest.TestCase):
    """The phases still speak the flat state dict; the file speaks sections."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = state.EstateState.load(Path(self.tmp.name))

    def test_absorb_routes_each_key_to_its_section(self):
        self.state.absorb({"run_id": "run-abc12345", "teams": {"team1": {"identifier": 101}},
                           "engine_template_vmid": 1220, "last_phase": 8,
                           "nakon_failed_steps": ["x"]})
        self.assertEqual(self.state.data["identity"]["run_id"], "run-abc12345")
        self.assertEqual(self.state.data["secrets"]["teams"]["team1"]["identifier"], 101)
        self.assertEqual(self.state.data["templates"]["engine_template_vmid"], 1220)
        self.assertEqual(self.state.data["run"]["last_phase"], 8)
        # An undeclared key is deploy bookkeeping and lands in run, never dropped.
        self.assertEqual(self.state.data["run"]["nakon_failed_steps"], ["x"])

    def test_flat_view_finds_the_engine_address_a_multinode_deploy_recorded(self):
        # A multi-node deploy keeps the engine's address in `placement` (it is a per-node choice
        # there), not in `identity` — and `verify`/every resume reads it from this flat view.
        self.state.absorb({"run_id": "run-abc12345", "scoring_vm_id": 1500})
        self.state.data["placement"] = {"engine_mgmt_ip": "10.0.0.252",
                                        "engine_mgmt_gw": "10.0.0.1"}
        flat = self.state.flatten()
        self.assertEqual(flat["engine_mgmt_ip"], "10.0.0.252")
        self.assertEqual(flat["engine_mgmt_gw"], "10.0.0.1")

    def test_round_trip_is_lossless_and_survives_a_reload(self):
        flat = {"run_id": "run-abc12345", "last_phase": 8, "teams": {"team1": {"identifier": 101}},
                "admin_password": "pw", "boxes": [{"name": "web01"}], "started": True,
                "injects_created": ["a"], "portal_up": False}
        self.state.absorb(flat)
        self.state.save()
        reloaded = state.EstateState.load(Path(self.tmp.name)).flatten()
        # Every written key survives; the skeleton's own comp_name is extra.
        self.assertEqual({key: reloaded[key] for key in flat}, flat)
        self.assertEqual(reloaded["comp_name"], Path(self.tmp.name).name)



class TheDocumentIsTheOnlyWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name)

    def test_every_folded_writer_stays_in_the_document(self):
        from common.targets import persist_targets
        from engine.template_freeze import save_template_hashes
        from multinode.placement_record import write_placement
        from teardown.engine import save_teardown_journal

        targets = [{"vm_name": "team1-web01", "team_key": "team1", "identifier": "101",
                    "box_name": "web01", "vmid": 1210, "ip": "192.168.101.4",
                    "node": "pve"}]
        persist_targets(self.comp, targets, [{"name": "web01"}])
        save_template_hashes(self.comp, engine={"hash": "e" * 64})
        write_placement(self.comp, {"version": 1, "engine_node": "n1"})
        save_teardown_journal(self.comp, {"run_id": "run-abc12345", "done": {"red01": {}}})

        self.assertEqual(sorted(p.name for p in self.comp.iterdir()), ["estate-state.json"])
        document = state.EstateState.load(self.comp)
        self.assertEqual(document.section("roster")["targets"]["team1-web01"]["vmid"], 1210)
        self.assertEqual(document.section("templates")["engine"]["hash"], "e" * 64)
        self.assertEqual(document.data["placement"]["engine_node"], "n1")
        self.assertEqual(document.section("teardown")["done"], {"red01": {}})


    def test_a_red_identity_write_survives_the_deploy_state_save(self):
        # red01_ops writes `red` straight to the file; the context's save used to clobber
        # its own copy, and teardown then skipped red01.
        from deploy_lib.context import DeployContext
        from red import red01_ops

        ctx = DeployContext(comp_dir=self.comp)
        ctx.document()
        red01_ops.record_identity(self.comp, None, vmid=1671, ip="10.0.0.195",
                                  node="proxmox")
        ctx.state["last_phase"] = 7
        ctx.save_state()

        self.assertEqual(state.EstateState.load(self.comp).data["red"],
                         {"vmid": 1671, "ip": "10.0.0.195", "node": "proxmox"})

    def test_a_placement_write_survives_the_deploy_state_save(self):
        # The planner writes `placement` straight to the file; the context's save used to
        # clobber it. That record keeps red01's selected address durable, never re-derived.
        from deploy_lib.context import DeployContext
        from multinode.placement_record import write_placement

        ctx = DeployContext(comp_dir=self.comp)
        self.assertIsNone(ctx.document().data.get("placement"))
        write_placement(self.comp, {"version": 2, "engine_vmid": 2200})
        ctx.state["last_phase"] = 1
        ctx.save_state()

        self.assertEqual(state.EstateState.load(self.comp).data["placement"],
                         {"version": 2, "engine_vmid": 2200})
