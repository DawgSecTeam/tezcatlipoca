"""Guest-side root-disk expansion for goldens (regression-4x1-2026-09-28: cloud-init
grows only plain partition+fs — ubuntu's LVM layout kept a 10G root LV inside the
30G hypervisor disk and the first big plant died with ENOSPC). expand_guest_root_disks
runs growpart/pvresize/lvextend/resize2fs post-boot; captured-command tests."""

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

    def test_runs_as_box_username_per_target(self):
        captured = _run()
        self.assertEqual([t[0] for t in captured["cmds"]],
                         [t["ip"] for t in TARGETS])
        self.assertTrue(all(user == "medic" for _, _, user in captured["cmds"]))

    def test_skips_gracefully_without_growpart(self):
        out = "growpart unavailable - skipping"
        _run(stdout=out)  # rc=0 → no raise even though nothing grew

    def test_failure_raises_naming_the_box(self):
        with patch.object(golden_ops, "ssh_via_gateway",
                          return_value=type("R", (), {"returncode": 1, "stdout": "",
                                                      "stderr": "boom"})):
            with self.assertRaises(RuntimeError) as cm:
                golden_ops.expand_guest_root_disks(TARGETS, CTX)
            self.assertIn("192.168.125.242", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
