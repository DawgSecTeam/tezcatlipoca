"""Teardown continuation (loadtest-2026-09-30): a failed/interrupted destroy must be
completable by re-running the pipeline — foreign VMs skip-and-continue, stale state
locks clear when no terraform is alive, and the leftover sweep matches ONLY the
competition's full tag set. Offline; fake Proxmox API."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


destroy = load_module("destroy_competition_under_test", "destroy-competition.py")
import golden_ops  # noqa: E402


def fake_api(vms, deleted):
    def _api(method, path, **kw):
        if method == "GET" and path.endswith("/qemu"):
            return {"data": vms}
        if method == "POST" and path.endswith("/status/stop"):
            return {"data": None}
        if method == "DELETE" and "/qemu/" in path:
            deleted.append(int(path.rsplit("/", 1)[1].split("?")[0]))
            return {"data": None}
        return {"data": None}
    return _api


def vm(vmid, name, tags, status="stopped"):
    return {"vmid": vmid, "name": name, "tags": tags, "status": status}


class LeftoverSweep(unittest.TestCase):
    def test_sweep_destroys_only_full_tag_matches(self):
        deleted = []
        vms = [
            vm(2300, "210-dc01", "tezcatlipoca;comp-loadtest-cr-a"),
            vm(2313, "211-win01", "tezcatlipoca;comp-loadtest-cr-a", status="running"),
            # partial/foreign overlap must NOT be candidates:
            vm(1080, "quotient-engine", "tezcatlipoca;comp-multinode"),
            vm(1333, "golden-web01", "template"),
            vm(9000, "workshop-box", ""),
        ]
        with patch.object(destroy, "proxmox_api", fake_api(vms, deleted)), \
             patch.object(destroy, "wait_for_proxmox_task"):
            destroy.sweep_tagged_leftovers(["proxmox"], "loadtest-cr-a")
        self.assertEqual(sorted(deleted), [2300, 2313])

    def test_sweep_is_silent_when_nothing_matches(self):
        deleted = []
        vms = [vm(1080, "quotient-engine", "tezcatlipoca;comp-multinode")]
        with patch.object(destroy, "proxmox_api", fake_api(vms, deleted)):
            destroy.sweep_tagged_leftovers(["proxmox"], "loadtest-cr-a")
        self.assertEqual(deleted, [])


class StaleStateLock(unittest.TestCase):
    def test_stale_lock_cleared_when_no_terraform_runs(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / ".terraform.tfstate.lock.info"
            lock.write_text("{}")
            with patch.object(destroy.subprocess, "run") as pgrep:
                pgrep.return_value.returncode = 1  # pgrep finds no terraform
                self.assertTrue(destroy.clear_stale_state_lock(d))
            self.assertFalse(lock.exists())

    def test_lock_kept_while_terraform_process_alive(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / ".terraform.tfstate.lock.info"
            lock.write_text("{}")
            with patch.object(destroy.subprocess, "run") as pgrep:
                pgrep.return_value.returncode = 0  # pgrep finds a live terraform
                self.assertFalse(destroy.clear_stale_state_lock(d))
            self.assertTrue(lock.exists())

    def test_no_lock_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(destroy.clear_stale_state_lock(d))


class GoldenDestroySkipsForeign(unittest.TestCase):
    def test_foreign_slot_skips_and_remaining_slots_continue(self):
        calls = []

        def fake_destroy(node, vmid, expect_tags=None, legacy_name=None):
            calls.append(vmid)
            if vmid == 1331:
                raise RuntimeError("refusing to destroy vmid 1331: foreign tags")

        with patch.object(golden_ops, "destroy_vm_if_exists", fake_destroy), \
             patch("builtins.print"):
            golden_ops.destroy_golden_set("proxmox", 1180, 3,
                                          expect_tags={"tezcatlipoca"})
        self.assertEqual(calls, [1330, 1331, 1332])

    def test_engine_template_foreign_skip_does_not_raise(self):
        def fake_destroy(node, vmid, expect_tags=None, legacy_name=None):
            raise RuntimeError("refusing to destroy vmid 1320: foreign tags")

        import template_ops
        with patch.object(template_ops, "destroy_vm_if_exists", fake_destroy), \
             patch("builtins.print"):
            template_ops.destroy_engine_template("proxmox", 1180,
                                                 expect_tags={"tezcatlipoca"})


if __name__ == "__main__":
    unittest.main()
