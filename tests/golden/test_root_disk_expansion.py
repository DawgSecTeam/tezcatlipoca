"""Goldens need guest-side root growth: cloud-init grows only plain partition+fs, never ubuntu's LVM, so the post-boot script must chain growpart/pvresize/lvextend/resize2fs."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from golden import golden_disk_ops

CTX = {"box_username": "medic"}
TARGETS = [{"ip": "192.168.125.242", "vmid": 1242}, {"ip": "192.168.125.243", "vmid": 1243}]


def _run(rc=0, stdout=""):
    captured = {}

    def fake_ssh(ctx, ip, cmd, timeout=0, user="ubuntu"):
        captured.setdefault("cmds", []).append((ip, cmd, user))
        return type("R", (), {"returncode": rc, "stdout": stdout, "stderr": ""})()

    with patch.object(golden_disk_ops, "ssh_via_gateway", side_effect=fake_ssh):
        golden_disk_ops.expand_guest_root_disks(TARGETS, CTX)
    return captured


class ExpansionScript(unittest.TestCase):
    def test_lvm_grow_chain_present(self):
        captured = _run()
        script = captured["cmds"][0][1]
        for needle in ("growpart", "pvresize", "lvextend -l +100%FREE", "resize2fs",
                       "xfs_growfs", "pvs", "lvs"):
            self.assertIn(needle, script)

    def test_resize2fs_device_is_not_wrapped_in_literal_quotes(self):
        script = _run()["cmds"][0][1]
        self.assertIn('GROW_FS="resize2fs $SRC"', script)
        self.assertNotIn('resize2fs \\"', script)

    def test_btrfs_layout_grows_via_btrfs_resize(self):
        # Fedora: findmnt reports btrfs root as '/dev/sda3[/root]' — the subvol suffix must be stripped
        captured = _run()
        script = captured["cmds"][0][1]
        self.assertIn("${ROOT_SRC%%\\[*}", script)
        self.assertIn('btrfs) GROW_FS="btrfs filesystem resize max /"', script)
        self.assertIn('if [ -n "$DISK" ] && [ -n "$PART" ]', script)

    def test_script_is_valid_shell(self):
        # substring checks miss shell syntax errors; `sh -n` parses without executing
        captured = _run()
        script = captured["cmds"][0][1]
        proc = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"shell syntax error: {proc.stderr}")



    def test_failure_warns_and_continues_with_a_degradation(self):
        failed = type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        with patch.object(golden_disk_ops, "ssh_via_gateway", return_value=failed), \
             patch.object(golden_disk_ops.time, "sleep"), \
             patch.object(golden_disk_ops, "record_degradation") as degr:
            golden_disk_ops.expand_guest_root_disks(TARGETS[:1], CTX)
        degr.assert_called_once()
        self.assertIn("192.168.125.242", degr.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
