"""A placement's record names must never reach a node-scoped API path.

activate_placement keys the route table by PVE node name, so `/nodes/<record>/qemu` is an
HTTP 500 ("hostname lookup '<record>' failed"). The teardown's engine node used the record
name, so the inventory, the pre-stop and the leftover sweep were all skipped (and the
verdict read NOT CLEAN) even though the satellite entries were already mapped
(2026-10-14, a field deploy's teardown).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from estate import state as estate_state  # noqa: E402
import teardown.machinery as tmach  # noqa: E402
import teardown.runner as trunner  # noqa: E402
import teardown.steps as tsteps  # noqa: E402

REC = {"name": "rec-engine", "endpoint": "https://10.0.0.9:8006", "node": "pve1",
       "datastore": "ds", "token_env": "TOK", "engine_base_vmid": 900}
SAT = {"name": "rec-sat", "endpoint": "https://10.0.0.10:8006", "node": "pve2",
       "datastore": "ds2", "token_env": "TOK2", "engine_base_vmid": 901}
PLACEMENT = {"engine_node": "rec-engine", "nodes": {"rec-engine": REC, "rec-sat": SAT},
             "team_nodes": {"team1": "rec-engine", "team2": "rec-sat"},
             "slots": {"rec-engine": 0, "rec-sat": 1},
             "satellites": [{"name": "rec-sat", "slot": 1, "teams": ["team2"]}],
             "team_identifiers": {"team1": "101", "team2": "102"}}


class ContextNodes(unittest.TestCase):
    def test_the_engine_node_is_the_pve_name_not_the_record_name(self):
        captured = {}
        engine = SimpleNamespace(execute=lambda plan: [],
                                 verify_clean=lambda plan, results: "ok")
        ownership = SimpleNamespace(run_id="run-x", tags=["comp-c", "run-x"])
        with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "pve1"}), \
                patch.object(trunner, "read_placement", return_value=PLACEMENT), \
                patch.object(trunner, "activate_placement", lambda p: {}), \
                patch.object(trunner, "frozen_state", return_value={}), \
                patch.object(trunner, "refuse_frozen_full_teardown", lambda *a, **k: None), \
                patch.object(trunner, "load_ownership", return_value=({}, ownership)), \
                patch.object(trunner, "TeardownContext",
                             side_effect=lambda **kw: captured.update(kw) or SimpleNamespace(**kw)), \
                patch.object(trunner, "TeardownEngine", return_value=engine):
            trunner.run_terminal_teardown(Path("/nonexistent"),
                                          {"team1": {"identifier": "101"},
                                           "team2": {"identifier": "102"}},
                                          [{"name": "web01"}], "demo")
        self.assertEqual(captured["nodes"], ["pve1", "pve2"])


class PlacementIdentityReachesTheContext(unittest.TestCase):
    def test_a_stale_caller_snapshot_is_replaced_by_the_placements_identity(self):
        captured = {}
        engine = SimpleNamespace(execute=lambda plan: [],
                                 verify_clean=lambda plan, results: "ok")
        ownership = SimpleNamespace(run_id="run-x", tags=["comp-c", "run-x"])
        stale = {"TEZ_PROXMOX_ENDPOINT": "https://realm.invalid:8006",
                 "TEZ_PROXMOX_API_TOKEN": "realm-token",
                 "TEZ_PROXMOX_NODE": "realmhost",
                 "TEZ_PROXMOX_DATASTORE": "hdd"}

        def fake_activate(_placement):
            os.environ.update({"TEZ_PROXMOX_ENDPOINT": "https://field.invalid:8006",
                               "TEZ_PROXMOX_API_TOKEN": "field-token",
                               "TEZ_PROXMOX_NODE": "pve1",
                               "TEZ_PROXMOX_DATASTORE": "hdrives-zfs"})

        with patch.dict(os.environ, stale), \
                patch.object(trunner, "read_placement", return_value=PLACEMENT), \
                patch.object(trunner, "activate_placement", side_effect=fake_activate), \
                patch.object(trunner, "frozen_state", return_value={}), \
                patch.object(trunner, "refuse_frozen_full_teardown", lambda *a, **k: None), \
                patch.object(trunner, "load_ownership", return_value=({}, ownership)), \
                patch.object(trunner, "TeardownContext",
                             side_effect=lambda **kw: captured.update(kw) or SimpleNamespace(**kw)), \
                patch.object(trunner, "TeardownEngine", return_value=engine):
            trunner.run_terminal_teardown(Path("/nonexistent"),
                                          {"team1": {"identifier": "101"},
                                           "team2": {"identifier": "102"}},
                                          [{"name": "web01"}], "demo", env=dict(stale))

        self.assertEqual(captured["env"]["TEZ_PROXMOX_ENDPOINT"], "https://field.invalid:8006")
        self.assertEqual(captured["env"]["TEZ_PROXMOX_API_TOKEN"], "field-token")
        self.assertEqual(captured["env"]["TEZ_PROXMOX_NODE"], "pve1")


class PreStopNodes(unittest.TestCase):
    def test_pre_stop_lists_teams_on_their_pve_node_names(self):
        seen = {}
        ctx = SimpleNamespace(
            teams={"team1": {"identifier": "101"}, "team2": {"identifier": "102"}},
            boxes=[{"name": "web01"}], default_node="pve1", placement=PLACEMENT,
            ownership=SimpleNamespace(tags=["comp-c", "run-x"]))
        with patch.object(tmach, "pre_stop_windows_boxes",
                          side_effect=lambda *a, **k: seen.update(k)):
            tsteps.PreStopStep().run(ctx)
        self.assertEqual(seen["team_nodes"], {"team1": "pve1", "team2": "pve2"})


class OwnershipIdentity(unittest.TestCase):
    def _workspace(self, td, comp_name):
        comp = Path(td) / "run-abc12345" / "comp"
        comp.mkdir(parents=True)
        (comp / "Compfile").write_text("name scrim-one-light\nscenario x\ndifficulty 1\n")
        (comp / "estate-state.json").write_text(json.dumps({
            "schema_version": 1,
            "identity": {"comp_name": comp_name, "run_id": "run-abc12345"},
            "resources": []}))
        return comp

    def test_the_deploys_recorded_identity_wins_over_the_container_dir(self):
        with tempfile.TemporaryDirectory() as td:
            _state, ownership = tmach.load_ownership(self._workspace(td, "scrim-one-light"))
        self.assertIn("comp-scrim-one-light", ownership.tags)
        self.assertNotIn("comp-comp", ownership.tags)

    def test_a_state_without_the_record_falls_back_to_the_compfile_name(self):
        with tempfile.TemporaryDirectory() as td:
            _state, ownership = tmach.load_ownership(self._workspace(td, ""))
        self.assertIn("comp-scrim-one-light", ownership.tags)

    def test_the_document_and_the_deploy_stamp_the_same_comp_tag(self):
        # A dashboard workspace is <run_dir>/comp: the deploy stamps the Compfile `name`,
        # and the destroy path must resolve that same string from the document.
        from common.constants import ownership_tags
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td) / "run-abc12345" / "comp"
            comp.mkdir(parents=True)
            (comp / "Compfile").write_text("name crimson-relay\nscenario x\ndifficulty 1\n")
            document = estate_state.EstateState.load(comp)
            document.section("identity")["run_id"] = "run-abc12345"
            document.save()
            recorded = estate_state.EstateState.load(comp).data["identity"]["comp_name"]
            _state, ownership = tmach.load_ownership(comp)
        self.assertEqual(recorded, "crimson-relay")
        self.assertEqual(ownership.tags, ownership_tags(recorded, "run-abc12345"))

    def test_an_old_documents_tag_wins_and_the_disagreement_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            comp = self._workspace(td, "comp")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                _state, ownership = tmach.load_ownership(comp)
        self.assertIn("comp-comp", ownership.tags)      # the tag its VMs actually carry
        self.assertIn("using the RECORDED name", out.getvalue())


if __name__ == "__main__":
    unittest.main()
