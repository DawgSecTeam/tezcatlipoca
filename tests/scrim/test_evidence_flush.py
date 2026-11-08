"""A6 — evidence out before the wrecking ball.

run-14ac7f14's collected evidence (team logs, red state, INTERACTION.md — 30MB)
lived only inside the destroy-bound comp workspace and survived solely because an
operator archived it by hand with minutes to spare. teardown_stage.flush_evidence
copies the test folder to $TEZ_EVIDENCE_DIR before any destroy step, warn-only."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from scrim import teardown_stage  # noqa: E402


def _tree(root):
    (root / "teams" / "team1").mkdir(parents=True)
    (root / "teams" / "team1" / "cycle-1.log").write_text("blue work\n")
    (root / "INTERACTION.md").write_text("verdict: NOT READY\n")
    (root / "run.json").write_text('{"phase": "teardown"}')


class Flush(unittest.TestCase):
    def test_test_folder_lands_in_the_evidence_dir(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "comp" / ".automated-tests" / "scrim-42"
            src.mkdir(parents=True)
            _tree(src)
            ev = Path(td) / "runs" / "run-42" / "evidence"
            args = SimpleNamespace(test_dir=str(src))
            with mock.patch.dict(os.environ, {"TEZ_EVIDENCE_DIR": str(ev)}):
                dest = teardown_stage.flush_evidence(args)
            self.assertEqual(dest, ev / "scrim-42")
            self.assertEqual((dest / "teams" / "team1" / "cycle-1.log").read_text(),
                             "blue work\n")
            self.assertIn("NOT READY", (dest / "INTERACTION.md").read_text())

    def test_no_env_no_copy_but_never_raises(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "t"; src.mkdir()
            with mock.patch.dict(os.environ, {}, clear=True):
                os.environ.pop("TEZ_EVIDENCE_DIR", None)
                self.assertIsNone(teardown_stage.flush_evidence(SimpleNamespace(test_dir=str(src))))

    def test_copy_target_inside_source_is_refused(self):
        # a flush that copies a folder into itself would grow until the disk dies
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "t"; src.mkdir()
            args = SimpleNamespace(test_dir=str(src))
            with mock.patch.dict(os.environ, {"TEZ_EVIDENCE_DIR": str(src / "inner" / "x")}):
                self.assertIsNone(teardown_stage.flush_evidence(args))

    def test_failure_warns_and_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "t"; src.mkdir()
            args = SimpleNamespace(test_dir=str(src))
            with mock.patch.dict(os.environ, {"TEZ_EVIDENCE_DIR": str(Path(td) / "ev")}), \
                 mock.patch("scrim.teardown_stage.shutil.copytree",
                            side_effect=OSError("disk gone")):
                self.assertIsNone(teardown_stage.flush_evidence(args))


class Ordering(unittest.TestCase):
    def test_flush_runs_before_any_destroy(self):
        order = []
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "ws" / ".automated-tests" / "run-7"; src.mkdir(parents=True)
            _tree(src)
            ev = Path(td) / "evidence"
            args = SimpleNamespace(test_dir=str(src), run_dir=str(src), keep_range=False,
                                   keep_templates=False)

            orig = teardown_stage.flush_evidence

            def flush(a):
                order.append("flush")
                return orig(a)
            with mock.patch.dict(os.environ, {"TEZ_EVIDENCE_DIR": str(ev)}), \
                 mock.patch.object(teardown_stage, "flush_evidence", side_effect=flush), \
                 mock.patch.object(teardown_stage, "teardown_red",
                                   side_effect=lambda *a, **k: order.append("red")), \
                 mock.patch.object(teardown_stage, "run_range_teardown",
                                   side_effect=lambda *a, **k: order.append("range")):
                teardown_stage.stage_teardown(args, creds=None)
            self.assertEqual(order, ["flush", "red", "range"])
            self.assertTrue((ev / "run-7" / "INTERACTION.md").exists())


if __name__ == "__main__":
    unittest.main()
