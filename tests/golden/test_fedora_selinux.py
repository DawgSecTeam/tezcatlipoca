"""Fedora goldens are sealed permissive; Debian/Ubuntu goldens are not touched.

The qemu-guest-agent domain is confined while SELinux enforces, so the golden build's SSH
channel (unconfined) sets /etc/selinux/config and the running box permissive before the
golden is converted. Offline: the SSH call is faked.
"""

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from golden import golden_ops  # noqa: E402


def _targets(templates):
    return [{"vm_name": f"golden-{i}", "ip": f"10.0.0.{i}",
             "box": {"template": template}}
            for i, template in enumerate(templates)]


class FedoraSelinux(unittest.TestCase):
    def _run(self, templates, returncode=0):
        calls = []

        def fake_ssh(ctx, ip, cmd, timeout=60, user="ubuntu"):
            calls.append((ip, cmd, user))
            return subprocess.CompletedProcess([], returncode,
                                               stdout="Permissive\n", stderr="denied")

        with patch.object(golden_ops, "ssh_via_gateway", side_effect=fake_ssh), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            golden_ops.set_fedora_selinux_permissive(
                _targets(templates), {"box_username": "medic"})
        return calls, out.getvalue()

    def test_fedora_gets_the_permissive_command_and_the_output_records_it(self):
        calls, out = self._run(["base-fedora44-fix"])
        self.assertEqual(len(calls), 1)
        ip, cmd, user = calls[0]
        self.assertEqual(ip, "10.0.0.0")
        self.assertEqual(user, "medic")
        self.assertIn("/etc/selinux/config", cmd)
        self.assertIn("SELINUX=permissive", cmd)
        self.assertIn("setenforce 0", cmd)
        self.assertIn("SELinux permissive", out)
        self.assertIn("golden-0", out)

    def test_debian_and_ubuntu_are_not_touched(self):
        calls, out = self._run(["base-debian12-fix", "base-ubuntu24.04-fix"])
        self.assertEqual(calls, [])
        self.assertEqual(out, "")

    def test_a_mixed_lineup_only_touches_the_fedora_box(self):
        calls, _out = self._run(["base-ubuntu24.04-fix", "base-fedora44-fix"])
        self.assertEqual([ip for ip, _cmd, _user in calls], ["10.0.0.1"])

    def test_a_failed_command_fails_the_build(self):
        with self.assertRaises(RuntimeError) as raised:
            self._run(["base-fedora44-fix"], returncode=1)
        self.assertIn("SELinux permissive", str(raised.exception))
        self.assertIn("golden-0", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
