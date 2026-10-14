"""Operator-side children must run under the harness's OWN interpreter.

Regression for the 2026-10-14 dashboard e2e: `stage_deploy` spawned the deploy as bare
`python3`, so under the dashboard (whose service venv is the interpreter with the engine deps)
the child got the distro python3 — `ModuleNotFoundError: No module named 'toml'` — and two runs
died minutes in, after the run record, workspace and worktree existed. The same bare spawn sat on
verify-competition.py, scrim-report.py and every `python3 -m badauto` (red) call, each of which
would have failed later in the run instead of at its start.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.utils import local_python
from scrim import core, staging, teardown_stage

REPO = Path(__file__).resolve().parents[1]
# Operator-side spawn sites (remote ssh commands are deliberately NOT here: on a box the remote
# python3 is the right interpreter).
LOCAL_SPAWN_FILES = ("scrim/staging.py", "scrim/cli.py", "scrim/compworld.py",
                     "scrim/test_folder.py", "scrim/red_stage.py", "scrim/teardown_stage.py",
                     "red/red_plant_ops.py", "artifacts_lib/verdict.py")


def _ok(**_kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class LocalPython(unittest.TestCase):
    def test_default_is_the_running_interpreter(self):
        with mock.patch.dict("os.environ", {"TEZ_PYTHON": ""}):
            self.assertEqual(local_python(), sys.executable)

    def test_env_override(self):
        with mock.patch.dict("os.environ", {"TEZ_PYTHON": "/opt/venv/bin/python"}):
            self.assertEqual(local_python(), "/opt/venv/bin/python")


class StageDeploySpawn(unittest.TestCase):
    def test_deploy_child_uses_the_harness_interpreter(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="last_phase: 8\n", stderr="")

        with mock.patch.object(staging.procs, "run", side_effect=fake_run), \
                mock.patch.object(core, "read_comp_json", return_value={"last_phase": 8}):
            args = SimpleNamespace(teams=2, resume=None, force_resume=False, run_dir=None)
            comp = Path("/tmp/does-not-matter")
            staging.stage_deploy(args, comp)
        self.assertEqual(calls[0][0], sys.executable, calls[0][:2])
        self.assertNotIn("python3", calls[0][0])


class RedSpawns(unittest.TestCase):
    def test_teardown_badauto_uses_the_harness_interpreter(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _ok(**kw)

        args = SimpleNamespace(competition="c1", comp_dir="/tmp/c1", red_vmid=999,
                               test_dir=None, run_dir=None)
        with mock.patch.object(teardown_stage.procs, "run", side_effect=fake_run), \
                mock.patch.object(teardown_stage, "log"), \
                mock.patch.object(teardown_stage.badauto_config_ops, "config_path",
                                  return_value=Path("/nonexistent/config.yaml")), \
                mock.patch.object(teardown_stage, "_config_red_vmid", return_value=None), \
                mock.patch.object(teardown_stage, "_red_vm_still_exists", return_value=False), \
                mock.patch.object(teardown_stage.red_link, "pull_red_state", return_value=[]):
            teardown_stage.teardown_red(args, {})
        self.assertEqual(calls[0][:3], [sys.executable, "-m", "badauto"], calls[0][:3])

    def test_no_operator_side_spawn_hardcodes_python3(self):
        offenders = []
        for rel in LOCAL_SPAWN_FILES:
            for n, line in enumerate((REPO / rel).read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or "local_python" in line:
                    continue
                if '"python3"' in line or "'python3'" in line:
                    offenders.append(f"{rel}:{n}: {stripped}")
        self.assertEqual(offenders, [],
                         "use common.utils.local_python() for operator-side children "
                         "(a bare python3 is a different environment):\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()


class DeployTargetsTheWorkspaceTests(unittest.TestCase):
    """`create-competition.py` must be pointed at the workspace, not told to scaffold a new one."""

    def test_deploy_child_gets_comp_dir_and_the_real_comp_name(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="last_phase: 8\n", stderr="")

        with mock.patch.object(staging.procs, "run", side_effect=fake_run), \
                mock.patch.object(core, "read_comp_json", return_value={"last_phase": 8}):
            args = SimpleNamespace(competition="scrim-one", teams=2, resume=None,
                                   force_resume=False, run_dir=None)
            staging.stage_deploy(args, Path("/data/runs/run-x/comp"))
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--comp-dir") + 1], "/data/runs/run-x/comp")
        self.assertEqual(cmd[cmd.index("--competition") + 1], "scrim-one")

    def test_deploy_falls_back_to_the_dir_name_without_a_comp_id(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="last_phase: 8\n", stderr="")

        with mock.patch.object(staging.procs, "run", side_effect=fake_run), \
                mock.patch.object(core, "read_comp_json", return_value={"last_phase": 8}):
            args = SimpleNamespace(competition=None, teams=2, resume=None,
                                   force_resume=False, run_dir=None)
            staging.stage_deploy(args, Path("/data/runs/run-x/comp"))
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--competition") + 1], "comp")

    def test_deploy_child_carries_the_runs_node_list(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="last_phase: 8\n", stderr="")

        with mock.patch.object(staging.procs, "run", side_effect=fake_run), \
                mock.patch.object(core, "read_comp_json", return_value={"last_phase": 8}):
            args = SimpleNamespace(competition="scrim-one", teams=1, resume=None,
                                   force_resume=False, run_dir=None,
                                   nodes="/data/runs/run-x/nodes.json")
            staging.stage_deploy(args, Path("/data/runs/run-x/comp"))
        cmd = calls[0]
        self.assertEqual(cmd[cmd.index("--nodes") + 1], "/data/runs/run-x/nodes.json")

    def test_verify_argument_is_absolute_outside_the_engine_tree(self):
        from scrim import compworld
        inside = core.REPO / "competitions" / "demo"
        outside = Path("/data/runs/run-x/comp")
        self.assertEqual(compworld.comp_arg(inside), "competitions/demo")
        self.assertEqual(compworld.comp_arg(outside), "/data/runs/run-x/comp")
