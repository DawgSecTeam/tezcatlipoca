"""Freeze guard: `verify --freeze` refuses a dirty worktree, and the commit it records
is read at deploy time. The archived claim was wrong — `.frozen.json`'s `code` was
written but never read, and the drift gate keys on per-template input hashes, so a
commit taken after --freeze used to run silently on code the freeze never verified.
Offline; git state is patched."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import template_freeze
import template_ops

_SPEC = importlib.util.spec_from_file_location("verify_competition", _REPO / "verify-competition.py")
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)
import subprocess  # noqa: E402
import verifier.freeze as v_freeze  # noqa: E402


class _Args:
    windows_domain_validated = False


def _comp_dir():
    tmp = tempfile.TemporaryDirectory()
    comp = Path(tmp.name)
    (comp / ".template-hashes.json").write_text(json.dumps(
        {"engine": {"hash": "e1"}, "golden": {"web01": {"hash": "g1"}}}))
    (comp / "nakon-config.json").write_text(json.dumps(
        {"machines": [{"name": "web01-team101", "os": "base-ubuntu24.04-fix"}]}))
    return tmp, comp


class _Popen:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


class GitDirtyLines(unittest.TestCase):
    def test_reads_porcelain_at_the_repo_root(self):
        with patch.object(subprocess, "run",
                          return_value=_Popen(stdout=" M docs/x.md\n?? comp/y.json\n")) as run:
            self.assertEqual(verify.git_dirty_lines(), [" M docs/x.md", "?? comp/y.json"])
        self.assertEqual(run.call_args.kwargs["cwd"], str(verify.REPO_ROOT))

    def test_none_when_git_cannot_answer(self):
        with patch.object(subprocess, "run", return_value=_Popen(returncode=128)):
            self.assertIsNone(verify.git_dirty_lines())
        with patch.object(subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(verify.git_dirty_lines())


class FreezeRefusesDirtyTree(unittest.TestCase):
    def _freeze(self, comp, dirty):
        stdout = io.StringIO()
        with patch.object(v_freeze, "git_dirty_lines", return_value=dirty), \
             patch.object(template_freeze, "git_commit_info",
                          return_value={"commit": "c" * 40, "dirty": False}), \
             contextlib.redirect_stdout(stdout):
            ok = verify.do_freeze(comp, _Args(), {"services": True}, True)
        return ok, stdout.getvalue()

    def test_dirty_code_refuses_and_writes_nothing(self):
        tmp, comp = _comp_dir()
        self.addCleanup(tmp.cleanup)
        ok, out = self._freeze(comp, [" M deploy.py", "?? comp/placement.json"])
        self.assertFalse(ok)
        self.assertNotIn("FROZEN", out)
        self.assertIn("--unfreeze --confirm-unfreeze", out)
        self.assertFalse((comp / ".frozen.json").exists())

    def test_dirty_non_code_freezes_with_note(self):
        # A post-run worktree is always dirty from generated state (placement.json,
        # nodes.json, .env backups, comp JSON) — that must not block a freeze.
        tmp, comp = _comp_dir()
        self.addCleanup(tmp.cleanup)
        ok, out = self._freeze(comp, [" M comp/placement.json", "?? nodes.json",
                                      "?? .env.pre-cde-20260929"])
        self.assertTrue(ok)
        self.assertIn("FROZEN", out)
        self.assertIn("non-code", out)
        self.assertTrue((comp / ".frozen.json").exists())

    def test_clean_tree_freezes(self):
        tmp, comp = _comp_dir()
        self.addCleanup(tmp.cleanup)
        ok, out = self._freeze(comp, [])
        self.assertTrue(ok)
        self.assertIn("FROZEN", out)
        self.assertTrue((comp / ".frozen.json").exists())


class FrozenCodeDrift(unittest.TestCase):
    def _drift(self, frozen_code, current):
        frozen = {"frozen_at": "2026-10-02 10:00:00", "code": frozen_code}
        with patch.object(template_freeze, "git_commit_info", return_value=current):
            return template_ops.frozen_code_drift(frozen)

    def test_same_commit_clean_is_silent(self):
        self.assertIsNone(self._drift({"commit": "a" * 40, "dirty": False},
                                      {"commit": "a" * 40, "dirty": False}))

    def test_moved_commit_warns_naming_both(self):
        msg = self._drift({"commit": "a" * 40, "dirty": False},
                          {"commit": "b" * 40, "dirty": False})
        self.assertIn("WARNING", msg)
        self.assertIn("a" * 12, msg)
        self.assertIn("b" * 12, msg)
        self.assertIn("--unfreeze --confirm-unfreeze", msg)

    def test_dirty_worktree_at_deploy_warns(self):
        msg = self._drift({"commit": "a" * 40, "dirty": False},
                          {"commit": "a" * 40, "dirty": True})
        self.assertIn("WARNING", msg)
        self.assertIn("DIRTY", msg)

    def test_dirty_freeze_record_warns(self):
        msg = self._drift({"commit": "a" * 40, "dirty": True},
                          {"commit": "a" * 40, "dirty": False})
        self.assertIn("WARNING", msg)

    def test_unknown_commit_is_silent(self):
        self.assertIsNone(self._drift({"commit": "unknown", "dirty": None},
                                      {"commit": "b" * 40, "dirty": False}))
        self.assertIsNone(self._drift({}, {"commit": "b" * 40, "dirty": False}))


class CodePathDirty(unittest.TestCase):
    def test_code_suffixes_dirty(self):
        for line in [" M deploy.py", "?? newmod.py", " M terraform/main.tf", "A  x.sh",
                     "R  old.py -> new.py", ' M "weird name.py"']:
            self.assertTrue(template_ops.code_path_dirty([line]), line)

    def test_generated_state_not_dirty(self):
        for line in [" M competitions/x/placement.json", "?? nodes.json",
                     "?? .env.pre-cde-20260929", " M docs/known-issues.md",
                     "?? competitions/x/.nakon-golden-slot1.json"]:
            self.assertFalse(template_ops.code_path_dirty([line]), line)


class GitCommitInfoScope(unittest.TestCase):
    def test_dirty_ignores_generated_state(self):
        with patch.object(template_freeze.subprocess, "run",
                          side_effect=[_Popen(stdout="a" * 40),
                                       _Popen(stdout=" M comp/placement.json\n?? nodes.json\n")]):
            self.assertFalse(template_ops.git_commit_info()["dirty"])

    def test_dirty_sees_code(self):
        with patch.object(template_freeze.subprocess, "run",
                          side_effect=[_Popen(stdout="a" * 40), _Popen(stdout=" M deploy.py\n")]):
            self.assertTrue(template_ops.git_commit_info()["dirty"])


if __name__ == "__main__":
    unittest.main()
