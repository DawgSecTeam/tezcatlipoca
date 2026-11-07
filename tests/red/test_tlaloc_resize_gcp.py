"""red01's disk grow on gcp: no qm disk handle, no cluster lookup — a compute PD
born at red_disk_gb, so the grow is at most a disks.resize plus the in-guest SSH
grow (the cluster lookup crashed phase 7 live: 'vmid 1160 not found in the cluster')."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from estate import state as estate_state  # noqa: E402
from providers.gcp import naming  # noqa: E402
from red import tlaloc_ops  # noqa: E402


class _Proc:
    returncode = 0

    def __init__(self, stdout):
        self.stdout = stdout


class ResizeRedDiskOnGcp(unittest.TestCase):
    def test_grows_the_boot_pd_and_runs_the_guest_grow_over_ssh(self):
        with tempfile.TemporaryDirectory() as d:
            estate_state.write_flat(Path(d), {"comp_name": "c1",
                                              "run_id": "run-0e6474eb",
                                              "platform": "gcp",
                                              "scoring_vm_id": 1000})
            seen = {}

            def boot_disk(creds, name):
                seen["name"] = name
                return "projects/t/global/disks/d1"

            def grow(creds, disk, target_gb):
                seen["disk"], seen["target"] = disk, target_gb
                return 30

            calls = []

            def runner(argv, timeout=None):
                seen["probe"] = argv[-1] if argv[-1] == "true" else seen.get("probe")
                seen["ssh"] = argv
                return _Proc("/dev/mapper/ubuntu--vg-root 58G 3G 53G 6% /\n")

            expected = naming.instance_name(naming.prefix("c1", "run-0e6474eb"),
                                             "tlaloc-red01")
            with patch("providers.gcp.client.load_credentials",
                        return_value={"project_id": "t"}), \
                    patch("providers.gcp.instance_ops.boot_disk", side_effect=boot_disk), \
                    patch("providers.gcp.instance_ops.resize_boot_disk", side_effect=grow):
                grown = tlaloc_ops.resize_red_disk(Path(d), "34.1.1.1", 1160,
                                                   ssh_key="/k", target_gb=60,
                                                   runner=runner)

        self.assertEqual(seen["name"], expected)
        self.assertEqual(seen["disk"], "projects/t/global/disks/d1")
        self.assertEqual(seen["target"], 60)
        self.assertIn("sysadmin@34.1.1.1", seen["ssh"])
        # the bounds-wait probes on tlaloc's own channel first
        self.assertEqual(seen["probe"], "true")
        self.assertTrue(grown["ok"])
        self.assertEqual(grown["before_gb"], 30)
        self.assertEqual(grown["after_gb"], 58)

    def test_a_proxmox_state_still_takes_the_cluster_path(self):
        with tempfile.TemporaryDirectory() as d:
            estate_state.write_flat(Path(d), {"comp_name": "c1",
                                              "run_id": "run-0e6474eb",
                                              "platform": "proxmox",
                                              "scoring_vm_id": 1000})
            seen = {}

            def px(method, path, params=None, data=None):
                seen.setdefault("px", []).append((method, path, data))
                if path == "/cluster/resources":
                    return {"data": [{"vmid": 1160, "node": "pve"}]}
                if path == "/nodes/pve/qemu/1160/config":
                    return {"data": {"scsi0": "local-lvm:vm-1160-disk-0,size=20G"}}
                if method == "PUT" and path == "/nodes/pve/qemu/1160/resize":
                    return {"data": "UPID:pve:qemu:1160"}
                raise AssertionError(f"unfake'd PVE call {method} {path}")

            def runner(argv, timeout=None):
                seen["ssh"] = argv
                return _Proc("/dev/sda1 58G 3G 53G 6% /\n")

            with patch("infra.client.proxmox_api", side_effect=px), \
                    patch("infra.client.wait_for_proxmox_task"):
                grown = tlaloc_ops.resize_red_disk(Path(d), "10.200.0.10", 1160,
                                                   ssh_key="/k", target_gb=60,
                                                   runner=runner)

        self.assertIn(("PUT", "/nodes/pve/qemu/1160/resize",
                       {"disk": "scsi0", "size": "60G"}), seen["px"])
        self.assertFalse(grown["grew"] is None)
        self.assertTrue(grown["ok"])


if __name__ == "__main__":
    unittest.main()
