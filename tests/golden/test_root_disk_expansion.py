"""Guest-side root-disk expansion for goldens (caught live: cloud-init
grows only plain partition+fs — ubuntu's LVM layout kept a 10G root LV inside the
30G hypervisor disk and the first big plant died with ENOSPC). expand_guest_root_disks
runs growpart/pvresize/lvextend/resize2fs post-boot; captured-command tests."""

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
        golden_disk_ops.expand_guest_root_disks(TARGETS, CTX)  # must not raise
    return captured


class ExpansionScript(unittest.TestCase):
    def test_lvm_grow_chain_present(self):
        captured = _run()
        script = captured["cmds"][0][1]
        for needle in ("growpart", "pvresize", "lvextend -l +100%FREE", "resize2fs",
                       "xfs_growfs", "pvs", "lvs"):
            self.assertIn(needle, script)

    def test_resize2fs_device_is_not_wrapped_in_literal_quotes(self):
    # $GROW_FS is expanded unquoted, so inner quotes become part of the path and
    #         resize2fs fails opening '"/dev/mapper/..."' (caught live: every LVM
    #         golden logged 8 failed attempts and a 'size unmeasurable' degradation).
        script = _run()["cmds"][0][1]
        self.assertIn('GROW_FS="resize2fs $SRC"', script)
        self.assertNotIn('resize2fs \\"', script)

    def test_btrfs_layout_grows_via_btrfs_resize(self):
    # Fedora cloud default: root is btrfs and findmnt reports the subvolume
    #         suffix ('/dev/sda3[/root]') — the parse must strip it and grow with
    #         `btrfs filesystem resize`, not resize2fs (caught live).
        captured = _run()
        script = captured["cmds"][0][1]
        self.assertIn("${ROOT_SRC%%\\[*}", script)
        self.assertIn('btrfs) GROW_FS="btrfs filesystem resize max /"', script)
        # growpart args are guarded so an unparseable source can't usage-error out
        self.assertIn('if [ -n "$DISK" ] && [ -n "$PART" ]', script)


    def test_script_is_valid_shell(self):
    # caught live: a '; ;' where a case branch needs ';;' survived the
    #         substring tests and died as a syntax error on the golden. sh -n parses without
    #         executing.
        captured = _run()
        script = captured["cmds"][0][1]
        proc = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"shell syntax error: {proc.stderr}")



    def test_failure_warns_and_continues_with_a_degradation(self):
        # caught live x3: the boot-window instability that breaks the grow must
        # warn, not kill phase 4 — the plant coverage gate catches a truly undersized root.
        failed = type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
        with patch.object(golden_disk_ops, "ssh_via_gateway", return_value=failed), \
             patch.object(golden_disk_ops.time, "sleep"), \
             patch.object(golden_disk_ops, "record_degradation") as degr:
            golden_disk_ops.expand_guest_root_disks(TARGETS[:1], CTX)  # must not raise
        degr.assert_called_once()
        self.assertIn("192.168.125.242", degr.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
