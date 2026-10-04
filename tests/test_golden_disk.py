"""Golden-set disk sizing (svc-matrix: the 15 GB ubuntu template disk filled
mid-plant on splunk's .deb). ensure_golden_disk_size grows the clone's root
disk to the box's disk_gb before first boot — grow-only, cdrom/cloudinit
entries ignored. Fake Proxmox API; no network."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import golden_disk_ops

UBUNTU_CFG = {
    "scsi0": "ssd:vm-1204-disk-0,iothread=1,size=15G",
    "scsihw": "virtio-scsi-single",
    "ide2": "local-lvm:vm-1204-cloudinit,media=cdrom,size=4M",
}


class FakePVE:
    def __init__(self, cfg):
        self.cfg, self.resizes = cfg, []

    def __call__(self, method, path, **kw):
        if method == "GET" and path.endswith("/config"):
            return {"data": self.cfg}
        if method == "PUT" and path.endswith("/resize"):
            self.resizes.append(kw["data"])
            self.cfg[kw["data"]["disk"]] = self.cfg[kw["data"]["disk"]].replace(
                "size=15G", f"size={kw['data']['size']}")
            return {"data": None}
        return {"data": None}


class RootDiskDetection(unittest.TestCase):
    def test_scsi_root_seen_cdrom_and_cloudinit_skipped(self):
        key, gb = golden_disk_ops._root_disk_gb(UBUNTU_CFG, 1204)
        self.assertEqual("scsi0", key)
        self.assertEqual(15, gb)

    def test_megabyte_sizes_parse(self):
        key, gb = golden_disk_ops._root_disk_gb(
            {"virtio0": "local:vm-1-disk-0,size=20480M"}, 1)
        self.assertEqual("virtio0", key)
        self.assertEqual(20, gb)

    def test_no_disk(self):
        key, gb = golden_disk_ops._root_disk_gb({"ide2": "local-lvm:vm-1-cloudinit,media=cdrom,size=4M"}, 1)
        self.assertIsNone(key)
        self.assertEqual(0, gb)


class EnsureGoldenDiskSize(unittest.TestCase):
    def test_grows_when_target_bigger(self):
        fake = FakePVE(dict(UBUNTU_CFG))
        with patch.object(golden_disk_ops, "proxmox_api", fake):
            golden_disk_ops.ensure_golden_disk_size("n", 1204, 30)
        self.assertEqual([{"disk": "scsi0", "size": "30G"}], fake.resizes)

    def test_noop_when_target_fits(self):
        fake = FakePVE(dict(UBUNTU_CFG))
        with patch.object(golden_disk_ops, "proxmox_api", fake):
            golden_disk_ops.ensure_golden_disk_size("n", 1204, 15)
        self.assertEqual([], fake.resizes)

    def test_noop_when_disk_gb_unset(self):
        fake = FakePVE(dict(UBUNTU_CFG))
        with patch.object(golden_disk_ops, "proxmox_api", fake):
            golden_disk_ops.ensure_golden_disk_size("n", 1204, None)
        self.assertEqual([], fake.resizes)

    def test_warns_without_root_disk(self):
        fake = FakePVE({"ide2": "local-lvm:vm-1-cloudinit,media=cdrom,size=4M"})
        with patch.object(golden_disk_ops, "proxmox_api", fake):
            golden_disk_ops.ensure_golden_disk_size("n", 1, 30)  # must not raise
        self.assertEqual([], fake.resizes)


if __name__ == "__main__":
    unittest.main()
