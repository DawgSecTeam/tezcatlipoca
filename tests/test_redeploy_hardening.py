"""Redeploy hardening is Linux-scoped: Windows boxes never reach the Linux-only executors.

fix_services_on_boxes (and DNS/auth hardening) build bash for Ubuntu boxes; deploy.py
and clone_ops.py pre-filter their lists, so redeploy's reconfigure/rollback/rebuild
paths must too. Nakon, bootstrap, waits, snapshots, and the domain chain stay on the
full mixed-platform target set."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "redeploy_hardening_test", _REPO / "redeploy-competition.py")
redeploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(redeploy)

LINUX_BOX = {"name": "web01", "template": "ubuntu-2204-web", "cpu": 2, "memory_mb": 2048}
WIN_BOX = {"name": "win01", "template": "windows-server-2022", "cpu": 2, "memory_mb": 4096}


def _target(box, vmid):
    return {
        "box": box,
        "box_name": box["name"],
        "machine": f"{box['name']}-team1",
        "team_key": "team1",
        "identifier": 104,
        "ip": f"192.168.104.{vmid % 250}",
        "vmid": vmid,
        "vm_name": f"{box['name']}-team1",
    }


LINUX = _target(LINUX_BOX, 2104)
WIN = _target(WIN_BOX, 2105)


class RunNakonAndHardenTests(unittest.TestCase):
    def _run(self, targets):
        ctx = {"ssh_key_path": "/tmp/id_ed25519", "scoring_engine_ip": "10.0.0.9"}
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            with patch.dict(os.environ, {"TF_VAR_vm_username": "ops"}), \
                 patch.object(redeploy.pipeline_api, "fix_dns_on_boxes") as p_dns, \
                 patch.object(redeploy.pipeline_api, "setup_ubuntu_auth") as p_auth, \
                 patch.object(redeploy.pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(redeploy.pipeline_api, "run_nakon",
                              return_value=MagicMock(failed=[])) as p_nakon, \
                 patch.object(redeploy.pipeline_api, "fix_services_on_boxes") as p_fix:
                redeploy.run_nakon_and_harden(
                    targets, ctx, comp_dir, {},
                    comp_dir / "nakon-config.json", comp_dir / "bundle")
        return p_dns, p_auth, p_nakon, p_fix

    def test_mixed_input_hardens_linux_only(self):
        p_dns, p_auth, p_nakon, p_fix = self._run([LINUX, WIN])
        self.assertEqual(p_fix.call_args[0][1], [LINUX])
        self.assertEqual(p_dns.call_args[0][0], [LINUX])
        self.assertEqual(p_auth.call_args[0][0], [LINUX])
        self.assertEqual(p_nakon.call_args.kwargs["only"],
                         ["web01-team1", "win01-team1"])

    def test_linux_only_input_hardens_everything(self):
        linux2 = _target({"name": "app01", "template": "ubuntu-2204-app",
                          "cpu": 2, "memory_mb": 2048}, 2106)
        _, _, _, p_fix = self._run([LINUX, linux2])
        self.assertEqual(p_fix.call_args[0][1], [LINUX, linux2])


class ModeRebuildTests(unittest.TestCase):
    def _run(self, targets):
        ctx = {"ssh_key_path": "/tmp/id_ed25519", "scoring_engine_ip": "10.0.0.9",
               "box_username": "ubuntu"}
        state = {
            "box_password": "pw",
            "pipeline_version": 2,
            "golden_template_ids": {t["box_name"]: 1150 + i
                                    for i, t in enumerate(targets)},
        }
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            stage = {"machines": [{"name": t["machine"]} for t in targets]}
            (comp_dir / ".nakon-repair.json").write_text(
                json.dumps({**stage, "steps": []}))
            (comp_dir / ".nakon-final.json").write_text(
                json.dumps({**stage, "steps": []}))
            with patch.dict(os.environ, {"TF_VAR_vm_username": "ops",
                                         "TF_VAR_ssh_public_key": "ssh-ed25519 AAAA test"}), \
                 patch.object(redeploy, "proxmox_api",
                              return_value={"data": "upid"}) as p_api, \
                 patch.object(redeploy, "destroy_vm_if_exists"), \
                 patch.object(redeploy, "wait_for_proxmox_task"), \
                 patch.object(redeploy, "start_vm") as p_start, \
                 patch.object(redeploy, "take_snapshot") as p_snap, \
                 patch.object(redeploy, "build_nakon_bundle"), \
                 patch.object(redeploy, "stored_template_hash", return_value=None), \
                 patch.object(redeploy, "rerun_domain_configs", return_value=True), \
                 patch.object(redeploy.pipeline_api, "bootstrap_windows_box") as p_boot, \
                 patch.object(redeploy.pipeline_api, "wait_for_boxes_ssh"), \
                 patch.object(redeploy.pipeline_api, "wait_for_cloud_init"), \
                 patch.object(redeploy.pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(redeploy.pipeline_api, "run_nakon",
                              return_value=MagicMock(failed=[])) as p_nakon, \
                 patch.object(redeploy.pipeline_api, "fix_services_on_boxes") as p_fix:
                redeploy.mode_rebuild(
                    targets, ctx, "node", comp_dir, state,
                    comp_dir / "nakon-config.json", comp_dir / "bundle")
        return p_boot, p_start, p_snap, p_nakon, p_fix, p_api

    def test_mixed_rebuild_hardens_linux_bootstraps_windows(self):
        p_boot, p_start, p_snap, p_nakon, p_fix, _ = self._run([LINUX, WIN])
        self.assertEqual(p_fix.call_args[0][1], [LINUX])
        self.assertEqual(p_boot.call_args[0][1], WIN["vmid"])
        self.assertEqual({c[0][1] for c in p_start.call_args_list},
                         {LINUX["vmid"], WIN["vmid"]})
        planted = p_nakon.call_args.kwargs["only"]
        self.assertIn("web01-team1", planted)
        self.assertIn("win01-team1", planted)
        self.assertEqual(p_snap.call_count, 4)

    def test_linux_only_rebuild_hardens_all(self):
        linux2 = _target({"name": "app01", "template": "ubuntu-2204-app",
                          "cpu": 2, "memory_mb": 2048}, 2106)
        p_boot, _, _, _, p_fix, _ = self._run([LINUX, linux2])
        self.assertEqual(p_fix.call_args[0][1], [LINUX, linux2])
        p_boot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
