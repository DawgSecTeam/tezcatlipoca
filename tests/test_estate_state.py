"""estate/state.py: the one per-competition state document and its legacy migration.

Offline; every comp dir is a temp directory. The migration fixtures are shaped after
the real writers (deploy_lib/context.py, multinode/placement_record.py, common/targets.py,
deploy_lib/estate_inputs.py, engine/template_freeze.py, verifier/freeze.py, teardown/engine.py)."""

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

from estate import state  # noqa: E402


def _write_legacy(comp):
# The seven legacy files a deployed competition leaves next to its estate state.
    (comp / ".deploy_state.json").write_text(json.dumps({
        "pipeline_version": 3,
        "last_phase": 8,
        "run_id": "run-abc12345",
        "scoring_vm_id": 1080,
        "teams": {"team1": {"identifier": 101, "password": "team-one-pw"}},
        "admin_password": "admin-pw",
        "scoring_password": "scoring-pw",
        "postgres_password": "pg-pw",
        "redis_password": "redis-pw",
        "box_password": "box-pw",
        "box_creds": {"linux": "linux-pw"},
        "domain_creds": None,
        "inject_password": None,
        "deployed_endpoint": "https://pve.example:8006",
        "engine_template_vmid": 1220,
        "engine_template_hash": "e" * 64,
        "golden_template_ids": {"web01": 1230},
        "golden_ids_by_slot": {"0": {"web01": 1230}},
        "engine_build_info": {"built": True},
        "hold_start": False,
        "paused_after_phase": None,
        "portal_up": True,
        "portal_url": "https://portal.example",
        "nakon_failed_steps": [],
        "plant_coverage_failed": {"team1-web01": ["apache"]},
        "degradations": [],
        "remote_access": {"enabled": True, "comp_id": "comp-a"},
        "assume_breach": {"ok": True, "vmid": 1999},
    }))
    (comp / "placement.json").write_text(json.dumps({
        "version": 1,
        "engine_node": "node-a",
        "nodes": {"node-a": {"name": "node-a", "endpoint": "https://pve.example:8006",
                             "node": "pve", "datastore": "local-lvm",
                             "token_env": "TF_VAR_proxmox_api_token"}},
        "team_nodes": {"team1": "node-a"},
        "team_slots": {"team1": 0},
        "slots": {"node-a": 0},
        "team_identifiers": {"team1": 101},
        "satellites": [],
    }))
    (comp / "targets.json").write_text(json.dumps({
        "box_order": ["web01"],
        "targets": {"team1-web01": {"team_key": "team1", "identifier": "101",
                                    "box_name": "web01", "vmid": 1234,
                                    "ip": "192.168.101.11"}},
    }))
    (comp / "teams.json").write_text(json.dumps(
        {"team1": {"identifier": 101, "password": "team-one-pw"}}))
    (comp / ".template-hashes.json").write_text(json.dumps({
        "engine": {"hash": "e" * 64, "inputs": {"config": {"quotient_ref": "v1"}}},
        "golden": {"web01": {"hash": "g" * 64, "inputs": {"config": {}}}},
        "golden_planted": {"web01": "g" * 64},
        "updated": "2026-10-02 10:00:00",
    }))
    (comp / ".frozen.json").write_text(json.dumps({
        "frozen_at": "2026-10-02 10:00:00",
        "code": {"commit": "abc", "dirty": False},
        "hashes": {"engine": {"hash": "e" * 64}},
        "verify_report": {"gates": {}, "plant_coverage": True},
    }))
    (comp / ".teardown-journal.json").write_text(json.dumps({
        "run_id": "run-abc12345",
        "done": {"engine": {"detail": "destroyed"}},
        "updated_at": "2026-10-02T11:00:00",
    }))
    (comp / "boxes.json").write_text(json.dumps([{"name": "web01", "last_octet": 11}]))


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


class EstateMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.comp = self.root / "comp-a"
        self.comp.mkdir()
        _write_legacy(self.comp)
        self.env = {"TF_VAR_proxmox_endpoint": "https://pve.example:8006",
                    "TF_VAR_proxmox_node": "pve",
                    "TF_VAR_datastore": "local-lvm",
                    "TF_VAR_engine_mgmt_ip": "10.0.0.250",
                    "TF_VAR_engine_mgmt_gw": "10.0.0.1"}

    def test_migrate_maps_every_legacy_file(self):
        with patch.dict(os.environ, self.env):
            migrated = state.EstateState.migrate(self.comp)

        self.assertEqual(migrated.data["schema_version"], state.SCHEMA_VERSION)
        self.assertEqual(migrated.data["identity"]["comp_name"], "comp-a")
        self.assertEqual(migrated.data["identity"]["run_id"], "run-abc12345")
        self.assertEqual(migrated.data["identity"]["scoring_vm_id"], 1080)
        self.assertEqual(migrated.data["identity"]["endpoint"], "https://pve.example:8006")
        self.assertEqual(migrated.data["identity"]["node"], "pve")
        self.assertEqual(migrated.data["identity"]["datastore"], "local-lvm")
        self.assertEqual(migrated.data["identity"]["engine_mgmt_ip"], "10.0.0.250")
        self.assertEqual(migrated.data["secrets"]["teams"]["team1"]["password"],
                         "team-one-pw")
        self.assertEqual(migrated.data["secrets"]["box_password"], "box-pw")
        self.assertEqual(migrated.data["roster"]["boxes"],
                         [{"name": "web01", "last_octet": 11}])
        self.assertEqual(migrated.data["roster"]["targets"]["team1-web01"]["vmid"], 1234)
        self.assertEqual(migrated.data["roster"]["targets"]["team1-web01"]["slot"], 0)
        self.assertEqual(migrated.data["placement"]["engine_node"], "node-a")
        self.assertEqual(migrated.data["templates"]["engine"]["hash"], "e" * 64)
        self.assertEqual(migrated.data["templates"]["engine"]["vmid"], 1220)
        self.assertEqual(migrated.data["templates"]["golden"]["web01"]["vmid"], 1230)
        self.assertEqual(migrated.data["templates"]["golden"]["web01"]["hash"], "g" * 64)
        self.assertEqual(migrated.data["templates"]["golden_planted"], {"web01": "g" * 64})
        self.assertEqual(migrated.data["templates"]["frozen"]["frozen_at"],
                         "2026-10-02 10:00:00")
        self.assertEqual(migrated.data["run"]["last_phase"], 8)
        self.assertEqual(migrated.data["run"]["plant_coverage_failed"],
                         {"team1-web01": ["apache"]})
        self.assertEqual(migrated.data["external"]["headscale"]["comp_id"], "comp-a")
        self.assertEqual(migrated.data["external"]["portal"],
                         {"up": True, "url": "https://portal.example"})
        self.assertEqual(migrated.data["external"]["red01"], {"vmid": 1999})
        self.assertEqual(migrated.data["teardown"]["done"]["engine"]["detail"], "destroyed")

        written = state.state_path(self.comp)
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)
        self.assertEqual(json.loads(written.read_text())["identity"]["run_id"],
                         "run-abc12345")

    def test_migrate_is_additive_and_teams_json_falls_back(self):
        (self.comp / ".deploy_state.json").write_text(json.dumps({"run_id": "run-abc12345"}))
        with patch.dict(os.environ, self.env):
            migrated = state.EstateState.migrate(self.comp)

        self.assertEqual(migrated.data["secrets"]["teams"]["team1"]["password"],
                         "team-one-pw")
        for legacy in (".deploy_state.json", "placement.json", "targets.json", "teams.json",
                       ".template-hashes.json", ".frozen.json", ".teardown-journal.json"):
            self.assertTrue((self.comp / legacy).exists(), legacy)

    def test_the_deploys_own_endpoint_outranks_the_ambient_env(self):
        # Teardown's ownership refusal compares the loaded env against the host the deploy
        # actually used, so the recorded value must win over whatever env is loaded now.
        with patch.dict(os.environ, {**self.env,
                                     "TF_VAR_proxmox_endpoint": "https://elsewhere:8006"}):
            migrated = state.EstateState.migrate(self.comp)
        self.assertEqual(migrated.data["identity"]["endpoint"], "https://pve.example:8006")


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


class PartialDocumentDoesNotShadowLegacyState(unittest.TestCase):
    """A folded record can create the document before the deploy records its identity.

    The teardown journal is the live case: a teardown of a pre-estate range wrote the
    document, and the next pass then read it as authoritative, found no run id and refused
    with "estate-state.json carries no run id, so ownership cannot be proven".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.comp = Path(self.tmp.name)

    def _legacy(self, run_id="run-abc12345"):
        (self.comp / ".deploy_state.json").write_text(json.dumps(
            {"run_id": run_id, "pipeline_version": 3, "last_phase": 8}))

    def _partial_document(self):
        document = state.EstateState.load(self.comp)
        document.section("teardown")["done"] = {"red01": {"detail": "gone"}}
        document.save()

    def test_read_flat_prefers_the_legacy_file_over_an_identity_less_document(self):
        self._legacy()
        self._partial_document()
        self.assertEqual(state.read_flat(self.comp)["run_id"], "run-abc12345")

    def test_the_documents_own_keys_win_over_the_legacy_file(self):
        self._legacy()
        document = state.EstateState.load(self.comp)
        document.section("run")["started"] = True
        document.save()
        flat = state.read_flat(self.comp)
        self.assertTrue(flat["started"], "the document's own record must not be shadowed")
        self.assertEqual(flat["run_id"], "run-abc12345", "the legacy file fills the gap")

    def test_open_migrates_when_the_document_is_absent(self):
        self._legacy()
        self.assertEqual(state.open(self.comp).flatten()["run_id"], "run-abc12345")

    def test_an_identity_less_document_with_no_legacy_file_reads_as_itself(self):
        state.EstateState.load(self.comp).save()
        flat = state.read_flat(self.comp)
        self.assertIsNotNone(flat)
        self.assertNotIn("run_id", flat)
