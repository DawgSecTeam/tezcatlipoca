"""Multi-node pipeline, offline and integrated: a synthetic 3-node nodes.json through
resolve_placement (real probes against a fake Proxmox), the REAL multinode preflight over the
same fake, build_terraform_inputs' satellite tfvars, and the jump ruleset for every satellite.

The unit tests in test_multinode.py stub each layer separately; this one wires them together
the way a deploy does, so a facade/module-qualification slip between layers shows up here."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from net import jump_rules  # noqa: E402

from multinode import placement_planner  # noqa: E402
from multinode import placement_record  # noqa: E402
from infra import client  # noqa: E402
from deploy_lib.context import DeployContext  # noqa: E402
from deploy_lib import estate_inputs  # noqa: E402
from deploy_lib.phases._estate import slot_team_identifiers  # noqa: E402
from estate import state as estate_state  # noqa: E402
from infra import gates as gate  # noqa: E402
from infra.plan import multinode_plan  # noqa: E402

NODES = {
    "nodes": [
        {"name": "n1", "endpoint": "https://n1.lab:8006", "node": "pve1", "datastore": "ds1",
         "token_env": "TOK_N1", "engine_base_vmid": 955, "engine_mgmt_ip": "10.0.0.250",
         "engine_mgmt_gw": "10.0.0.1", "max_teams": 2},
        {"name": "n2", "endpoint": "https://n2.lab:8006", "node": "pve2", "datastore": "ds2",
         "token_env": "TOK_N2", "engine_base_vmid": 955, "jump_mgmt_ip": "10.0.0.249"},
        {"name": "n3", "endpoint": "https://n3.lab:8006", "node": "pve3", "datastore": "ds3",
         "token_env": "TOK_N3", "engine_base_vmid": 955},
    ],
    "balancing": {},
}
BOXES = [
    {"name": "web01", "last_octet": 2, "cpu": 1, "memory_mb": 2048, "template": "ubuntu-fix"},
    {"name": "app01", "last_octet": 3, "cpu": 1, "memory_mb": 1024, "template": "alpine-fix"},
]
TEAMS = {f"team{i}": {"identifier": str(100 + i), "password": "pw"} for i in range(1, 6)}


def _templates(node, base=True):
    out = [{"vmid": 910, "node": node, "name": "ubuntu-fix", "template": 1, "tags": "template",
            "status": "stopped"},
           {"vmid": 911, "node": node, "name": "alpine-fix", "template": 1, "tags": "template",
            "status": "stopped"}]
    if base:
        out.append({"vmid": 955, "node": node, "name": "engine-base", "template": 1,
                    "tags": "template", "status": "stopped"})
    return out


# endpoint -> VM list; answers just the paths the probe/preflight/jump code reads
class FakeProxmox:

    def __init__(self, vms_by_endpoint):
        self.vms = vms_by_endpoint
        self.calls = []

    def __call__(self, endpoint, token, method, path, **kwargs):
        self.calls.append((method, endpoint, path))
        assert token, f"empty token for {endpoint}"
        if path == "/cluster/resources":
            return {"data": self.vms[endpoint]}
        if path.endswith("/status") and "/storage/" not in path:
            return {"data": {"memory": {"free": 64 << 30}}}
        if "/storage/" in path:
            return {"data": {"avail": 1 << 50}}
        if path.endswith("/network"):
            return {"data": [{"iface": "vmbr0"}]}
        if "/config" in path:
            return {"data": {"ostype": "l26", "ide2": "local:vm-1-cloudinit"}}
        raise AssertionError(f"unexpected API path {path}")


class MultinodeOfflineE2E(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.comp = self.root / "competitions" / "mn"
        self.comp.mkdir(parents=True)
        (self.root / "nodes.json").write_text(json.dumps(NODES))
        self.cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.cwd)
        env = {"TOK_N1": "u@pam!a=1", "TOK_N2": "u@pam!b=2", "TOK_N3": "u@pam!c=3",
               "TF_VAR_proxmox_endpoint": "https://n1.lab:8006",
               "TF_VAR_proxmox_api_token": "u@pam!a=1", "TF_VAR_proxmox_node": "pve1",
               "TF_VAR_datastore": "ds1", "TF_VAR_template_vm_id": "955",
               "TF_VAR_engine_mgmt_ip": "10.0.0.250",  # hermetic against a .env-loaded value
               "TF_VAR_ssh_private_key_path": str(self.root / "key")}
        p = patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(client.clear_node_routes)
        vms = {f"https://{n}.lab:8006": _templates(f"pve{i}", base=(n == "n1"))
               for i, n in ((1, "n1"), (2, "n2"), (3, "n3"))}
        self.fake = FakeProxmox(vms)

    def _resolve(self, **kw):
        with patch.object(client, "proxmox_api_for", self.fake):
            return placement_planner.resolve_placement(self.comp, 1000, TEAMS, BOXES, "mn", **kw)

    def test_placement_preflight_tfvars_and_jump_rules(self):
        placement, engine = self._resolve()
        self.assertEqual(set(placement["team_nodes"]), set(TEAMS))
        self.assertTrue(placement_record.read_placement(self.comp),
                        "the placement record is in the document")
        self.assertEqual(engine.name, placement["engine_node"])
        # n1 is capped at 2 teams, so a 5-team comp must use satellites
        used = set(placement["team_nodes"].values())
        self.assertGreaterEqual(len(used), 3)
        sat_slots = sorted(s for s in placement["slots"].values() if s)
        self.assertEqual(sat_slots, [1, 2])

        # resolve again: placement.json is authoritative, no new probe traffic
        n_calls = len(self.fake.calls)
        again, _ = self._resolve()
        self.assertEqual(again["team_nodes"], placement["team_nodes"])
        self.assertEqual(len(self.fake.calls), n_calls)

        # the real multi-node preflight over the same fake estate
        placement_record.activate_placement(placement)
        self.addCleanup(placement_record.deactivate_placement, {})
        with patch.object(client, "proxmox_api_for", self.fake), \
                patch("infra.gates.remote_access_gate"), \
                patch("infra.gates.catalog_gate"), \
                patch("infra.ownership.has_clone_marker", return_value=False):
            gate.run_preflight(multinode_plan(self.comp, BOXES, TEAMS, 1000, placement,
                                              engine_mgmt_ip="10.0.0.250", check_free=True,
                                              our_run_tag=None))

        # the estate path reads the placement directly: slot 0's teams are the engine's NICs
        ctx = DeployContext(name="mn", box_username="ubuntu", comp_name="mn", boxes=BOXES,
                            teams=TEAMS, box_password="bp", state={}, run_id="run-0badf00d",
                            engine_vmid=1000, placement=placement)
        ctx.engine_mgmt_ip = estate_inputs.engine_mgmt_ip_from_env()  # as prepare() does
        estate_inputs.build_estate_inputs(ctx, self.comp)
        slot0 = {t["identifier"] for k, t in TEAMS.items()
                 if placement["team_slots"][k] == 0}
        self.assertEqual(set(slot_team_identifiers(ctx)), slot0)
        self.assertEqual(ctx.engine_mgmt_ip, "10.0.0.250")
        # The routes are computed at engine-network time; every satellite team subnet must
        # be reachable through its jump.
        from multinode.placement_record import satellite_routes_for
        routes = {r["subnet"] for r in satellite_routes_for(placement)}
        for sat in placement["satellites"]:
            for key in sat["teams"]:
                self.assertIn(f"192.168.{TEAMS[key]['identifier']}.0/24", routes)
        mode = estate_state.state_path(self.comp).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)  # holds tokens + passwords

        # jump rulesets for each satellite only cover that satellite's teams
        checked = 0
        for sat in placement["satellites"]:
            idents = [TEAMS[k]["identifier"] for k in sat["teams"]]
            rules = jump_rules.jump_rules(idents, "10.0.0.250")
            for ident in idents:
                self.assertIn(f"192.168.{ident}.0/24", rules)
            for other in set(TEAMS[k]["identifier"] for k in TEAMS) - set(idents):
                self.assertNotIn(f"192.168.{other}.0/24", rules)
            self.assertIn(":FORWARD DROP", rules)
            checked += 1
        self.assertEqual(checked, 2)

    def test_missing_token_env_fails_loud(self):
        placement, _ = self._resolve()
        with patch.dict(os.environ, {"TOK_N2": ""}):
            with self.assertRaises(SystemExit):
                placement_record.activate_placement(placement)


if __name__ == "__main__":
    unittest.main()
