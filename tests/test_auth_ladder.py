"""SSH retry ladders (cyberrange loadtest 2026-09-30): definitive rejections
(Permission denied / host-key failure) must go straight to the guest-agent
fallback instead of burning the 8x15s ladder — only boot-time timeouts deserve
retries. Offline; fake subprocess/agent."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import hardening_ops

ENV = {"TF_VAR_proxmox_node": "proxmox", "TF_VAR_ssh_public_key": "ssh-ed25519 AAAAtest"}
CTX = {"ssh_key_path": "/tmp/fake-key", "vm_username": "sysadmin",
       "scoring_engine_ip": "10.0.0.250", "box_username": "ubuntu"}
TARGETS = [{"ip": "192.168.210.242", "vmid": 1152, "identifier": 210}]


class FakeProc:
    def __init__(self, rc, stderr=""):
        self.returncode, self.stderr = rc, stderr


class AuthLadder(unittest.TestCase):
    def run_auth(self, results, agent_rc=(0, "", "")):
        """Run setup_ubuntu_auth with scripted ssh results; return (result, agent_calls)."""
        calls = {"ssh": 0, "agent": 0}

        def fake_run(*a, **kw):
            r = results[min(calls["ssh"], len(results) - 1)]
            calls["ssh"] += 1
            if isinstance(r, BaseException):
                raise r
            return r

        def fake_agent(node, vmid, script, timeout=120):
            calls["agent"] += 1
            return agent_rc

        with patch.dict("os.environ", ENV):
            with patch.object(hardening_ops.subprocess, "run", side_effect=fake_run), \
                 patch.object(hardening_ops.time, "sleep"), \
                 patch.object(hardening_ops, "guest_agent_exec_root",
                              side_effect=fake_agent), \
                 patch.object(hardening_ops, "wait_for_guest_agent"):
                hardening_ops.setup_ubuntu_auth(TARGETS, CTX)
        return calls

    def test_first_try_success_never_touches_agent(self):
        calls = self.run_auth([FakeProc(0)])
        self.assertEqual((calls["ssh"], calls["agent"]), (1, 0))

    def test_permission_denied_goes_straight_to_agent(self):
        denied = FakeProc(1, stderr="ssh: Permission denied (publickey,password).")
        calls = self.run_auth([denied])
        self.assertEqual(calls["ssh"], 1)   # no retry ladder
        self.assertEqual(calls["agent"], 1)

    def test_host_key_failure_goes_straight_to_agent(self):
        badkey = FakeProc(255, stderr="Host key verification failed.")
        calls = self.run_auth([badkey])
        self.assertEqual(calls["ssh"], 1)
        self.assertEqual(calls["agent"], 1)

    def test_generic_failure_still_retries_then_agent(self):
        flaky = FakeProc(255, stderr="ssh: connect timed out")
        calls = self.run_auth([flaky])
        self.assertEqual(calls["ssh"], 8)   # full ladder for boot-time flakiness
        self.assertEqual(calls["agent"], 1)

    def test_timeout_ladder_then_agent(self):
        calls = self.run_auth([subprocess.TimeoutExpired("ssh", 40)])
        self.assertEqual(calls["ssh"], 8)
        self.assertEqual(calls["agent"], 1)


if __name__ == "__main__":
    unittest.main()
