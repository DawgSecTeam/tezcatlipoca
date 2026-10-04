"""redeploy-competition.py's main() end to end with Proxmox replaced by fakes.

Nothing exercised this entrypoint after the split into redeploy_*_ops: the gate, selectors,
dry-run/reset-ladder printout, snapshot refusal and the dispatch table are all reached only
through main(). Every Proxmox/engine call is patched in the script's own namespace."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from constants import PIPELINE_VERSION, SNAP_BASE, SNAP_READY  # noqa: E402


def _load_script():
    spec = importlib.util.spec_from_file_location("redeploy_cli", _REPO / "redeploy-competition.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BOXES = [{"name": "web01", "template": "base-ubuntu24.04-fix", "last_octet": 2, "cpu": 1,
          "memory_mb": 1024},
         {"name": "dc01", "template": "base-windows-server", "last_octet": 3, "cpu": 2,
          "memory_mb": 2048}]
TEAMS = {"team1": {"identifier": "101", "password": "pw"},
         "team2": {"identifier": "102", "password": "pw"}}


class RedeployCliOffline(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.comp = self.root / "competitions" / "rd"
        self.comp.mkdir(parents=True)
        (self.comp / "Compfile").write_text("name rd\nscenario s\ndifficulty 2\n")
        (self.comp / "teams.json").write_text(json.dumps(TEAMS))
        (self.comp / "boxes.json").write_text(json.dumps(BOXES))
        self.state = {"pipeline_version": PIPELINE_VERSION, "run_id": "run-0badf00d",
                      "scoring_vm_id": 1234}
        (self.comp / ".deploy_state.json").write_text(json.dumps(self.state))
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        p = patch.dict(os.environ, {"TF_VAR_proxmox_node": "pve1"})
        p.start()
        self.addCleanup(p.stop)
        self.mod = _load_script()

    def run_main(self, *argv, snaps=None, answer=None):
        snaps = snaps if snaps is not None else {SNAP_READY, SNAP_BASE}
        out = io.StringIO()
        with patch.object(sys, "argv", ["redeploy-competition.py", *argv]), \
                patch.object(self.mod, "list_snapshots", side_effect=lambda n, v: set(snaps)), \
                patch.object(self.mod, "proxmox_api",
                             return_value={"data": {"tags": "tezcatlipoca;comp-rd"}}), \
                patch.object(self.mod, "acquire_engine_lock"), \
                patch.object(self.mod.pipeline_api, "read_terraform_ctx", return_value={"scoring_engine_ip": "10.0.0.9"}), \
                patch.object(self.mod, "prepare_nakon_assets", return_value=("cfg", "bundle")), \
                patch("builtins.input", return_value=answer or "n"), \
                contextlib.redirect_stdout(out):
            code = None
            try:
                self.mod.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_dry_run_lists_all_targets(self):
        code, out = self.run_main("--competition", "rd", "--dry-run")
        self.assertIsNone(code)
        self.assertIn("4 of 4 box(es) selected", out)
        self.assertIn("--dry-run: nothing was changed", out)

    def test_selectors_and(self):
        code, out = self.run_main("--competition", "rd", "--teams", "102", "--platform", "windows",
                                  "--dry-run")
        self.assertIsNone(code)
        self.assertIn("1 of 4 box(es) selected", out)
        self.assertIn("team2", out)
        self.assertIn("dc01", out)

    def test_unknown_team_and_box_refused(self):
        code, _ = self.run_main("--competition", "rd", "--teams", "nope", "--dry-run")
        self.assertIn("no team matches", str(code))
        code, _ = self.run_main("--competition", "rd", "--boxes", "nope", "--dry-run")
        self.assertIn("no box named", str(code))

    def test_no_match_exits(self):
        code, _ = self.run_main("--competition", "rd", "--teams", "1", "--boxes", "web01",
                                "--platform", "windows", "--dry-run")
        self.assertIn("nothing to do", str(code))

    def test_old_pipeline_state_refused_before_any_proxmox_call(self):
        (self.comp / ".deploy_state.json").write_text(json.dumps({"pipeline_version": 1}))
        code, _ = self.run_main("--competition", "rd", "--dry-run")
        self.assertIn("older pipeline", str(code))
        state = dict(self.state)
        state.pop("run_id")
        (self.comp / ".deploy_state.json").write_text(json.dumps(state))
        code, _ = self.run_main("--competition", "rd", "--dry-run")
        self.assertIn("older pipeline", str(code))

    def test_bad_comp_name_and_missing_dir(self):
        code, _ = self.run_main("--competition", "../x", "--dry-run")
        self.assertIn("invalid competition name", str(code))
        code, _ = self.run_main("--competition", "ghost", "--dry-run")
        self.assertIn("no such competition", str(code))

    def test_reset_ladder_rungs(self):
        code, out = self.run_main("--competition", "rd", "--mode", "reset", "--dry-run",
                                  snaps={SNAP_BASE})
        self.assertIsNone(code)
        self.assertIn("2/3", out)
        code, out = self.run_main("--competition", "rd", "--mode", "reset", "--dry-run", snaps=set())
        self.assertIn("3/3 golden rebuild", out)

    def test_missing_snapshot_refused(self):
        code, out = self.run_main("--competition", "rd", "--mode", "rollback-ready", snaps=set())
        self.assertEqual(code, 1)
        self.assertIn(f"no '{SNAP_READY}' snapshot", out)

    def test_cancel_changes_nothing(self):
        with patch.object(self.mod, "mode_rollback") as m:
            code, out = self.run_main("--competition", "rd", "--mode", "rollback-ready",
                                      answer="n")
        self.assertIsNone(code)
        self.assertIn("Cancelled", out)
        m.assert_not_called()

    def test_reset_event_only_with_rollback_or_reset(self):
        code, _ = self.run_main("--competition", "rd", "--mode", "resync", "--reset-event", "--yes")
        self.assertIn("--reset-event only applies", str(code))

    def test_dispatch_table(self):
        calls = {}

        def rec(name):
            def f(*a, **k):
                calls[name] = (a, k)
                return []
            return f

        patches = {n: rec(n) for n in ("mode_rollback", "mode_reconfigure", "mode_resync",
                                       "mode_reset", "mode_rebuild")}
        with patch.multiple(self.mod, **patches):
            for mode, expect in (("rollback-ready", "mode_rollback"),
                                 ("rollback-base", "mode_rollback"),
                                 ("reconfigure", "mode_reconfigure"),
                                 ("resync", "mode_resync"), ("reset", "mode_reset"),
                                 ("rebuild", "mode_rebuild")):
                calls.clear()
                code, out = self.run_main("--competition", "rd", "--mode", mode, "--yes")
                self.assertIn(expect, calls, f"{mode}: {out[-400:]}")
        a, _ = calls["mode_rebuild"]
        self.assertTrue(a)

    def test_reset_event_runs_engine_recovery_then_reseed(self):
        order = []
        with patch.multiple(self.mod, mode_rollback=lambda *a, **k: [],
                            engine_recovery=lambda *a, **k: order.append("recover") or True,
                            release_engine_lock=lambda: order.append("release"),
                            reseed_event=lambda cd: order.append("reseed")):
            code, out = self.run_main("--competition", "rd", "--mode", "rollback-ready",
                                      "--reset-event", "--yes")
        self.assertIsNone(code, out)
        self.assertEqual(order, ["recover", "release", "reseed"])

    def test_engine_recovery_mode_skips_selection(self):
        with patch.object(self.mod, "engine_recovery") as er:
            code, _ = self.run_main("--competition", "rd", "--mode", "engine-recovery", "--yes")
        self.assertIsNone(code)
        er.assert_called_once()


if __name__ == "__main__":
    unittest.main()
