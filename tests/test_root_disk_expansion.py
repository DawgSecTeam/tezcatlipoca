"""Guest-side root-disk expansion for goldens (regression-4x1-2026-09-28: cloud-init
grows only plain partition+fs — ubuntu's LVM layout kept a 10G root LV inside the
30G hypervisor disk and the first big plant died with ENOSPC). expand_guest_root_disks
runs growpart/pvresize/lvextend/resize2fs post-boot; captured-command tests."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import golden_ops

CTX = {"box_username": "medic"}
TARGETS = [{"ip": "192.168.125.242", "vmid": 1242}, {"ip": "192.168.125.243", "vmid": 1243}]


def _run(rc=0, stdout=""):
    captured = {}

    def fake_ssh(ctx, ip, cmd, timeout=0, user="ubuntu"):
        captured.setdefault("cmds", []).append((ip, cmd, user))
        return type("R", (), {"returncode": rc, "stdout": stdout, "stderr": ""})()

    with patch.object(golden_ops, "ssh_via_gateway", side_effect=fake_ssh):
        golden_ops.expand_guest_root_disks(TARGETS, CTX)  # must not raise
    return captured


class ExpansionScript(unittest.TestCase):
    def test_lvm_grow_chain_present(self):
        captured = _run()
        script = captured["cmds"][0][1]
        for needle in ("growpart", "pvresize", "lvextend -l +100%FREE", "resize2fs",
                       "xfs_growfs", "pvs", "lvs"):
            self.assertIn(needle, script)

    def test_btrfs_layout_grows_via_btrfs_resize(self):
        """Fedora cloud default: root is btrfs and findmnt reports the subvolume
        suffix ('/dev/sda3[/root]') — the parse must strip it and grow with
        `btrfs filesystem resize`, not resize2fs (live-found 2026-09-29)."""
        captured = _run()
        script = captured["cmds"][0][1]
        self.assertIn("${ROOT_SRC%%\\[*}", script)
        self.assertIn('btrfs) GROW_FS="btrfs filesystem resize max /"', script)
        # growpart args are guarded so an unparseable source can't usage-error out
        self.assertIn('if [ -n "$DISK" ] && [ -n "$PART" ]', script)

    def test_btrfs_on_lvm_still_grows_the_pv_chain(self):
        captured = _run()
        script = captured["cmds"][0][1]
        # GROW_FS is selected by fstype BEFORE the source-type branch, so btrfs-on-LVM
        # gets growpart+pvresize+lvextend AND the btrfs grow
        self.assertLess(script.index("GROW_FS="), script.index("/dev/mapper/*"))

    def test_script_is_valid_shell(self):
        """Live-found 2026-09-29: a '; ;' where a case branch needs ';;' survived the
        substring tests and died as a syntax error on the golden. sh -n parses without
        executing."""
        captured = _run()
        script = captured["cmds"][0][1]
        proc = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"shell syntax error: {proc.stderr}")

    def test_runs_as_box_username_per_target(self):
        captured = _run()
        self.assertEqual([t[0] for t in captured["cmds"]],
                         [t["ip"] for t in TARGETS])
        self.assertTrue(all(user == "medic" for _, _, user in captured["cmds"]))

    def test_skips_gracefully_without_growpart(self):
        out = "growpart unavailable - skipping"
        _run(stdout=out)  # rc=0 → no raise even though nothing grew

    @staticmethod
    def _grow_fails_probe_returns(stdout):
        calls = {"n": 0}

        def fake(ctx, ip, cmd, timeout=0, user="ubuntu"):
            if "df -BK" in cmd:
                return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})
            calls["n"] += 1
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})

        return fake, calls

    def test_failure_raises_naming_the_box(self):
        fake, _ = self._grow_fails_probe_returns("6291455")  # just under the 6G floor
        with patch.object(golden_ops, "ssh_via_gateway", side_effect=fake), \
             patch.object(golden_ops.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                golden_ops.expand_guest_root_disks(TARGETS[:1], CTX)
            self.assertIn("192.168.125.242", str(cm.exception))

    def test_failure_with_unmeasurable_size_warns_and_continues(self):
        """Live-found 2026-09-29 x3: the boot-window instability that broke the grow
        also broke the size probe — an unmeasurable root must warn, not kill phase 4."""
        fake, _ = self._grow_fails_probe_returns("")  # probe answers nothing
        with patch.object(golden_ops, "ssh_via_gateway", side_effect=fake), \
             patch.object(golden_ops.time, "sleep"):
            golden_ops.expand_guest_root_disks(TARGETS[:1], CTX)  # must not raise

    def test_failure_with_adequate_root_continues(self):
        fake, _ = self._grow_fails_probe_returns("13631488")  # 13G >= floor
        with patch.object(golden_ops, "ssh_via_gateway", side_effect=fake), \
             patch.object(golden_ops.time, "sleep"):
            golden_ops.expand_guest_root_disks(TARGETS[:1], CTX)  # must not raise


if __name__ == "__main__":
    unittest.main()
