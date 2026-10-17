"""estate/state.py: the one per-competition state document.

Offline; every comp dir is a temp directory. The fixtures are shaped after the real
writers (multinode/placement_record.py, common/targets.py, engine/template_freeze.py,
teardown/engine.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from estate import state  # noqa: E402


class EstateStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = self.root / "comp-a"
        self.comp.mkdir()

    def test_fresh_skeleton_has_version_and_comp_name(self):
        loaded = state.EstateState.load(self.comp)
        self.assertEqual(loaded.data["schema_version"], state.SCHEMA_VERSION)
        self.assertEqual(loaded.data["identity"]["comp_name"], "comp-a")
        self.assertFalse(state.state_path(self.comp).exists())

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

    def test_record_upserts_same_vmid(self):
        loaded = state.EstateState.load(self.comp)
        loaded.record("team_box", "pve", "team1-web01", vmid=1234, tags=["a"])
        loaded.record("team_box", "pve2", "team1-web01", vmid=1234, tags=["a", "b"])

        entries = loaded.resources(kind="team_box")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tags"], ["a", "b"])
        self.assertEqual(entries[0]["node"], "pve2")

    def test_forget_by_vmid_and_bridge(self):
        loaded = state.EstateState.load(self.comp)
        loaded.record("team_box", "pve", "team1-web01", vmid=1234)
        loaded.record("team_box", "pve", "team1-db01", vmid=1235)
        loaded.record("bridge", "pve", "vmbr101", bridge="vmbr101")

        self.assertEqual(loaded.forget("team_box", vmid=1235), 1)
        self.assertEqual(loaded.forget("bridge", bridge="vmbr101"), 1)
        self.assertEqual(loaded.forget("team_box", vmid=9999), 0)
        self.assertEqual([e["vmid"] for e in loaded.resources()], [1234])

    def test_checkpoint_persists_phase(self):
        state.EstateState.load(self.comp).checkpoint(4)
        on_disk = json.loads(state.state_path(self.comp).read_text())
        self.assertEqual(on_disk["run"]["last_phase"], 4)
        self.assertTrue(on_disk["updated_at"])

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
        self.assertIn("v1", message)

    def test_load_all_finds_every_comp(self):
        for name in ("comp-a", "comp-b"):
            comp = self.root / name
            comp.mkdir(exist_ok=True)
            state.EstateState.load(comp).checkpoint(1)
        (self.root / "not-a-comp").mkdir()

        found = state.load_all(self.root)
        self.assertEqual([s.data["identity"]["comp_name"] for s in found],
                         ["comp-a", "comp-b"])
        self.assertEqual([s.data["run"]["last_phase"] for s in found], [1, 1])


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

    def test_absorb_leaves_structured_sections_alone(self):
        self.state.data["templates"]["engine"] = {"hash": "e" * 64}
        self.state.absorb({"last_phase": 3})
        self.assertEqual(self.state.data["templates"]["engine"], {"hash": "e" * 64})


class TheDocumentIsTheOnlyWriter(unittest.TestCase):
    """Every folded record must land in the state document, and nowhere else."""

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

    def test_a_frozen_record_lands_in_the_document(self):
        from engine.template_freeze import frozen_state
        document = state.EstateState.load(self.comp)
        document.section("templates")["frozen"] = {"frozen_at": "now"}
        document.save()
        self.assertEqual(frozen_state(self.comp), {"frozen_at": "now"})
        self.assertEqual(sorted(p.name for p in self.comp.iterdir()), ["estate-state.json"])

    def test_a_placement_write_survives_the_deploy_state_save(self):
        # The planner writes `placement` straight to the file while the deploy context holds
        # its own copy of the document from load_prior_deploy_state, so saving that copy back
        # dropped the record: state documents written before this gate read `placement: None`
        # while the run's own log said "Placement recorded in <that file>". The record is what
        # makes red01's selected address durable ("the recorded value is never re-derived"),
        # so it must outlive the deploy's own save.
        from deploy_lib.context import DeployContext
        from multinode.placement_record import write_placement

        ctx = DeployContext(comp_dir=self.comp)  # binds a copy of the document, no placement
        self.assertIsNone(ctx.document().data.get("placement"))
        write_placement(self.comp, {"version": 2, "engine_vmid": 2200})
        ctx.state["last_phase"] = 1
        ctx.save_state()

        self.assertEqual(state.EstateState.load(self.comp).data["placement"],
                         {"version": 2, "engine_vmid": 2200})
