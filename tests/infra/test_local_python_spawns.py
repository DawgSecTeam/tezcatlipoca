"""Operator-side children must run under the harness's OWN interpreter.

Regression for a dashboard e2e finding: `stage_deploy` spawned the deploy as bare
`python3`, so under the dashboard (whose service venv is the interpreter with the engine deps)
the child got the distro python3 — `ModuleNotFoundError: No module named 'toml'` — and two runs
died minutes in, after the run record, workspace and worktree existed. The same bare spawn sat on
verify-competition.py, scrim-report.py and every `python3 -m badauto` (red) call, each of which
would have failed later in the run instead of at its start.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.utils import local_python
from estate import config
from scrim import core, staging, teardown_stage

REPO = Path(__file__).resolve().parents[2]
# Operator-side spawn sites (remote ssh commands are deliberately NOT here: on a box the remote
# python3 is the right interpreter).
LOCAL_SPAWN_FILES = ("scrim/staging.py", "scrim/cli.py", "scrim/compworld.py",
                     "scrim/test_folder.py", "scrim/red_stage.py", "scrim/teardown_stage.py",
                     "red/tlaloc_ops.py", "artifacts_lib/verdict.py")


def _ok(**_kw):
    return SimpleNamespace(returncode=0, stdout="", stderr="")




class StageDeploySpawn(unittest.TestCase):
    def _spawn(self, executable=None):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="last_phase: 8\n", stderr="")

        args = SimpleNamespace(teams=2, resume=None, force_resume=False, run_dir=None)
        with mock.patch.object(staging.procs, "run", side_effect=fake_run), \
                mock.patch.object(core, "read_comp_json", return_value={"last_phase": 8}), \
                mock.patch.dict("os.environ", {"TEZ_PYTHON": ""}):
            if executable is not None:
                with mock.patch.object(sys, "executable", executable):
                    config.reset()
                    self.addCleanup(config.reset)
                    staging.stage_deploy(args, Path("/tmp/does-not-matter"))
            else:
                staging.stage_deploy(args, Path("/tmp/does-not-matter"))
        return calls[0]


    def test_deploy_child_uses_the_harness_interpreter_when_that_is_a_distro_python(self):
        # The contract is argv[0] == local_python(), not "argv[0] is not named python3":
        # the harness itself can be /usr/bin/python3, which carries none of the deps. The
        # old assertion (`"python3" not in sys.executable`) tested the host, not the spawn.
        cmd = self._spawn(executable="/usr/bin/python3")
        self.assertEqual(cmd[0], local_python())
        self.assertEqual(cmd[0], "/usr/bin/python3")


class RedClock(unittest.TestCase):
    def test_the_runs_duration_is_exported_as_reds_event_clock(self):
        # red's pushed clock is the scrim's --duration-min: the deploy child inherits
        # the env, and the in-process re-stage reads it too (the comp profile is not
        # the authority, so a scrim and its comp cannot disagree).
        with mock.patch.dict("os.environ", {}, clear=False):
            staging.set_red_clock(SimpleNamespace(duration_min=45))
            self.assertEqual(os.environ["TEZ_TLALOC_DURATION_MIN"], "45")

    def test_a_missing_duration_leaves_the_env_alone(self):
        with mock.patch.dict("os.environ", {"TEZ_TLALOC_DURATION_MIN": "60"}):
            staging.set_red_clock(SimpleNamespace())
            self.assertEqual(os.environ["TEZ_TLALOC_DURATION_MIN"], "60")


class RedSpawns(unittest.TestCase):
    def test_red_stage_builders_use_the_harness_interpreter(self):
        from red import tlaloc_ops
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp)
            (comp / "Compfile").write_text("name demo\n")
            argv = tlaloc_ops.comp_prep_command(comp)
        self.assertEqual(argv[0], local_python())


if __name__ == "__main__":
    unittest.main()
