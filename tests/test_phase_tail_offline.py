"""Phases 1 (multi-node walk), 7 and the finish/connect steps, driven with fakes.

These branches ran only on a live deploy (phase 7's final pass and beacon plant, phase 1's
satellite bridge reclaim, connect_terraform, the "is live" report), so a TypeError or a
renamed attribute introduced by the module split would have surfaced mid-event."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from deploy_lib.phases import cleanup, final, finish  # noqa: E402

TEAMS = {"team1": {"identifier": "101", "password": "p1"},
         "team2": {"identifier": "102", "password": "p2"}}
BOXES = [{"name": "web01", "template": "ubuntu-fix", "last_octet": 2}]


def _ctx(comp, **kw):
    base = dict(
        comp_dir=comp, comp_name=comp.name, from_phase=1, teams=TEAMS, boxes=BOXES, node="pve1",
        engine_vmid=1000, reclaim_tags={"tezcatlipoca", f"comp-{comp.name}", "run-0badf00d"},
        golden_hashes={"web01": "h"}, frozen_keep=set(), placement=None,
        all_targets=[{"team_key": k, "vm_name": f"web01-{k}", "vmid": 200 + int(t["identifier"]) * 10,
                      "node": "pve1", "box": BOXES[0], "ip": f"192.168.{t['identifier']}.2"}
                     for k, t in TEAMS.items()],
        state={}, save_state=MagicMock(), nakon_jobs=2, tf_ctx={}, ssh_key=Path("k"),
        scoring_user="sysadmin", scoring_ip="10.0.0.9", box_password="bp", box_username="u",
        box_creds={"a": "b"}, linux_targets=[], windows_targets=[], nakon_config_path=Path("n"),
        final_config_path=comp / "final.json", name="N", scenario="S", admin_password="ap",
        scoring_password="sp", inject_password="ip", injects=[1, 2], domain_creds=None,
        packet_pw=None, comp_tags={"tezcatlipoca"}, ssh_key_abs="k", engine_mgmt_ip="10.0.0.250")
    base.update(kw)
    return SimpleNamespace(**base)


class Tail(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.comp = Path(self._tmp.name) / "tailcomp"
        self.comp.mkdir()
        (self.comp / "Compfile").write_text("name tailcomp\nteam_beacons 1\n")
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    # ------------------------------------------------------------------ phase 7
    def _p7(self, machines, **kw):
        (self.comp / "final.json").write_text(json.dumps({"machines": machines}))
        ctx = _ctx(self.comp, **kw)
        mocks = {}
        with patch.object(final, "deploy_domain_configs") as mocks["domains"], \
                patch.object(final, "ensure_nat_forwarding") as mocks["nat"], \
                patch.object(final, "build_nakon_bundle", return_value="bundle") as mocks["bundle"], \
                patch.object(final, "run_nakon",
                             return_value=SimpleNamespace(failed=["x: boom"], machines=machines)) \
                as mocks["nakon"], \
                patch.object(final, "reensure_mysql_credlist_users") as mocks["mysql"], \
                patch.object(final, "plant_team_beacons") as mocks["beacons"], \
                patch.object(final, "run_concurrent") as mocks["snap"], \
                patch.object(final, "record_stage_coverage") as mocks["cov"]:
            final.phase7_domains_and_final(ctx)
        return ctx, mocks

    def test_phase7_with_final_machines(self):
        ctx, m = self._p7([{"name": "web01-team101"}])
        m["domains"].assert_called_once()
        m["nakon"].assert_called_once()
        m["cov"].assert_called_once()
        m["beacons"].assert_called_once()
        m["snap"].assert_called_once()
        self.assertEqual(ctx.state["nakon_failed_steps"], ["final: x: boom"])

    def test_phase7_merges_prior_failures_without_duplicates(self):
        (self.comp / "final.json").write_text(json.dumps({"machines": [{"n": 1}]}))
        ctx = _ctx(self.comp, state={"nakon_failed_steps": ["repair: a", "final: x: boom"]})
        with patch.object(final, "deploy_domain_configs"), patch.object(final, "ensure_nat_forwarding"), \
                patch.object(final, "build_nakon_bundle"), \
                patch.object(final, "run_nakon", return_value=SimpleNamespace(failed=["x: boom"])), \
                patch.object(final, "reensure_mysql_credlist_users"), \
                patch.object(final, "plant_team_beacons"), patch.object(final, "run_concurrent"), \
                patch.object(final, "record_stage_coverage"):
            final.phase7_domains_and_final(ctx)
        self.assertEqual(ctx.state["nakon_failed_steps"], ["repair: a", "final: x: boom"])

    def test_phase7_empty_final_stage_records_clean_coverage(self):
        ctx, m = self._p7([])
        m["nakon"].assert_not_called()
        self.assertIn("plant_coverage_failed", ctx.state)
        self.assertEqual(ctx.state["nakon_failed_steps"], [])

    def test_phase7_skipped_on_resume(self):
        (self.comp / "final.json").write_text('{"machines": []}')
        ctx = _ctx(self.comp, from_phase=8)
        with patch.object(final, "deploy_domain_configs") as d:
            final.phase7_domains_and_final(ctx)
        d.assert_not_called()

    # ------------------------------------------------------------------ phase 1
    def test_phase1_multinode_reclaims_satellite_bridges_and_jump(self):
        placement = {
            "engine_node": "n1", "team_nodes": {"team1": "n1", "team2": "n2"},
            "satellites": [{"name": "n2", "slot": 1, "jump_vmid": 1131, "teams": ["team2"]}]}
        ctx = _ctx(self.comp, placement=placement)
        ctx.all_targets[1]["node"] = "pve2"
        rec = SimpleNamespace(node="pve2")
        destroyed, bridges = [], []
        with patch.object(cleanup, "record_of", return_value=rec), \
                patch.object(cleanup, "proxmox_api", return_value={"data": []}), \
                patch.object(cleanup, "destroy_vm_if_exists",
                             side_effect=lambda n, v, expect_tags=None: destroyed.append((n, v))), \
                patch.object(cleanup, "destroy_bridge_if_exists",
                             side_effect=lambda n, b: bridges.append((n, b))), \
                patch.object(cleanup, "_is_template", return_value=False), \
                patch.object(cleanup, "load_template_hashes", return_value={}), \
                patch.object(cleanup.time, "sleep"):
            cleanup.phase1_cleanup(ctx)
        self.assertIn(("pve2", 1131), destroyed)                 # satellite jump VM
        self.assertIn(("pve1", 1000), destroyed)                 # engine on the engine node
        self.assertIn(("pve1", 200 + 1010), destroyed)           # team1 box on pve1
        self.assertIn(("pve2", 200 + 1020), destroyed)           # team2 box on the satellite
        self.assertEqual(sorted(bridges), [("pve1", "vmbr101"), ("pve2", "vmbr102")])

    def test_phase1_bridge_in_use_is_kept(self):
        ctx = _ctx(self.comp)

        def api(method, path, **kw):
            if path.endswith("/qemu"):
                return {"data": [{"vmid": 5}]}
            return {"data": {"net0": "virtio=aa:bb,bridge=vmbr101"}}
        with patch.object(cleanup, "proxmox_api", side_effect=api), \
                patch.object(cleanup, "destroy_bridge_if_exists") as d:
            cleanup._reclaim_bridge(ctx, "pve1", "vmbr101")
            d.assert_not_called()

    def test_phase1_in_path_fw_also_reclaims_transit_bridges(self):
        boxes = BOXES + [{"name": "fw01", "template": "pfsense", "unmanaged": True,
                          "in_path": True, "last_octet": 1}]
        ctx = _ctx(self.comp, boxes=boxes, all_targets=[])
        bridges = []
        with patch.object(cleanup, "proxmox_api", return_value={"data": []}), \
                patch.object(cleanup, "destroy_vm_if_exists"), \
                patch.object(cleanup, "destroy_bridge_if_exists",
                             side_effect=lambda n, b: bridges.append(b)), \
                patch.object(cleanup, "_is_template", return_value=False), \
                patch.object(cleanup, "load_template_hashes", return_value={}), \
                patch.object(cleanup.time, "sleep"):
            cleanup.phase1_cleanup(ctx)
        self.assertEqual(sorted(bridges), ["vmbr101", "vmbr102", "vmbrW101", "vmbrW102"])

    # ------------------------------------------------------------------ finish
    def test_connect_terraform_and_finish(self):
        ctx = _ctx(self.comp)
        tf = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9"}
        with patch.object(finish, "read_terraform_ctx", return_value=tf), \
                patch.dict(os.environ, {"TF_VAR_vm_username": "sysadmin"}):
            finish.connect_terraform(ctx)
        self.assertEqual((ctx.scoring_ip, ctx.scoring_user, ctx.ssh_key),
                         ("10.0.0.9", "sysadmin", Path("/k")))
        buf = io.StringIO()
        with patch.object(finish, "print_timing_summary"), contextlib.redirect_stdout(buf):
            finish.finish_deploy(ctx)
        text = (self.comp / "credentials.txt").read_text()
        self.assertIn("scoring  sp", text)
        self.assertIn("inject  ip", text)
        self.assertIn("is live", buf.getvalue())
        self.assertEqual((self.comp / "credentials.txt").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
