"""Phases 1 (multi-node walk), 7 and the finish/connect steps, driven with fakes.

These branches ran only on a live deploy (phase 7's final pass and beacon plant, phase 1's
satellite bridge reclaim, connect_estate, the "is live" report), so a TypeError or a
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

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from deploy_lib.phases import final, finish  # noqa: E402

TEAMS = {"team1": {"identifier": "101", "password": "p1"},
         "team2": {"identifier": "102", "password": "p2"}}
BOXES = [{"name": "web01", "template": "ubuntu-fix", "last_octet": 2}]


def _ctx(comp, **kw):
    base = dict(
        comp_dir=comp, comp_name=comp.name, from_phase=1, teams=TEAMS, boxes=BOXES, node="pve1",
        engine_vmid=1000, reclaim_tags={"tezcatlipoca", f"comp-{comp.name}", "run-0badf00d"},
        reclaim_tag="run-0badf00d",
        golden_hashes={"web01": "h"}, placement=None,
        all_targets=[{"team_key": k, "vm_name": f"web01-{k}", "vmid": 200 + int(t["identifier"]) * 10,
                      "node": "pve1", "box": BOXES[0], "ip": f"192.168.{t['identifier']}.2"}
                     for k, t in TEAMS.items()],
        state={}, save_state=MagicMock(), nakon_jobs=2, tf_ctx={}, ssh_key=Path("k"),
        scoring_user="sysadmin", scoring_ip="10.0.0.9", box_password="bp", box_username="u",
        box_creds={"a": "b"}, linux_targets=[], windows_targets=[], nakon_config_path=Path("n"),
        final_config_path=comp / "final.json", late_config_path=comp / "late.json",
        name="N", scenario="S", admin_password="ap",
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
        (self.comp / "Compfile").write_text("name tailcomp\n")
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)

    def _p7(self, machines, late=(), **kw):
        (self.comp / "final.json").write_text(json.dumps({"machines": machines}))
        (self.comp / "late.json").write_text(json.dumps({"machines": list(late)}))
        ctx = _ctx(self.comp, **kw)
        mocks = {}
        with patch.object(final, "deploy_domain_configs") as mocks["domains"], \
                patch.object(final, "build_nakon_bundle", return_value="bundle") as mocks["bundle"], \
                patch.object(final, "run_nakon",
                             return_value=SimpleNamespace(failed=["x: boom"], machines=machines)) \
                as mocks["nakon"], \
                patch.object(final, "reensure_mysql_credlist_users") as mocks["mysql"], \
                patch.object(final.tlaloc_ops, "plant_assume_breach") as mocks["red"], \
                patch.object(final, "run_concurrent") as mocks["snap"], \
                patch.object(final, "record_stage_coverage") as mocks["cov"]:
            final.phase7_domains_and_final(ctx)
        return ctx, mocks



    def test_phase7_plants_account_file_modes_after_the_seed(self):
        # The last pass must be the phase's LAST nakon call: fix_services (phase 6) and the
        # assume-breach seed both useradd, which recreates the account files at their
        # default modes and discards a mode planted any earlier.
        ctx, m = self._p7([{"name": "web01-team101"}], late=[{"name": "web01-team101"}])
        self.assertEqual(m["nakon"].call_count, 2)
        self.assertEqual(m["cov"].call_count, 2)
        self.assertEqual(m["nakon"].call_args_list[1].args[4], ctx.late_config_path)
        self.assertIn("late: x: boom", ctx.state["nakon_failed_steps"])


    def test_phase7_normal_path_prints_the_banner(self):
        # The [N/8] banners are what operators and the dashboard's stage jump key on.
        # Phase 7's banner lived only on the skip branch, so every real deploy ran
        # [6/8] → [8/8] in the log.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._p7([])
        self.assertIn("[7/8] Configuring Windows AD domains", buf.getvalue())

    def test_phase7_publishes_the_red_intel_state(self):
        # bad-auto's nakon intel reads the box account from <comp>/.deploy_state.json; the
        # modern pipeline never wrote it, so every seed sprayed (user, None) and paramiko's
        # "no authentication methods" was reported as "unreachable over SSH" on every box
        # (caught live). It must exist before the assume-breach step reads the intel.
        self._p7([])
        path = self.comp / ".deploy_state.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["box_password"], "bp")
        self.assertEqual(data["box_creds"], {"a": "b"})
        self.assertEqual(data["admin_password"], "ap")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_red_failure_aborts_the_deploy(self):
        (self.comp / "final.json").write_text('{"machines": []}')
        (self.comp / "late.json").write_text('{"machines": []}')
        ctx = _ctx(self.comp)
        with patch.object(final, "deploy_domain_configs"), \
                patch.object(final, "reensure_mysql_credlist_users"), \
                patch.object(final, "run_concurrent"), \
                patch.object(final.tlaloc_ops, "plant_assume_breach",
                             side_effect=RuntimeError("no red01")):
            with self.assertRaises(RuntimeError) as cm:
                final.phase7_domains_and_final(ctx)
        self.assertIn("no red01", str(cm.exception))


    def test_connect_estate_and_finish(self):
        ctx = _ctx(self.comp)
        tf = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9",
              "vm_username": "sysadmin"}
        with patch.object(finish, "engine_context", return_value=tf):
            finish.connect_estate(ctx)
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
