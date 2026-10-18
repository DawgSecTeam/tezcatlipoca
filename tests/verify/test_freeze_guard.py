"""Freeze guard: `verify --freeze` refuses a dirty worktree, and the commit it records
is read at deploy time. The frozen record's `code` must actually be consulted: the drift
gate keys on per-template input hashes, so without it a commit taken after --freeze would
run silently on code the freeze never verified. Offline; git state is patched."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from engine import template_freeze
from engine.template_freeze import frozen_state, save_template_hashes

import verifier.context as v_context  # noqa: E402
import subprocess  # noqa: E402
import verifier.freeze as v_freeze  # noqa: E402


class _Args:
    pass


def _comp_dir():
    tmp = tempfile.TemporaryDirectory()
    comp = Path(tmp.name)
    save_template_hashes(comp, engine={"hash": "e1"}, golden={"web01": {"hash": "g1"}})
    (comp / "nakon-config.json").write_text(json.dumps(
        {"machines": [{"name": "web01-team101", "os": "base-ubuntu24.04-fix"}]}))
    return tmp, comp


class _Popen:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout




class FreezeRefusesDirtyTree(unittest.TestCase):
    def _freeze(self, comp, dirty):
        stdout = io.StringIO()
        with patch.object(v_freeze, "git_dirty_lines", return_value=dirty), \
             patch.object(template_freeze, "git_commit_info",
                          return_value={"commit": "c" * 40, "dirty": False}), \
             contextlib.redirect_stdout(stdout):
            ok = v_freeze.do_freeze(comp, _Args(), {"services": True}, True)
        return ok, stdout.getvalue()

    def test_dirty_code_refuses_and_writes_nothing(self):
        tmp, comp = _comp_dir()
        self.addCleanup(tmp.cleanup)
        ok, out = self._freeze(comp, [" M deploy.py", "?? comp/estate-state.json"])
        self.assertFalse(ok)
        self.assertNotIn("FROZEN", out)
        self.assertIn("--unfreeze --confirm-unfreeze", out)
        self.assertIsNone(frozen_state(comp))

    def test_dirty_non_code_freezes_with_note(self):
        # A post-run worktree is always dirty from generated state (estate-state.json,
        # nodes.json, .env backups, comp JSON) — that must not block a freeze.
        tmp, comp = _comp_dir()
        self.addCleanup(tmp.cleanup)
        ok, out = self._freeze(comp, [" M comp/estate-state.json", "?? nodes.json",
                                      "?? .env.pre-fixture-20260101"])
        self.assertTrue(ok)
        self.assertIn("FROZEN", out)
        self.assertIn("non-code", out)
        self.assertIsNotNone(frozen_state(comp))



class FrozenCodeDrift(unittest.TestCase):
    def _drift(self, frozen_code, current):
        frozen = {"frozen_at": "2026-01-02 10:00:00", "code": frozen_code}
        with patch.object(template_freeze, "git_commit_info", return_value=current):
            return template_freeze.frozen_code_drift(frozen)


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




class CodePathDirty(unittest.TestCase):
    def test_code_suffixes_dirty(self):
        for line in [" M deploy.py", "?? newmod.py", "A  x.sh",
                     "R  old.py -> new.py", ' M "weird name.py"']:
            self.assertTrue(template_freeze.code_path_dirty([line]), line)



class GitCommitInfoScope(unittest.TestCase):
    def test_dirty_ignores_generated_state(self):
        with patch.object(template_freeze.subprocess, "run",
                          side_effect=[_Popen(stdout="a" * 40),
                                       _Popen(stdout=" M comp/estate-state.json\n?? nodes.json\n")]):
            self.assertFalse(template_freeze.git_commit_info()["dirty"])



class TemplateStampAuthority(unittest.TestCase):
# The reuse decision reads the template's OWN node stamp, not the per-comp-dir
# estate-state.json — a fresh worktree and every dashboard workspace lack it, so a
# decision keyed on the document would rebuild every template a --teams-only teardown had
# deliberately kept.



    def test_unstamped_template_is_not_reuse(self):
        # No `tezcatlipoca-m4 hash=` description reads as None: never reuse what we
        # cannot attribute to these exact inputs.
        with patch.object(template_freeze, "stored_template_hash", lambda node, vmid: None):
            self.assertFalse(template_freeze.template_stamp_current("node", 7, "h1"))


if __name__ == "__main__":
    unittest.main()
