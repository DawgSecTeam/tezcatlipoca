"""The terraform provider cache must outlive the checkout that used it.

The deploy inits with TF_PLUGIN_CACHE_DIR, so `<comp>/terraform/.terraform/providers` holds links
into that cache. When the cache lived inside the engine tree (and the dashboard deletes its
per-run worktree at the end of every run), those links dangled and the teardown could not run
`terraform destroy` at all — four attempts, estate left unverified (2026-10-14, run-3aaf37f1).
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from common import terraform_workdir
from teardown import machinery


class PluginCacheLocation(unittest.TestCase):
    def test_default_is_outside_the_checkout(self):
        with mock.patch.dict(os.environ, {"TEZ_TF_PLUGIN_CACHE": ""}):
            cache = terraform_workdir.terraform_plugin_cache_dir()
        self.assertFalse(str(cache).startswith(str(terraform_workdir.REPO_ROOT) + os.sep),
                         f"{cache} lives inside the checkout and dies with it")

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"TEZ_TF_PLUGIN_CACHE": "/tmp/tez-cache-test"}):
            cache = terraform_workdir.terraform_plugin_cache_dir()
        self.assertEqual(str(cache), "/tmp/tez-cache-test")
        self.assertTrue(cache.is_dir())


class DestroyInitsFirst(unittest.TestCase):
    def test_destroy_runs_init_before_destroy(self):
        calls = []

        def fake_run_terraform(args, cwd=None, env=None, timeout=None, check=True, grace=60):
            calls.append(args)
            return type("P", (), {"returncode": 0})()

        with mock.patch.object(machinery, "run_terraform", side_effect=fake_run_terraform), \
                mock.patch.object(machinery, "clear_stale_state_lock"), \
                mock.patch.object(machinery, "sweep_tagged_leftovers"):
            ok = machinery.destroy_with_recovery({}, "/tmp/tf", [], None)
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "init", calls)
        self.assertEqual(calls[1][0], "destroy", calls)


if __name__ == "__main__":
    unittest.main()


class RepairRecordedKeyPath(unittest.TestCase):
    def _tfvars(self, tmp_path, recorded):
        p = tmp_path / "terraform.tfvars.json"
        p.write_text(json.dumps({"ssh_private_key_path": recorded, "engine_mgmt_ip": "10.0.0.252"}))
        return p

    def test_dead_recorded_path_is_replaced_from_the_env(self):
        import json
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live = tmp / "tez-proxmox"
            live.write_text("KEY")
            p = self._tfvars(tmp, str(tmp / "runs" / "gone-engine" / "proxmox"))
            changed = machinery.repair_ssh_key_path(p, {"TF_VAR_ssh_private_key_path": str(live)})
            self.assertTrue(changed)
            self.assertEqual(json.loads(p.read_text())["ssh_private_key_path"], str(live))
            # everything else is preserved
            self.assertEqual(json.loads(p.read_text())["engine_mgmt_ip"], "10.0.0.252")

    def test_live_recorded_path_is_left_alone(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live = tmp / "key"
            live.write_text("KEY")
            p = self._tfvars(tmp, str(live))
            self.assertFalse(machinery.repair_ssh_key_path(
                p, {"TF_VAR_ssh_private_key_path": str(tmp / "other")}))

    def test_no_live_replacement_never_invents_one(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            p = self._tfvars(tmp, str(tmp / "gone" / "proxmox"))
            self.assertFalse(machinery.repair_ssh_key_path(
                p, {"TF_VAR_ssh_private_key_path": str(tmp / "also-missing")}))
