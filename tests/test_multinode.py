"""Multi-node placement, jump rules, slot vmid math, and sync planner — offline.

No live Proxmox: probes are stubbed, placement is computed from fake probe dicts,
and the sync planner's API reads are patched. The single-node invariant (no
nodes.json -> single-node fallback) is asserted alongside."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy_lib import placement as dl_placement  # noqa: E402
from estate import config  # noqa: E402
from estate import state as estate_state  # noqa: E402
from net import jump_ops  # noqa: E402
from multinode import nodes_config  # noqa: E402
from multinode import placement_planner  # noqa: E402
from multinode import placement_record  # noqa: E402
from infra import client  # noqa: E402
from infra.gates import run_preflight  # noqa: E402
from infra.plan import multinode_plan  # noqa: E402
from common.targets import enumerate_targets  # noqa: E402


def _rec(name, node=None, **kw):
    base = dict(name=name, endpoint=f"https://{name}.lab:8006", node=node or name.upper(),
                datastore=f"ds-{name}", token_env=f"TOK_{name.upper()}",
                engine_base_vmid=900)
    base.update(kw)
    return nodes_config.NodeRecord(**base)


def _probe(name, ok=True, mem_gib=64, templates=(), collisions=(), running=2):
    return {"name": name, "node": name.upper(), "ok": ok,
            "reasons": [] if ok else ["probe: down"],
            "missing_templates": [], "collisions": list(collisions),
            "mem_free_bytes": mem_gib * 1024 ** 3, "datastore_free": 500 * 1024 ** 3,
            "running_ours": running,
            "templates": {t: 900 + i for i, t in enumerate(templates)}}


BOXES = [
    {"name": "web01", "last_octet": 2, "cpu": 1, "memory_mb": 2048, "template": "ubuntu-fix"},
    {"name": "app01", "last_octet": 3, "cpu": 1, "memory_mb": 1024, "template": "alpine-fix"},
]
TEAMS = {f"team{i}": {"identifier": str(100 + i), "password": "pw"} for i in range(1, 5)}


class LoadNodesConfigTest(unittest.TestCase):
    def test_absent_file_is_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            records, balancing = nodes_config.load_nodes_config(Path(td) / "nodes.json")
        self.assertIsNone(records)
        self.assertIsNone(balancing)

    def test_valid_config_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            path.write_text(json.dumps({
                "balancing": {"prefer": ["b"]},
                "nodes": [{"name": "a", "endpoint": "https://a:8006/", "node": "pveA",
                           "datastore": "ds", "token_env": "TOK_A", "engine_base_vmid": 955},
                          {"name": "b", "endpoint": "https://b:8006", "node": "pveB",
                           "datastore": "zfs", "token_env": "TOK_B", "engine_base_vmid": 1007,
                           "weight": 2.0, "jump_mgmt_ip": "10.0.0.240"}]}))
            records, balancing = nodes_config.load_nodes_config(path)
        self.assertEqual([r.name for r in records], ["a", "b"])
        self.assertEqual(records[1].engine_base_vmid, 1007)
        self.assertEqual(records[1].jump_mgmt_ip, "10.0.0.240")
        self.assertEqual(balancing, {"prefer": ["b"]})

    def test_missing_required_field_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            path.write_text(json.dumps({"nodes": [{"name": "a"}]}))
            with self.assertRaises(SystemExit):
                nodes_config.load_nodes_config(path)

    def test_duplicate_names_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            path.write_text(json.dumps({"nodes": [
                {"name": "a", "endpoint": "https://a:8006", "node": "n1", "datastore": "d",
                 "token_env": "T"},
                {"name": "a", "endpoint": "https://b:8006", "node": "n2", "datastore": "d",
                 "token_env": "T"}]}))
            with self.assertRaises(SystemExit):
                nodes_config.load_nodes_config(path)

    # Two nodes on one token var is the scale8 soak's mid-preflight 401:
    # activate_placement applies one record's token at a time INTO the env var the
    # record names, so a shared name means the last write is used for every node —
    # the satellite authenticated with the engine node's token and 401'd. Distinct
    # node names here, so this failure can only come from the token check.
    def test_shared_token_env_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            path.write_text(json.dumps({"nodes": [
                {"name": "eng", "endpoint": "https://a:8006", "node": "n1",
                 "datastore": "d", "token_env": "TEZ_PROXMOX_API_TOKEN"},
                {"name": "sat", "endpoint": "https://b:8006", "node": "n2",
                 "datastore": "d", "token_env": "TEZ_PROXMOX_API_TOKEN"}]}))
            with self.assertRaises(SystemExit) as raised:
                nodes_config.load_nodes_config(path)
        msg = str(raised.exception)
        self.assertIn("token_env", msg)
        self.assertIn("eng", msg)
        self.assertIn("sat", msg)

    def test_unique_token_env_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            path.write_text(json.dumps({"nodes": [
                {"name": "eng", "endpoint": "https://a:8006", "node": "n1",
                 "datastore": "d", "token_env": "TEZ_PROXMOX_API_TOKEN"},
                {"name": "sat", "endpoint": "https://b:8006", "node": "n2",
                 "datastore": "d", "token_env": "TEZ_PROXMOX_API_TOKEN_150"}]}))
            records, _ = nodes_config.load_nodes_config(path)
        self.assertEqual([r.token_env for r in records],
                         ["TEZ_PROXMOX_API_TOKEN", "TEZ_PROXMOX_API_TOKEN_150"])


class NodeEnvTest(unittest.TestCase):
    def test_apply_and_restore(self):
        rec = _rec("x")
        # Activation is an in-process config override, not an os.environ mutation.
        cfg = config.configure(Path("/nonexistent/tezcatlipoca.toml"))
        env = {"TOK_X": "tokval", "TEZ_PROXMOX_NODE": ""}
        with patch.dict(os.environ, env, clear=False):
            restore = nodes_config.apply_node_env(rec)
            self.assertEqual(cfg.proxmox.endpoint, rec.endpoint)
            self.assertEqual(cfg.proxmox.api_token, "tokval")
            self.assertEqual(cfg.proxmox.node, "X")
            self.assertEqual(cfg.proxmox.template_vm_id, 900)
            nodes_config.restore_node_env(restore)
            self.assertEqual(cfg.proxmox.node, "pve")

    def test_missing_token_fails_loud(self):
        rec = _rec("y")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOK_Y", None)
            with self.assertRaises(SystemExit):
                nodes_config.apply_node_env(rec)


class PlacementComputeTest(unittest.TestCase):
    def _compute(self, probes, records=None, teams=None, overrides=None, engine=None):
        records = records or [_rec("n1"), _rec("n2")]
        probes = dict(probes)
        return placement_planner.compute_placement(
            records, {}, teams or TEAMS, BOXES, 1000, "testcomp",
            team_overrides=overrides, engine_override=engine, probes=probes)

    def test_capacity_fill_spreads_by_free_mem(self):
        # team reservations: 3072 MB each. n1 has room for only 2 teams (max_teams),
        # so teams 3-4 spill to n2 — the engine lands on the node holding most teams
        # (tie -> more free RAM -> n1).
        records = [_rec("n1", max_teams=2), _rec("n2")]
        placement = self._compute({
            "n1": _probe("n1", mem_gib=100, templates=["ubuntu-fix", "alpine-fix"]),
            "n2": _probe("n2", mem_gib=8, templates=["ubuntu-fix", "alpine-fix"]),
        }, records=records)
        self.assertEqual(placement["engine_node"], "n1")
        self.assertEqual(placement["slots"], {"n1": 0, "n2": 1})
        on_n1 = [k for k, n in placement["team_nodes"].items() if n == "n1"]
        on_n2 = [k for k, n in placement["team_nodes"].items() if n == "n2"]
        self.assertEqual(len(on_n1), 2)
        self.assertEqual(len(on_n2), 2)

    def test_missing_template_blocks_node(self):
        placement = self._compute({
            "n1": _probe("n1", templates=["ubuntu-fix"]),  # alpine-fix missing
            "n2": _probe("n2", templates=["ubuntu-fix", "alpine-fix"]),
        })
        self.assertEqual(set(placement["team_nodes"].values()), {"n2"})

    def test_unreachable_node_never_used(self):
        placement = self._compute({
            "n1": _probe("n1", ok=False),
            "n2": _probe("n2", templates=["ubuntu-fix", "alpine-fix"]),
        })
        self.assertEqual(set(placement["team_nodes"].values()), {"n2"})
        self.assertEqual(placement["engine_node"], "n2")

    def test_override_pins_team_and_engine(self):
        placement = self._compute(
            {"n1": _probe("n1", templates=["ubuntu-fix", "alpine-fix"]),
             "n2": _probe("n2", templates=["ubuntu-fix", "alpine-fix"])},
            overrides={"team1": "n1", "team2": "n1"}, engine="n2")
        self.assertEqual(placement["engine_node"], "n2")
        self.assertEqual(placement["team_nodes"]["team1"], "n1")
        self.assertEqual(placement["slots"], {"n2": 0, "n1": 1})

    def test_override_by_identifier(self):
        placement = self._compute(
            {"n1": _probe("n1", templates=["ubuntu-fix", "alpine-fix"]),
             "n2": _probe("n2", templates=["ubuntu-fix", "alpine-fix"])},
            overrides={"101": "n2"})
        self.assertEqual(placement["team_nodes"]["team1"], "n2")

    def test_unknown_override_node_rejected(self):
        with self.assertRaises(SystemExit):
            self._compute(
                {"n1": _probe("n1", templates=["ubuntu-fix", "alpine-fix"]),
                 "n2": _probe("n2", templates=["ubuntu-fix", "alpine-fix"])},
                overrides={"team1": "nope"})

    def test_no_eligible_node_is_a_loud_error(self):
        with self.assertRaises(SystemExit):
            self._compute({"n1": _probe("n1", ok=False), "n2": _probe("n2", ok=False)})

    def test_satellite_carry_jump_ip_and_anchor(self):
        records = [_rec("n1", max_teams=2), _rec("n2")]
        placement = self._compute(
            {"n1": _probe("n1", mem_gib=100, templates=["ubuntu-fix", "alpine-fix"]),
             "n2": _probe("n2", mem_gib=8, templates=["ubuntu-fix", "alpine-fix"])},
            records=records)
        sats = placement["satellites"]
        self.assertEqual(len(sats), 1)
        self.assertEqual(sats[0]["name"], "n2")
        self.assertEqual(sats[0]["jump_mgmt_ip"], "10.0.0.249")  # slot 1 default (.249, .248, ...)
        self.assertTrue(sats[0]["anchor_identifier"])  # first local team's subnet

    def test_slot_vmid_math(self):
        self.assertEqual(placement_record.golden_vmid_for_slot(1000, 0, 2), 1152)
        self.assertEqual(placement_record.golden_vmid_for_slot(1000, 1, 2), 1162)
        self.assertEqual(placement_record.jump_vmid_for(1000, 1), 1131)
        self.assertEqual(placement_record.jump_vmid_for(1000, 4), 1134)


class RedAddressWriteBack(unittest.TestCase):
    def _placement(self):
        return {"version": 2, "engine_vmid": 1300, "engine_node": "n1",
                "nodes": {"n1": _rec("n1").to_json()}, "slots": {"n1": 0, "n2": 1},
                "team_nodes": {"team1": "n1"}, "team_slots": {"team1": 0},
                "team_identifiers": {"team1": "140"},
                "satellites": [{"name": "n2", "slot": 1, "teams": [], "jump_vmid": 1431,
                                "jump_mgmt_ip": "10.0.0.249", "anchor_identifier": ""}],
                "jump_mgmt_ips": {"n2": "10.0.0.249"}, "probe_summary": {},
                "red01": {"vmid": 1470, "ip": "", "node": "N1", "explicit_ip": False}}

    def test_the_selected_address_is_written_back_to_the_document(self):
        from red.red_plant_ops import select_red_ip_for
        placement = self._placement()
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            placement_record.write_placement(comp, placement)
            with patch("infra.collisions.scan_vms_for",
                       return_value=[{"vmid": 100, "node": "N1", "status": "running",
                                      "tags": ""}]), \
                    patch("infra.collisions.guest_ipv4s", return_value=["10.0.0.240"]):
                ip, _unchecked = select_red_ip_for(
                    comp, placement, engine_mgmt_ip="10.0.0.250", comp_tags={"tezcatlipoca"})
            stored = estate_state.EstateState.load(comp).data["placement"]["red01"]
        self.assertEqual(ip, "10.0.0.241")            # .240 is answered, .241 is free
        self.assertEqual(stored["ip"], "10.0.0.241")  # the address red01 was created at
        self.assertFalse(stored["explicit_ip"])
        self.assertEqual(stored["vmid"], 1470)

    def test_a_team_block_over_the_placements_own_bands_is_refused(self):
        placement = self._placement()
        # engine 1300 with one satellite: slot 1's goldens are 1460..1469, so identifier
        # 126's block (1460..1469) lands exactly on them.
        with self.assertRaises(SystemExit) as cm:
            placement_record.check_team_blocks(placement, {"team1": {"identifier": "126"}})
        self.assertIn("reserved vmids", str(cm.exception))
        placement_record.check_team_blocks(placement, {"team1": {"identifier": "140"}})


class ManagementBridge(unittest.TestCase):
    def test_a_node_record_carries_its_own_bridge(self):
        rec = nodes_config.NodeRecord.from_json(
            {**_rec("n1").to_json(), "mgmt_bridge": "vmbr100"})
        self.assertEqual(placement_record.mgmt_bridge_for(rec), "vmbr100")

    def test_without_a_node_value_the_profile_applies(self):
        self.assertEqual(placement_record.mgmt_bridge_for(_rec("n1")),
                         config.active().proxmox.mgmt_bridge or "vmbr0")
        self.assertEqual(placement_record.resolved_mgmt_bridge({"mgmt_bridge": "vmbr100"}),
                         "vmbr100")
        self.assertEqual(placement_record.resolved_mgmt_bridge(None),
                         config.active().proxmox.mgmt_bridge or "vmbr0")


class PlacementRecordTest(unittest.TestCase):
    def test_write_read_roundtrip_and_authority(self):
        placement = {"version": nodes_config.PLACEMENT_VERSION, "comp": "c",
                     "engine_vmid": 1000, "engine_node": "n1",
                     "nodes": {"n1": _rec("n1").to_json()},
                     "slots": {"n1": 0}, "team_nodes": {"team1": "n1"},
                     "team_slots": {"team1": 0},
                     "team_identifiers": {"team1": "101"},
                     "satellites": [], "jump_mgmt_ips": {}, "probe_summary": {}}
        with tempfile.TemporaryDirectory() as td:
            placement_record.write_placement(Path(td), placement)
            back = placement_record.read_placement(Path(td))
        self.assertEqual(back["engine_node"], "n1")

    def test_a_v1_document_resolves_red01_the_old_way_and_writes_it_back(self):
        placement = {"version": 1, "comp": "c", "engine_vmid": 2000, "engine_node": "n1",
                     "nodes": {"n1": _rec("n1").to_json()},
                     "slots": {"n1": 0}, "team_nodes": {"team1": "n1"},
                     "team_slots": {"team1": 0},
                     "team_identifiers": {"team1": "101"},
                     "satellites": [], "jump_mgmt_ips": {}, "probe_summary": {}}
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            (comp / "Compfile").write_text("name c\nred01_vmid 2222\n")
            placement_record.write_placement(comp, placement)
            back = placement_record.read_placement(comp)
            document = estate_state.EstateState.load(comp)
        self.assertEqual(back["version"], 2)
        self.assertEqual(back["red01"]["vmid"], 2222)          # Compfile wins, as v1 did
        self.assertEqual(back["golden_vmids"], {"n1": list(range(2150, 2160))})
        self.assertEqual(back["engine_template_vmid"], 2140)
        stored = document.data["placement"]
        self.assertEqual((stored["version"], stored["red01"]["vmid"]), (2, 2222))

    def test_a_v1_document_without_an_override_derives_engine_plus_170(self):
        placement = {"version": 1, "comp": "c", "engine_vmid": 2000, "engine_node": "n1",
                     "nodes": {"n1": _rec("n1").to_json()},
                     "slots": {"n1": 0}, "team_nodes": {"team1": "n1"},
                     "team_slots": {"team1": 0},
                     "team_identifiers": {"team1": "101"},
                     "satellites": [], "jump_mgmt_ips": {}, "probe_summary": {}}
        with tempfile.TemporaryDirectory() as td:
            placement_record.write_placement(Path(td), placement)
            back = placement_record.read_placement(Path(td))
        self.assertEqual(back["red01"], {"vmid": 2170, "ip": "10.0.0.198", "node": "N1",
                                         "explicit_ip": False})

    def test_record_identities_writes_the_whole_block_once(self):
        placement = {"version": 2, "comp": "c", "engine_vmid": 2000, "engine_node": "n1",
                     "nodes": {"n1": _rec("n1").to_json(), "n2": _rec("n2").to_json()},
                     "slots": {"n1": 0, "n2": 1},
                     "team_nodes": {"team1": "n1", "team2": "n2"},
                     "team_slots": {"team1": 0, "team2": 1},
                     "team_identifiers": {"team1": "101", "team2": "102"},
                     "satellites": [{"name": "n2", "slot": 1, "teams": ["team2"],
                                     "jump_vmid": 2131, "jump_mgmt_ip": "10.0.0.249",
                                     "anchor_identifier": "102"}],
                     "jump_mgmt_ips": {"n2": "10.0.0.249"}, "probe_summary": {},
                "red01": {"vmid": 1470, "ip": "", "node": "N1", "explicit_ip": False}}
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            placement_record.record_placement_identities(
                comp, placement, "10.0.0.230", "10.0.0.1",
                red01={"vmid": 2160, "ip": "", "node": "pve1", "explicit_ip": False})
            stored = estate_state.EstateState.load(comp).data["placement"]
        self.assertEqual(stored["engine_mgmt_ip"], "10.0.0.230")
        self.assertEqual(stored["engine_mgmt_gw"], "10.0.0.1")
        self.assertEqual(stored["mgmt_bridge"], "vmbr0")
        self.assertEqual(stored["engine_template_vmid"], 2140)
        self.assertEqual(stored["golden_vmids"]["n2"], list(range(2160, 2170)))
        self.assertEqual(stored["red01"]["vmid"], 2160)

    def test_bad_version_refused(self):
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td)
            document = estate_state.EstateState.load(comp)
            document.data["placement"] = {"version": 99}
            document.save()
            with self.assertRaises(SystemExit):
                placement_record.read_placement(comp)

    def test_resolve_legacy_when_nothing_configured(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(placement_planner, "load_nodes_config", return_value=(None, None)):
            placement, rec = placement_planner.resolve_placement(Path(td), 1000, TEAMS, BOXES, "c")
        self.assertIsNone(placement)
        self.assertIsNone(rec)

    def test_resolve_reads_the_explicit_nodes_path(self):
        probes = {"n1": _probe("n1", templates=["ubuntu-fix", "alpine-fix"])}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "run-nodes.json"
            path.write_text(json.dumps({"nodes": [_rec("n1").to_json()]}))
            with patch.object(placement_planner, "probe_node",
                              side_effect=lambda r, *a, **k: probes[r.name]):
                placement, _engine_rec = placement_planner.resolve_placement(
                    Path(td), 1000, TEAMS, BOXES, "c", engine_override="n1", nodes_path=path)
        self.assertEqual(placement["engine_node"], "n1")

    def test_pin_without_a_nodes_list_is_refused(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(placement_planner, "load_nodes_config", return_value=(None, None)):
            with self.assertRaises(SystemExit) as raised:
                placement_planner.resolve_placement(Path(td), 1000, TEAMS, BOXES, "c",
                                                    engine_override="node-x")
        self.assertIn("--nodes", str(raised.exception))

    def test_resume_adopts_deployed_endpoint(self):
        records = [_rec("n1"), _rec("n2")]
        probes = {"n1": _probe("n1", templates=["ubuntu-fix", "alpine-fix"]),
                  "n2": _probe("n2", templates=["ubuntu-fix", "alpine-fix"])}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(placement_planner, "load_nodes_config", return_value=(records, {})), \
                patch.object(placement_planner, "probe_node", side_effect=lambda r, *a, **k: probes[r.name]):
            placement, rec = placement_planner.resolve_placement(
                Path(td), 1000, TEAMS, BOXES, "c",
                resume_endpoint="https://n2.lab:8006")
            self.assertEqual(placement["engine_node"], "n2")
            self.assertEqual(set(placement["team_nodes"].values()), {"n2"})
            self.assertTrue(placement_record.read_placement(Path(td)),
                        "the placement record is in the document")

    def test_resume_refuses_unknown_endpoint(self):
        records = [_rec("n1")]
        with tempfile.TemporaryDirectory() as td, \
                patch.object(placement_planner, "load_nodes_config", return_value=(records, {})):
            with self.assertRaises(SystemExit):
                placement_planner.resolve_placement(Path(td), 1000, TEAMS, BOXES, "c",
                                            resume_endpoint="https://elsewhere:8006")

    def test_existing_placement_wins_over_overrides(self):
        placement = {"version": nodes_config.PLACEMENT_VERSION, "comp": "c",
                     "engine_vmid": 1000, "engine_node": "n1",
                     "nodes": {"n1": _rec("n1").to_json()},
                     "slots": {"n1": 0},
                     "team_nodes": {k: "n1" for k in TEAMS},
                     "team_slots": {k: 0 for k in TEAMS},
                     "team_identifiers": {k: TEAMS[k]["identifier"] for k in TEAMS},
                     "satellites": [], "jump_mgmt_ips": {}, "probe_summary": {}}
        with tempfile.TemporaryDirectory() as td:
            placement_record.write_placement(Path(td), placement)
            back, rec = placement_planner.resolve_placement(Path(td), 1000, TEAMS, BOXES, "c",
                                                    team_overrides={"team1": "x"})
        self.assertEqual(back["engine_node"], "n1")


    def test_routes_cover_every_team_subnet_behind_the_jump(self):
        placement = {"team_identifiers": {"team1": "103", "team2": "107"},
                     "satellites": [
            {"name": "n2", "slot": 1, "jump_mgmt_ip": "10.0.0.248",
             "anchor_identifier": "103", "teams": ["team1", "team2"]}]}
        self.assertEqual(placement_record.satellite_routes_for(placement),
                         [{"subnet": "192.168.103.0/24", "via": "10.0.0.248"},
                          {"subnet": "192.168.107.0/24", "via": "10.0.0.248"}])


class DeployBannerTest(unittest.TestCase):
# The entry banner must not name an estate before placement resolves it. On a
# nodes.json-only run the ambient config has no endpoint/template, so the old banner
# printed endpoint=? node=pve datastore=local-lvm tpl=? engine-mgmt=default — a first-time
# operator read that as the target host mid-deploy.

    def test_entry_banner_prints_no_unresolved_estate_values(self):
        from deploy_lib import inputs as dl_inputs
        from deploy_lib.context import DeployContext
        with tempfile.TemporaryDirectory() as td:
            comp = Path(td) / "probe"
            comp.mkdir()
            (comp / "Compfile").write_text("name probe\nscenario s\ndifficulty 1\n")
            (comp / "users.json").write_text(json.dumps(
                {"box_username": "ubuntu", "credlist_usernames": ["admin"]}))
            (comp / "boxes.json").write_text(json.dumps(
                [{"name": "web01", "template": "ubuntu-fix"}]))
            config.reset()
            self.addCleanup(config.reset)
            buf = io.StringIO()
            with patch.dict(os.environ, {"TEZ_CONFIG": str(Path(td) / "absent.toml")},
                            clear=True), contextlib.redirect_stdout(buf):
                dl_inputs.load_competition_spec(DeployContext(), comp)
        out = buf.getvalue()
        self.assertIn("Deploying probe", out)
        self.assertNotIn("=?", out)
        self.assertNotIn("engine-mgmt=", out)


class JumpRulesTest(unittest.TestCase):
    GOLDEN = """*filter
