"""Unbooted-DC golden contract: stage configs and golden build/convert for DC roles.

A DC-role box gets NO golden machine (its otherwise golden-stage configs ride the
per-team repair stage, still pre-domain), and its golden is a FULL clone of the
generalized base converted without ever booting — a dead prior attempt is destroyed
and recloned, never reused, and a selective rebuild touches only unconverted slots.
All Proxmox/Nakon boundaries are mocked; no API calls."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import golden_ops
import nakon_ops

TEAMS = {"team1": {"identifier": 104}, "team2": {"identifier": 105}}
BOXES = [{"name": "dc01", "template": "windows-server-2022"},
         {"name": "web01", "template": "ubuntu-2204-web"}]
BOX_BY_GOLDEN_VMID = {1150: "dc01", 1151: "web01"}

DC_GOLDEN_CONFIG = {"name": "install-ad-misconfig", "vars": {}}


def _config_names(machine):
    return [c if isinstance(c, str) else c["name"] for c in machine["configurations"]]


class GenerateStageConfigsTests(unittest.TestCase):
    def test_dc_golden_stage_moves_to_repair_per_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            machines = [
                {"name": "dc01-team1", "ip": "192.168.104.6",
                 "configurations": [dict(DC_GOLDEN_CONFIG)]},
                {"name": "dc01-team2", "ip": "192.168.105.6",
                 "configurations": [dict(DC_GOLDEN_CONFIG)]},
                {"name": "web01-team1", "ip": "192.168.104.11",
                 "configurations": ["install-nginx", "writable-sudoers",
                                    "resolv-conf-null-dns"]},
                {"name": "web01-team2", "ip": "192.168.105.11",
                 "configurations": ["install-nginx", "writable-sudoers",
                                    "resolv-conf-null-dns"]},
            ]
            (comp_dir / "nakon-config.json").write_text(
                json.dumps({"machines": machines}))
            golden_p, repair_p, final_p, _ = nakon_ops.generate_stage_configs(
                comp_dir, TEAMS, BOXES, unbooted={"dc01"})
            golden = json.loads(golden_p.read_text())["machines"]
            repair = json.loads(repair_p.read_text())["machines"]
            final = json.loads(final_p.read_text())["machines"]

            self.assertEqual([m["name"] for m in golden], ["web01-golden"])
            self.assertEqual(golden[0]["ip"], "192.168.104.241")
            self.assertEqual(_config_names(golden[0]), ["install-nginx"])

            repair_by_name = {m["name"]: _config_names(m) for m in repair}
            self.assertEqual(repair_by_name["dc01-team1"], ["install-ad-misconfig"])
            self.assertEqual(repair_by_name["dc01-team2"], ["install-ad-misconfig"])
            self.assertEqual(repair_by_name["web01-team1"], ["writable-sudoers"])
            self.assertEqual(repair_by_name["web01-team2"], ["writable-sudoers"])

            self.assertEqual({m["name"] for m in final},
                             {"web01-team1", "web01-team2"})
            self.assertFalse(any("dc01" in m["name"] for m in final))


class BuildGoldenSetTests(unittest.TestCase):
    def _build(self, vm_exists, is_template, hashes, unbooted, boxes=BOXES,
               template_map=None):
        template_map = template_map or {"windows-server-2022": 955,
                                        "ubuntu-2204-web": 956}
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            with patch.object(golden_ops, "proxmox_api") as p_api, \
                 patch.object(golden_ops, "_vm_exists",
                              side_effect=lambda n, v: vm_exists.get(v, False)), \
                 patch.object(golden_ops, "_is_template",
                              side_effect=lambda n, v: is_template.get(v, False)), \
                 patch.object(golden_ops, "_template_vmid_map",
                              return_value=template_map), \
                 patch.object(golden_ops, "destroy_vm_if_exists") as p_destroy, \
                 patch.object(golden_ops, "wait_for_proxmox_task"), \
                 patch.object(golden_ops, "write_template_hash") as p_hash, \
                 patch.object(golden_ops, "stored_template_hash",
                              side_effect=lambda n, v: hashes[BOX_BY_GOLDEN_VMID[v]]), \
                 patch.object(golden_ops, "start_vm") as p_start, \
                 patch.object(golden_ops, "take_snapshot") as p_snap, \
                 patch.object(golden_ops, "run_nakon") as p_nakon, \
                 patch.object(golden_ops, "build_nakon_bundle") as p_bundle, \
                 patch.object(golden_ops, "prep_apt_on_boxes"), \
                 patch.object(golden_ops, "ssh_via_gateway"), \
                 patch.object(golden_ops, "wait_for_boxes_ssh"), \
                 patch.object(golden_ops, "wait_for_cloud_init"):
                result = golden_ops.build_golden_set(
                    "node", TEAMS, boxes, {}, comp_dir, 1000, "pw",
                    comp_dir / "nakon-golden.json", None, "ops", "10.0.0.9",
                    golden_hashes=hashes, unbooted=unbooted)
        return result, SimpleNamespace(api=p_api, destroy=p_destroy, hash=p_hash,
                                       start=p_start, snap=p_snap, nakon=p_nakon)

    def test_dc_golden_full_cloned_and_converted_without_booting(self):
        dc_only = [BOXES[0]]
        result, ctx = self._build({}, {}, {"dc01": "h1"}, {"dc01"}, boxes=dc_only)
        self.assertEqual(result, {"dc01": 1150})
        calls = [(c.args[0], c.args[1], c.kwargs.get("data"))
                 for c in ctx.api.call_args_list]
        clone = [d for m, p, d in calls if m == "POST" and p == "/nodes/node/qemu/955/clone"]
        self.assertEqual(len(clone), 1)
        self.assertEqual({k: clone[0][k] for k in ("newid", "name", "full")},
                         {"newid": 1150, "name": "golden-dc01", "full": 1})
        self.assertTrue(clone[0]["description"].startswith("tezcatlipoca-clone comp-"))
        self.assertIn(("POST", "/nodes/node/qemu/1150/template", None), calls)
        ctx.destroy.assert_called_once()
        self.assertIn(1150, ctx.destroy.call_args.args)
        ctx.hash.assert_called_once()
        self.assertEqual(ctx.hash.call_args.args[1], 1150)
        self.assertIn("unbooted", ctx.hash.call_args.kwargs.get("extra", ""))
        ctx.start.assert_not_called()
        ctx.snap.assert_not_called()
        ctx.nakon.assert_not_called()

    def test_dead_cold_attempt_destroyed_and_recloned_from_base(self):
        dc_only = [BOXES[0]]
        result, ctx = self._build({1150: True}, {}, {"dc01": "h1"}, {"dc01"},
                                  boxes=dc_only)
        self.assertEqual(result, {"dc01": 1150})
        ctx.destroy.assert_called_once()
        self.assertIn(1150, ctx.destroy.call_args.args)
        clone_calls = [c for c in ctx.api.call_args_list
                       if c.args[0] == "POST" and c.args[1].endswith("/clone")]
        self.assertEqual(len(clone_calls), 1)
        self.assertEqual(clone_calls[0].kwargs["data"]["full"], 1)
        ctx.nakon.assert_not_called()

    def test_selective_rebuild_touches_only_unconverted_slot(self):
        hashes = {"dc01": "h1", "web01": "h-web01"}
        result, ctx = self._build({1151: True}, {1151: True}, hashes, {"dc01"})
        self.assertEqual(result, {"dc01": 1150, "web01": 1151})
        for c in ctx.api.call_args_list:
            self.assertNotIn("1151", c.args[1])
        destroyed = [c.args[1] for c in ctx.destroy.call_args_list]
        self.assertEqual(destroyed, [1150])
        ctx.nakon.assert_not_called()

    def test_fully_converted_set_short_circuits(self):
        hashes = {"dc01": "h1", "web01": "h-web01"}
        result, ctx = self._build({1150: True, 1151: True},
                                  {1150: True, 1151: True}, hashes, {"dc01"})
        self.assertEqual(result, {"dc01": 1150, "web01": 1151})
        ctx.api.assert_not_called()
        ctx.destroy.assert_not_called()
        ctx.nakon.assert_not_called()


if __name__ == "__main__":
    unittest.main()
