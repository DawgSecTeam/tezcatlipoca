"""Redeploy hardening is Linux-scoped: Windows boxes never reach the Linux-only executors.

fix_services_on_boxes (and DNS/auth hardening) build bash for Ubuntu boxes; deploy.py
and clone_ops.py pre-filter their lists, so redeploy's reconfigure/rollback/rebuild
paths must too. Nakon, bootstrap, waits, snapshots, and the domain chain stay on the
full mixed-platform target set."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from tests._deploy_patch import fake_nakon_result  # noqa: E402

import pipeline_api  # noqa: E402
from guests import converge_ops  # noqa: E402
from redeploy import redeploy_plant_ops  # noqa: E402
from redeploy import redeploy_rebuild_ops  # noqa: E402
from redeploy.redeploy_context import RedeployContext  # noqa: E402


def _rctx(ctx, comp_dir, state=None, node="node"):
    return RedeployContext(comp_dir=comp_dir, name="x", comp_name="x", node=node,
                           teams=None, boxes=None, difficulty=None,
                           state={} if state is None else state, state_path=None, ctx=ctx)

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
    def _run(self, targets, late=None):
        ctx = {"ssh_key_path": "/tmp/id_ed25519", "scoring_engine_ip": "10.0.0.9"}
        order = []
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            if late is not None:
                (comp_dir / ".nakon-late.json").write_text(
                    json.dumps({"machines": [{"name": n} for n in late]}))
            with patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "ops"}), \
                 patch.object(pipeline_api, "prep_apt_on_boxes"), \
                 patch.object(pipeline_api, "fix_dns_on_boxes") as p_dns, \
                 patch.object(pipeline_api, "setup_ubuntu_auth") as p_auth, \
                 patch.object(pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(converge_ops, "build_nakon_bundle", return_value="bundle"), \
                 patch.object(converge_ops, "run_nakon",
                              side_effect=lambda *a, **k: (
                                  order.append(("nakon", Path(a[4]).name)),
                                  fake_nakon_result())[1]) as p_nakon, \
                 patch.object(pipeline_api, "fix_services_on_boxes",
                              side_effect=lambda *a, **k: order.append(("harden", ""))) as p_fix:
                cfg = comp_dir / "nakon-config.json"
                cfg.write_text(json.dumps(
                    {"machines": [{"name": t["machine"]} for t in targets]}))
                redeploy_plant_ops.run_nakon_and_harden(
                    targets, _rctx(ctx, comp_dir), cfg, comp_dir / "bundle")
        return p_dns, p_auth, p_nakon, p_fix, order

    def test_mixed_input_hardens_linux_only(self):
        p_dns, p_auth, p_nakon, p_fix, _ = self._run([LINUX, WIN])
        self.assertEqual(p_fix.call_args[0][1], [LINUX])
        self.assertEqual(p_dns.call_args[0][0], [LINUX])
        self.assertEqual(p_auth.call_args[0][0], [LINUX])
        self.assertEqual(p_nakon.call_args.kwargs["only"],
                         ["web01-team1", "win01-team1"])




class ModeRebuildTests(unittest.TestCase):
    """The rebuild delegates its clone to the deploy's creator, then runs the shared
    post-tz-base convergence (predomain -> harden -> domains -> final -> late)."""

    def _run(self, targets, late=None):
        ctx = {"ssh_key_path": "/tmp/id_ed25519", "scoring_engine_ip": "10.0.0.9",
               "box_username": "ubuntu"}
        order = []
        created = {}
        state = {
            "box_password": "pw",
            "run_id": "run-beefcafe",
            "golden_template_ids": {t["box_name"]: 1150 + i
                                    for i, t in enumerate(targets)},
        }

        def fake_create(_document, _ownership, target, **kw):
            created[target["box_name"]] = kw["source_vmid"]

        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            stage = {"machines": [{"name": t["machine"]} for t in targets]}
            (comp_dir / ".nakon-predomain.json").write_text(json.dumps(stage))
            (comp_dir / ".nakon-final.json").write_text(json.dumps(stage))
            if late is not None:
                (comp_dir / ".nakon-late.json").write_text(
                    json.dumps({"machines": [{"name": n} for n in late]}))
            with patch.dict(os.environ, {"TEZ_ENGINE_USERNAME": "ops",
                                         "TEZ_SSH_PUBLIC_KEY": "ssh-ed25519 AAAA test"}), \
                 patch.object(redeploy_rebuild_ops, "destroy_vm_if_exists"), \
                 patch.object(redeploy_rebuild_ops.estate_apply, "create_team_box",
                              side_effect=fake_create), \
                 patch.object(redeploy_rebuild_ops, "stored_template_hash", return_value=None), \
                 patch.object(redeploy_plant_ops, "rerun_domain_configs", return_value=True), \
                 patch.object(pipeline_api, "bootstrap_windows_box") as p_boot, \
                 patch.object(pipeline_api, "wait_for_boxes_ssh"), \
                 patch.object(pipeline_api, "wait_for_cloud_init"), \
                 patch.object(pipeline_api, "setup_ubuntu_auth"), \
                 patch.object(pipeline_api, "fix_dns_on_boxes"), \
                 patch.object(pipeline_api, "prep_apt_on_boxes"), \
                 patch.object(pipeline_api, "ensure_nat_forwarding"), \
                 patch.object(pipeline_api, "take_snapshot") as p_snap, \
                 patch.object(converge_ops, "build_nakon_bundle", return_value="bundle"), \
                 patch.object(converge_ops, "run_nakon",
                              side_effect=lambda *a, **k: (
                                  order.append(("nakon", Path(a[4]).name)),
                                  fake_nakon_result())[1]) as p_nakon, \
                 patch.object(pipeline_api, "fix_services_on_boxes",
                              side_effect=lambda *a, **k: order.append(("harden", ""))) as p_fix:
                redeploy_rebuild_ops.mode_rebuild(
                    targets, _rctx(ctx, comp_dir, state),
                    comp_dir / "nakon-config.json", comp_dir / "bundle")
        return p_boot, p_snap, p_nakon, p_fix, created, order

    def test_mixed_rebuild_hardens_linux_bootstraps_windows(self):
        p_boot, p_snap, p_nakon, p_fix, created, _ = self._run([LINUX, WIN])
        self.assertEqual(p_fix.call_args[0][1], [LINUX])
        self.assertEqual(p_boot.call_args[0][1], WIN["vmid"])
        # Each box clones from ITS golden, through the deploy's creator.
        self.assertEqual(created, {"web01": 1150, "win01": 1151})
        planted = p_nakon.call_args.kwargs["only"]
        self.assertIn("web01-team1", planted)
        self.assertIn("win01-team1", planted)
        # SNAP_BASE + SNAP_READY per box.
        self.assertEqual(p_snap.call_count, 4)

    def test_rebuild_runs_the_late_mode_pass_after_the_final_stage(self):
        # The rebuild's own fix_services useradd would clobber a mode planted by the
        # pre-domain/final passes, so the late pass must be the last writer here too.
        _, _, p_nakon, _, _, order = self._run([LINUX, WIN], late=["web01-team1"])
        self.assertEqual(Path(p_nakon.call_args_list[-1][0][4]).name, ".nakon-late.json")
        self.assertEqual(p_nakon.call_args_list[-1].kwargs["only"], ["web01-team1"])
        kinds = [k for k, _ in order]
        self.assertEqual(kinds, ["nakon", "harden", "nakon", "nakon"])
        stages = [Path(c[0][4]).name for c in p_nakon.call_args_list]
        self.assertEqual(stages, [".nakon-predomain.json", ".nakon-final.json",
                                  ".nakon-late.json"])


if __name__ == "__main__":
    unittest.main()
