"""Interrupted-clone recovery (winad-testrun rec 3): clone marker, stale-lock unlock, orphan
zvol GC, and destroy's purge flags. Fake Proxmox API; no network."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import range_ops


class FakePVE:
    def __init__(self, vms=None, cfgs=None, vols=None, unlock_ok=True):
        self.vms, self.cfgs, self.vols = vms or [], cfgs or {}, vols or {}
        self.unlock_ok, self.calls = unlock_ok, []

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if method == "GET" and path.endswith("/qemu"):
            return {"data": self.vms}
        if method == "GET" and path.endswith("/config"):
            return {"data": self.cfgs.get(int(path.split("/")[-2]), {})}
        if method == "PUT" and path.endswith("/config"):
            if not self.unlock_ok:
                raise RuntimeError("403 only root can set 'lock' config")
            self.cfgs[int(path.split("/")[-2])].pop("lock", None)
            return {"data": None}
        if method == "GET" and path.endswith("/storage"):
            return {"data": [{"storage": "local-zfs"}]}
        if method == "GET" and path.endswith("/content"):
            return {"data": self.vols.get(kw["params"]["vmid"], [])}
        return {"data": "UPID:x"}


@patch.object(range_ops, "wait_for_proxmox_task", lambda *a, **k: None)
class InterruptedClone(unittest.TestCase):
    def test_marker_detected(self):
        fake = FakePVE(cfgs={4150: {"description": range_ops.clone_marker("c1")}})
        with patch.object(range_ops, "proxmox_api", fake):
            self.assertTrue(range_ops.has_clone_marker("n", 4150, "c1"))
            self.assertFalse(range_ops.has_clone_marker("n", 4150, "other"))

    def test_locked_untagged_is_unlocked_then_purged(self):
        fake = FakePVE(vms=[{"vmid": 4150, "name": "golden-web01", "status": "stopped"}],
                       cfgs={4150: {"lock": "clone"}})
        with patch.object(range_ops, "proxmox_api", fake):
            range_ops.destroy_vm_if_exists("n", 4150, expect_tags={"tezcatlipoca"})
        methods = [(m, p) for m, p, _ in fake.calls]
        self.assertIn(("PUT", "/nodes/n/qemu/4150/config"), methods)
        delete = [kw for m, p, kw in fake.calls if m == "DELETE"]
        self.assertEqual(delete[0]["params"]["destroy-unreferenced-disks"], 1)

    def test_unlock_forbidden_gives_exact_command(self):
        fake = FakePVE(vms=[{"vmid": 4150, "status": "stopped"}],
                       cfgs={4150: {"lock": "clone"}}, unlock_ok=False)
        with patch.object(range_ops, "proxmox_api", fake):
            with self.assertRaisesRegex(RuntimeError, "qm unlock 4150"):
                range_ops.destroy_vm_if_exists("n", 4150, expect_tags=set())

    def test_orphan_volumes_gc_only_matching_vmid(self):
        fake = FakePVE(vols={4150: [{"vmid": 4150, "volid": "local-zfs:vm-4150-disk-0"},
                                    {"vmid": 4151, "volid": "local-zfs:vm-4151-disk-0"}]})
        with patch.object(range_ops, "proxmox_api", fake):
            range_ops.destroy_vm_if_exists("n", 4150)
        deleted = [p for m, p, _ in fake.calls if m == "DELETE"]
        self.assertEqual(deleted, ["/nodes/n/storage/local-zfs/content/local-zfs:vm-4150-disk-0"])


if __name__ == "__main__":
    unittest.main()
