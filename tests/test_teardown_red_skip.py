"""A teardown for a run that never staged red must not touch the host's bad-auto config.

caught live: tearing down a workspace whose run died in phase 4 (no red01 was ever
deployed) ran `badauto destroy` with no per-comp config, so bad-auto read the HOST's default
config — which belonged to another session working in `tezcatlipoca-teardown-redesign/
competitions/tdred-2026`. Its identity guard refused (rc 17), and the whole teardown of a run
with no red agent came back NOT CLEAN. Had the stamps agreed, it would have destroyed another
session's red box.
"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scrim import teardown_stage


def _fake_proc(*_a, **_k):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class _Cfg:
    def __init__(self, exists):
        self._exists = exists

    def exists(self):
        return self._exists

    def __str__(self):
        return "/tmp/comp/.badauto-config.yaml"


class TeardownRedNeverStaged(unittest.TestCase):

    def _args(self):
        return SimpleNamespace(competition="scrim-one", comp_dir="/tmp/comp", red_vmid=999,
                               test_dir=None)

    def _run(self, *, comp_cfg_exists, manifest_vmid, calls):
        with mock.patch.object(teardown_stage.red_link, "pull_red_state", lambda *a, **k: None), \
             mock.patch.object(teardown_stage, "resolve_comp_dir",
                               lambda *a, **k: Path("/tmp/comp")), \
             mock.patch.object(teardown_stage.badauto_config_ops, "config_path",
                               lambda p: _Cfg(comp_cfg_exists)), \
             mock.patch.object(teardown_stage, "_manifest_red_vmid", lambda a: manifest_vmid), \
             mock.patch.object(teardown_stage, "_config_red_vmid", lambda p: None), \
             mock.patch.object(teardown_stage, "_red_vm_still_exists", lambda v: False), \
             mock.patch.object(teardown_stage.core, "read_comp_json", side_effect=OSError), \
             mock.patch.object(teardown_stage.procs, "run",
                               lambda *a, **k: calls.append(a[0]) or _fake_proc()):
            return teardown_stage.teardown_red(self._args(), env={})

    def test_no_badauto_destroy_without_a_per_comp_config_or_a_recorded_red_vmid(self):
        calls = []
        vmid = self._run(comp_cfg_exists=False, manifest_vmid=None, calls=calls)
        self.assertEqual(calls, [], "badauto destroy must not run against the host's default "
                                    "bad-auto config")
        self.assertEqual(vmid, 999)          # the proof still runs and reports this run's vmid

    def test_a_recorded_red_vmid_still_runs_badauto_destroy(self):
        calls = []
        self._run(comp_cfg_exists=False, manifest_vmid=999, calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertIn("destroy", calls[0])

    def test_a_per_comp_config_still_runs_badauto_destroy(self):
        calls = []
        self._run(comp_cfg_exists=True, manifest_vmid=None, calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertIn("--config", calls[0])


if __name__ == "__main__":
    unittest.main()
