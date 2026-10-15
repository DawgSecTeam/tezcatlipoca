"""A placement's record names must never reach a node-scoped API path.

activate_placement keys the route table by PVE node name, so `/nodes/<record>/qemu` is an
HTTP 500 ("hostname lookup '<record>' failed"). The teardown's engine node used the record
name, so the inventory, the pre-stop and the leftover sweep were all skipped (and the
verdict read NOT CLEAN) even though the satellite entries were already mapped
(2026-10-14, a field deploy's teardown).
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

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
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve1"}), \
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
        stale = {"TF_VAR_proxmox_endpoint": "https://realm.invalid:8006",
                 "TF_VAR_proxmox_api_token": "realm-token",
                 "TF_VAR_proxmox_node": "realmhost",
                 "TF_VAR_datastore": "hdd"}

        def fake_activate(_placement):
            os.environ.update({"TF_VAR_proxmox_endpoint": "https://field.invalid:8006",
                               "TF_VAR_proxmox_api_token": "field-token",
                               "TF_VAR_proxmox_node": "pve1",
                               "TF_VAR_datastore": "hdrives-zfs"})

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

        self.assertEqual(captured["env"]["TF_VAR_proxmox_endpoint"], "https://field.invalid:8006")
        self.assertEqual(captured["env"]["TF_VAR_proxmox_api_token"], "field-token")
        self.assertEqual(captured["env"]["TF_VAR_proxmox_node"], "pve1")


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


if __name__ == "__main__":
    unittest.main()
