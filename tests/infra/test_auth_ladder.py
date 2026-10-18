"""SSH retry ladders: definitive rejections
(Permission denied / host-key failure) must go straight to the guest-agent
fallback instead of burning the 8x15s ladder — only boot-time timeouts deserve
retries. Offline; fake subprocess/agent."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common import ssh_ops
from guests import ubuntu_auth_ops
from infra.guest_exec import DetachedExecResult

ENV = {"TEZ_PROXMOX_NODE": "proxmox", "TEZ_SSH_PUBLIC_KEY": "ssh-ed25519 AAAAtest"}
CTX = {"ssh_key_path": "/tmp/fake-key", "vm_username": "sysadmin",
       "scoring_engine_ip": "10.0.0.250", "box_username": "ubuntu"}
TARGETS = [{"ip": "192.168.210.242", "vmid": 1152, "identifier": 210}]


class FakeProc:
    def __init__(self, rc, stderr=""):
        self.returncode, self.stderr = rc, stderr


class AuthLadder(unittest.TestCase):
    def run_auth(self, results, agent_rc=(0, "", "")):
    # Run setup_ubuntu_auth with scripted ssh results; return (result, agent_calls).
        calls = {"ssh": 0, "agent": 0}

        def fake_run(*a, **kw):
            r = results[min(calls["ssh"], len(results) - 1)]
            calls["ssh"] += 1
            if isinstance(r, BaseException):
                raise r
            return r

        def fake_agent(node, vmid, script, log_path, timeout=900):
            calls["agent"] += 1
            rc, out, err = agent_rc
            return DetachedExecResult(rc, err or out, log_path)

        with patch.dict("os.environ", ENV):
            with patch.object(ssh_ops.subprocess, "run", side_effect=fake_run), \
                 patch.object(ssh_ops.time, "sleep"), \
                 patch.object(ubuntu_auth_ops, "guest_agent_exec_detached",
                              side_effect=fake_agent):
                ubuntu_auth_ops.setup_ubuntu_auth(TARGETS, CTX)
        return calls


    def test_definitive_rejection_goes_straight_to_agent(self):
        cases = [FakeProc(1, stderr="ssh: Permission denied (publickey,password)."),
                 FakeProc(255, stderr="Host key verification failed.")]
        for result in cases:
            with self.subTest(stderr=result.stderr):
                calls = self.run_auth([result])
                self.assertEqual(calls["ssh"], 1)   # no retry ladder
                self.assertEqual(calls["agent"], 1)

    def test_boot_time_failures_retry_full_ladder_then_agent(self):
        cases = [FakeProc(255, stderr="ssh: connect timed out"),
                 subprocess.TimeoutExpired("ssh", 40)]
        for result in cases:
            with self.subTest(result=result):
                calls = self.run_auth([result])
                self.assertEqual(calls["ssh"], 8)   # full ladder for boot-time flakiness
                self.assertEqual(calls["agent"], 1)


if __name__ == "__main__":
    unittest.main()