:INPUT ACCEPT
:FORWARD DROP
:OUTPUT ACCEPT
-A FORWARD -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A FORWARD -s 10.0.0.0/24 -d 192.168.103.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
-A FORWARD -s 10.0.0.0/24 -d 192.168.104.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
-A FORWARD -s 10.0.0.250/32 -d 192.168.103.0/24 -j ACCEPT
-A FORWARD -s 10.0.0.250/32 -d 192.168.104.0/24 -j ACCEPT
-A FORWARD -s 192.168.103.0/24 -d 10.0.0.250/32 -j ACCEPT
-A FORWARD -s 192.168.104.0/24 -d 10.0.0.250/32 -j ACCEPT
-A FORWARD -s 192.168.103.0/24 ! -d 192.168.0.0/16 -j ACCEPT
-A FORWARD -s 192.168.104.0/24 ! -d 192.168.0.0/16 -j ACCEPT
COMMIT
*nat
-A PREROUTING -d 192.168.103.1/32 -p tcp --dport 3142 -j DNAT --to-destination 10.0.0.250:3142
-A PREROUTING -d 192.168.104.1/32 -p tcp --dport 3142 -j DNAT --to-destination 10.0.0.250:3142
-A POSTROUTING -s 10.0.0.250/32 -d 192.168.103.0/24 -j SNAT --to-source 192.168.103.1
-A POSTROUTING -s 10.0.0.250/32 -d 192.168.104.0/24 -j SNAT --to-source 192.168.104.1
-A POSTROUTING -s 192.168.103.0/24 -d 10.0.0.250/32 -j ACCEPT
-A POSTROUTING -s 192.168.103.0/24 ! -d 192.168.0.0/16 -j MASQUERADE
-A POSTROUTING -s 192.168.104.0/24 -d 10.0.0.250/32 -j ACCEPT
-A POSTROUTING -s 192.168.104.0/24 ! -d 192.168.0.0/16 -j MASQUERADE
COMMIT
"""

    def test_ruleset_matches_golden(self):
        self.assertEqual(jump_ops.jump_rules([103, 104], "10.0.0.250"), self.GOLDEN)


class EnumerateTargetsTest(unittest.TestCase):
    def test_legacy_stamp_and_multinode_split(self):
        targets = enumerate_targets(TEAMS, BOXES, default_node="engNode")
        self.assertTrue(all(t["node"] == "engNode" for t in targets))
        placement = {"team_nodes": {"team1": "n1", "team2": "n2"},
                     "team_slots": {"team1": 0, "team2": 1}}
        targets = enumerate_targets(TEAMS, BOXES, placement=placement, default_node="n1")
        by_team = {t["team_key"]: t["node"] for t in targets}
        self.assertEqual(by_team, {"team1": "n1", "team2": "n2",
                                   "team3": "n1", "team4": "n1"})
        slots = {t["team_key"]: t["slot"] for t in targets}
        self.assertEqual(slots["team2"], 1)


# Routed red -> satellite teams (a scale soak caught it): red01 sat on its own segment
# (10.200.0.0/24) with the engine as its gateway, and the jump's FORWARD policy is DROP
# with accepts only for the engine and team egress, so red's source matched nothing —
# all 15 cred_sprays + 2 db_attacks against satellite teams failed "unreachable over
# SSH", 0 footholds, and half the range was never attackable. `TEZ_RED_SEGMENT` adds the
# path; empty must stay a no-op.
class RedSegmentRulesTest(unittest.TestCase):

    RED = "10.200.0.0/24"

    def rules(self, red=None):
        return jump_ops.jump_rules([103, 104], "10.0.0.250",
                                   red_segment=self.RED if red is None else red)

    def test_red_reaches_each_local_team(self):
        rules = self.rules()
        for t in (103, 104):
            self.assertIn(f"-A FORWARD -s {self.RED} -d 192.168.{t}.0/24 -j ACCEPT", rules)

    def test_reply_to_red_is_accepted(self):
        # The jump is the teams' gateway, so a reply to red leaves via the mgmt default
        # route unless this accept matches FIRST — nothing upstream filters it.
        rules = self.rules()
        for t in (103, 104):
            self.assertIn(f"-A FORWARD -s 192.168.{t}.0/24 -d {self.RED} -j ACCEPT", rules)

    def test_red_is_snat_to_the_team_gateway(self):
        # Boxes trust SSH only from 192.168.<id>.1, so red must arrive as the gateway
        # or it gets refused even with the FORWARD accept in place.
        rules = self.rules()
        for t in (103, 104):
            self.assertIn(f"-A POSTROUTING -s {self.RED} -d 192.168.{t}.0/24 "
                          f"-j SNAT --to-source 192.168.{t}.1", rules)

    def test_red_snat_precedes_team_egress_masquerade(self):
        # nat POSTROUTING is first-match: if the team-egress MASQUERADE won, red's own
        # source would be rewritten twice and arrive as the jump's mgmt address.
        lines = self.rules().splitlines()
        red_snat = lines.index(f"-A POSTROUTING -s {self.RED} -d 192.168.103.0/24 "
                               f"-j SNAT --to-source 192.168.103.1")
        masq = next(i for i, l in enumerate(lines)
                    if l.startswith("-A POSTROUTING -s 192.168.103.0/24 ! -d"))
        self.assertLess(red_snat, masq)

    def test_red_does_not_break_team_isolation(self):
        rules = self.rules()
        self.assertNotIn("192.168.103.0/24 -d 192.168.104", rules)
        self.assertIn(":FORWARD DROP", rules)

    def test_empty_segment_emits_no_red_rules(self):
        rules = self.rules(red="")
        self.assertNotIn("10.200.0.0/24", rules)
        for t in (103, 104):
            self.assertNotIn(f"192.168.{t}.0/24 -d 10.200.0.0/24", rules)

    def test_host_bits_and_open_form_normalise(self):
        # A /24 written as a host address must still produce the canonical network.
        self.assertIn(f"-A FORWARD -s {self.RED} -d 192.168.103.0/24 -j ACCEPT",
                      self.rules(red="10.200.0.10/24"))

    def test_mss_is_clamped_towards_red_and_the_engine(self):
        # This VM is the first routed hop between two /24s; without the clamp a path
        # MTU below 1500 black-holes large replies (the soak's SSH failures rode the
        # same hop as its successful MySQL probes).
        rules = self.rules()
        for zone in ("10.0.0.0/24", self.RED):
            self.assertIn(f"-A FORWARD -s {zone} -d 192.168.103.0/24 -p tcp "
                          f"--tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu", rules)


# TEZ_RED_SEGMENT parsing: empty is the safe default, a typo is a hard error
class RedSegmentFromEnvTest(unittest.TestCase):

    def test_unset_and_blank_are_empty(self):
        os.environ.pop("TEZ_RED_SEGMENT", None)
        self.assertEqual(jump_ops.red_segment_from_env(), "")
        with patch.dict("os.environ", {"TEZ_RED_SEGMENT": "   "}):
            self.assertEqual(jump_ops.red_segment_from_env(), "")

    def test_normalises_host_bits(self):
        with patch.dict("os.environ", {"TEZ_RED_SEGMENT": "10.200.0.10/24"}):
            self.assertEqual(jump_ops.red_segment_from_env(), "10.200.0.0/24")

    def test_malformed_is_fatal_not_silent(self):
        # A silently-dropped segment puts a live range back in the state that lost the
        # soak's entire red coverage, so it must refuse to deploy instead. `10.200.0.0`
        # parses as /32 — a plausible typo that would match only red01 itself.
        with patch.dict("os.environ", {"TEZ_RED_SEGMENT": "10.200.0.0"}):
            self.assertEqual(jump_ops.red_segment_from_env(), "10.200.0.0/32")
        with patch.dict("os.environ", {"TEZ_RED_SEGMENT": "red-team"}):
            with self.assertRaises(SystemExit):
                jump_ops.red_segment_from_env()


class ParseTeamNodeTest(unittest.TestCase):
    def test_shapes(self):
        self.assertIsNone(dl_placement.parse_team_node(None))
        self.assertEqual(dl_placement.parse_team_node("103=n2,team4=n1"),
                         {"103": "n2", "team4": "n1"})
        with self.assertRaises(SystemExit):
            dl_placement.parse_team_node("103-n2")


class ActivatePlacementTest(unittest.TestCase):
    def test_routes_keyed_by_pve_host_name(self):
        from infra.client import _NODE_ROUTES
        placement = {
            "engine_node": "hdd-150",
            "nodes": {"hdd-150": _rec("hdd-150", node="proxmox",
                                      token_env="TOK_HDD150").to_json(),
                      "zfs-193": _rec("zfs-193", node="pve",
                                      token_env="TOK_ZFS193").to_json()},
        }
        with patch.dict(os.environ, {"TOK_HDD150": "t1", "TOK_ZFS193": "t2"}):
            restore = placement_record.activate_placement(placement)
            try:
                self.assertEqual(set(_NODE_ROUTES), {"proxmox", "pve"})
                self.assertEqual(_NODE_ROUTES["pve"],
                                 ("https://zfs-193.lab:8006", "t2"))
            finally:
                placement_record.deactivate_placement(restore)
        self.assertEqual(_NODE_ROUTES, {})


class ActivationProjectsBackToEnv(unittest.TestCase):
    """activate_placement moves the process by mutating the CONFIG, not os.environ.

    A teardown caller snapshots os.environ before activation and hands that dict to the steps
    and to bad-auto's subprocess. Copying os.environ back is now a no-op, which silently sent
    a multi-node teardown at the profile's host (the wrong-PVE bug). The identity
    must come from the config.
    """

    def test_proxmox_env_carries_the_activated_identity(self):
        from estate import config
        placement = {
            "engine_node": "hdd-150",
            "nodes": {"hdd-150": _rec("hdd-150", node="proxmox",
                                      token_env="TOK_HDD150").to_json()},
        }
        with patch.dict(os.environ,
                        {"TOK_HDD150": "t1", "TEZ_PROXMOX_NODE": "profile-host"},
                        clear=False):
            restore = placement_record.activate_placement(placement)
            try:
                env = config.proxmox_env()
            finally:
                placement_record.deactivate_placement(restore)
        # The placement's node, not the profile's: this is what a stale os.environ copy got wrong.
        self.assertEqual(env["TEZ_PROXMOX_NODE"], "proxmox")
        self.assertEqual(env["TEZ_PROXMOX_API_TOKEN"], "t1")


class SyncPlannerTest(unittest.TestCase):
    def test_plan_commands_and_vmid_pick(self):
        from multinode.template_sync_ops import plan_sync
        src, dst = _rec("src"), _rec("dst")
        with patch("multinode.template_sync_ops.find_template_vmid", return_value=955), \
                patch("multinode.template_sync_ops.pick_destination_vmid", return_value=1120):
            plan = plan_sync(src, dst, "base-ubuntu24.04-fix")
        self.assertEqual(plan["src_vmid"], 955)
        self.assertEqual(plan["dst_vmid"], 1120)
        self.assertEqual(plan["commands"][0], ["ssh", "root@src.lab",
                                               "vzdump 955 --stdout --compress zstd"])
        self.assertEqual(plan["commands"][1], ["ssh", "root@dst.lab",
                                               "qmrestore - 1120 --storage ds-dst"])


class MultinodePreflightResumeTest(unittest.TestCase):
    def test_in_path_firewall_refused_on_satellite_teams(self):
        # The jump VM impersonates 192.168.<id>.1 on each satellite; an in-path
        # firewall there would fight it for the gateway address. Refusal fires before
        # any API call, so no stubbing is needed.
        from multinode.nodes_config import PLACEMENT_VERSION
        placement = {
            "version": PLACEMENT_VERSION, "engine_node": "n1", "engine_vmid": 1000,
            "nodes": {"n1": _rec("n1").to_json()},
            "slots": {"n1": 0},
            "team_nodes": {k: "n1" for k in TEAMS},
            "team_slots": {k: 1 for k in TEAMS},
            "team_identifiers": {k: TEAMS[k]["identifier"] for k in TEAMS},
            "satellites": [], "jump_mgmt_ips": {}, "probe_summary": {},
        }
        fw = {"name": "fw01", "last_octet": 1, "template": "pfsense",
              "unmanaged": True, "in_path": True}
        with self.assertRaises(SystemExit) as cm:
            run_preflight(multinode_plan(
                Path("competitions/example"), [fw], TEAMS, 1000, placement,
                engine_mgmt_ip=None, check_free=False, our_run_tag=None))
        self.assertIn("in_path firewalls are only supported on engine-node", str(cm.exception))

    def test_check_free_false_skips_collisions(self):
        from multinode.nodes_config import PLACEMENT_VERSION
        placement = {
            "version": PLACEMENT_VERSION, "engine_node": "n1", "engine_vmid": 1000,
            "nodes": {"n1": _rec("n1").to_json(), "n2": _rec("n2").to_json()},
            "slots": {"n1": 0, "n2": 1},
            "team_nodes": {k: "n2" for k in TEAMS},
            "team_slots": {k: 1 for k in TEAMS},
            "team_identifiers": {k: TEAMS[k]["identifier"] for k in TEAMS},
            "satellites": [], "jump_mgmt_ips": {}, "probe_summary": {},
        }
        boxes = [{"name": "web01", "template": "ubuntu-fix", "memory_mb": 512}]
        teams = TEAMS
        # Colliding VMs on n2 would fatal under check_free=True; must pass on resume.
        # proxmox_api is stubbed because the template preflight reads each template's
        # config for the cloud-init gate (test_template_cloudinit covers that gate).
        with patch.dict(os.environ, {"TOK_N1": "t", "TOK_N2": "t"}), \
                patch.object(placement_record, "record_of", side_effect=lambda pl, n: _rec(n)), \
                patch.object(placement_record, "teams_on_node",
                             side_effect=lambda pl, n: [k for k, v in pl["team_nodes"].items() if v == n]), \
                patch.object(client, "cluster_vms_for",
                             return_value=[{"vmid": 1700, "node": "N2", "name": "foreign",
                                            "tags": "x", "status": "stopped"},
                                           {"vmid": 900, "node": "N1", "name": "engine-base",
                                            "tags": "template", "template": 1,
                                            "status": "stopped"},
                                           {"vmid": 901, "node": "N2", "name": "ubuntu-fix",
                                            "tags": "template", "template": 1,
                                            "status": "stopped"}]), \
                patch("infra.ownership.has_clone_marker", return_value=False), \
                patch("infra.resources.proxmox_api",
                      return_value={"data": {"ostype": "l26",
                                             "ide2": "local:vm-901-cloudinit"}}), \
                patch("infra.collisions.proxmox_api",
                      return_value={"data": [{"iface": "vmbr0"}]}), \
                patch("infra.resources.proxmox_api", return_value={"data": {"avail": 1 << 50}}), \
                patch("infra.gates.remote_access_gate"), \
                patch("infra.gates.catalog_gate"), \
                patch("net.jump_ops.find_jump_template", return_value={"alpine": 900}):
            run_preflight(multinode_plan(
                Path("competitions/example"), boxes, teams, 1000, placement,
                engine_mgmt_ip=None, check_free=False, our_run_tag=None))


if __name__ == "__main__":
    unittest.main()
