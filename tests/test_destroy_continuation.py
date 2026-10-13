"""Teardown continuation (loadtest-2026-09-30): a failed/interrupted destroy must be
completable by re-running the pipeline — foreign VMs skip-and-continue, stale state
locks clear when no terraform is alive. Offline; fake Proxmox API."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


from teardown import machinery as destroy_sweep_ops  # noqa: E402


import golden_ops  # noqa: E402


class StaleStateLock(unittest.TestCase):
    def test_stale_lock_cleared_when_no_terraform_runs(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / ".terraform.tfstate.lock.info"
            lock.write_text("{}")
            with patch.object(destroy_sweep_ops.subprocess, "run") as pgrep:
                pgrep.return_value.returncode = 1  # pgrep finds no terraform
                self.assertTrue(destroy_sweep_ops.clear_stale_state_lock(d))
            self.assertFalse(lock.exists())

    def test_lock_kept_while_terraform_process_alive(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / ".terraform.tfstate.lock.info"
            lock.write_text("{}")
            with patch.object(destroy_sweep_ops.subprocess, "run") as pgrep:
                pgrep.return_value.returncode = 0  # pgrep finds a live terraform
                self.assertFalse(destroy_sweep_ops.clear_stale_state_lock(d))
            self.assertTrue(lock.exists())

    def test_no_lock_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(destroy_sweep_ops.clear_stale_state_lock(d))


class GoldenDestroySkipsForeign(unittest.TestCase):
    def test_foreign_slot_skips_and_remaining_slots_continue(self):
        calls = []

        def fake_destroy(node, vmid, expect_tags=None):
            calls.append(vmid)
            if vmid == 1331:
                raise RuntimeError("refusing to destroy vmid 1331: foreign tags")

        with patch.object(golden_ops, "destroy_vm_if_exists", fake_destroy), \
             patch("builtins.print"):
            golden_ops.destroy_golden_set("proxmox", 1180, 3,
                                          expect_tags={"tezcatlipoca"})
        self.assertEqual(calls, [1330, 1331, 1332])

    def test_engine_template_foreign_skip_does_not_raise(self):
        def fake_destroy(node, vmid, expect_tags=None):
            raise RuntimeError("refusing to destroy vmid 1320: foreign tags")

        import engine_template_ops
        with patch.object(engine_template_ops, "destroy_vm_if_exists", fake_destroy), \
             patch("builtins.print"):
            engine_template_ops.destroy_engine_template("proxmox", 1180,
                                                 expect_tags={"tezcatlipoca"})


if __name__ == "__main__":
    unittest.main()
