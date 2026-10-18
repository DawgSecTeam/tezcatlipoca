"""Guest-agent exec: detached long-running path + diagnosable timeouts.

Why this file exists (caught live..10-01):
  * five exec logs die on `did not finish within 120s` — the agent exec channel is a
    poll loop with a caller-set budget, so any plant that legitimately outlives the
    budget dies mid-plant and leaves its partial effects on the box;
  * the timeout error named only the vmid and the budget, so an operator had nothing
    to act on even though the agent was holding partial output;
  * `wait_for_guest_agent` returned a silent False, which is how one vmid escalated
    600 -> 1200 -> 2700 -> 2900s without anyone noticing it was never coming back.

Offline; the Proxmox API is faked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from infra import guest_exec
import infra.guest_exec  # noqa: F401  (facade must keep exporting the helpers)


class _FakeApi:
# Scripted proxmox_api: records calls, answers agent exec POST/GET and VM state.

    def __init__(self, statuses=None):
        self.calls = []
        self.statuses = list(statuses or [])
        self.status_index = 0

    def __call__(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/agent/exec") and method == "POST":
            return {"data": {"pid": 4242}}
        if path.endswith("/agent/exec-status"):
            status = self.statuses[min(self.status_index, len(self.statuses) - 1)]
            self.status_index += 1
            return {"data": status}
        if path.endswith("/config"):
            return {"data": {"name": "golden-web01", "lock": "clone"}}
        if path.endswith("/status/current"):
            return {"data": {"status": "running"}}
        if path.endswith("/agent/ping"):
            raise RuntimeError("no agent")
        raise AssertionError(f"unexpected call {method} {path}")


class ExecTimeoutDiagnosis(unittest.TestCase):
    def test_root_timeout_names_pid_budget_and_partial_output(self):
        api = _FakeApi([{"exited": False, "out-data": "installing nginx\n",
                         "err-data": "E: lock held by apt\n"}])
        with patch.object(guest_exec, "proxmox_api", api), \
             patch.object(guest_exec.time, "sleep"), \
             patch.object(guest_exec.time, "time", side_effect=[0, 0, 999]):
            with self.assertRaises(RuntimeError) as ctx:
                infra.guest_exec.guest_agent_exec_root("proxmox", 1234, "apt-get install -y nginx",
                                                timeout=120)
        msg = str(ctx.exception)
        self.assertIn("pid=4242", msg)
        self.assertIn("within 120s", msg)
        self.assertIn("installing nginx", msg)          # partial stdout is included
        self.assertIn("lock held by apt", msg)          # partial stderr is included
        self.assertIn("guest_agent_exec_detached()", msg)  # points at the right tool



class DetachedExec(unittest.TestCase):
    def _run(self, tails, rc_line=None):
    # Run guest_agent_exec_detached with scripted log tails.
        api = _FakeApi()
        seen = {"tail_calls": 0}

        def fake_root(node, vmid, script, timeout=60, shell="bash"):
            seen["tail_calls"] += 1
            text = tails[min(seen["tail_calls"] - 1, len(tails) - 1)]
            return 0, text, ""

        with patch.object(guest_exec, "proxmox_api", api), \
             patch.object(guest_exec, "guest_agent_exec_root", side_effect=fake_root), \
             patch.object(guest_exec.time, "sleep"), \
             patch.object(guest_exec.time, "time", side_effect=list(range(0, 5000, 5))):
            result = infra.guest_exec.guest_agent_exec_detached(
                "proxmox", 1234, "apt-get install -y nginx",
                "/tmp/tz-detached-1234.log", timeout=300, poll_interval=1)
        return result, api, seen

    def test_log_path_with_spaces_is_quoted(self):
        api = _FakeApi()

        def fake_root(node, vmid, script, timeout=60, shell="bash"):
            return 0, "__TZ_DETACHED_RC=0\n", ""

        with patch.object(guest_exec, "proxmox_api", api), \
             patch.object(guest_exec, "guest_agent_exec_root", side_effect=fake_root), \
             patch.object(guest_exec.time, "sleep"), \
             patch.object(guest_exec.time, "time", side_effect=list(range(0, 5000, 5))):
            infra.guest_exec.guest_agent_exec_detached(
                "proxmox", 1234, "true", "/tmp/tz log/run 1.log", timeout=300)
        wrapper = [c for c in api.calls if c[0] == "POST"][0][2]["data"]["command"][2]
        self.assertIn("'/tmp/tz log/run 1.log'", wrapper)

    def test_polls_until_the_marker_then_returns_the_rc(self):
        result, _, seen = self._run(["starting\n", "still going\n",
                                     "all done\n__TZ_DETACHED_RC=0\n"])
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.log_path, "/tmp/tz-detached-1234.log")
        self.assertIn("all done", result.log)
        self.assertGreaterEqual(seen["tail_calls"], 3)   # polled, did not give up early

    def test_nonzero_rc_is_reported_not_swallowed(self):
        result, _, _ = self._run(["boom\n__TZ_DETACHED_RC=100\n"])
        self.assertEqual(result.rc, 100)

    def test_agent_hiccup_while_polling_does_not_abort_the_run(self):
        api = _FakeApi()
        calls = {"n": 0}

        def flaky_root(node, vmid, script, timeout=60, shell="bash"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("agent busy")
            return 0, "ok\n__TZ_DETACHED_RC=0\n", ""

        with patch.object(guest_exec, "proxmox_api", api), \
             patch.object(guest_exec, "guest_agent_exec_root", side_effect=flaky_root), \
             patch.object(guest_exec.time, "sleep"), \
             patch.object(guest_exec.time, "time", side_effect=list(range(0, 5000, 5))):
            result = infra.guest_exec.guest_agent_exec_detached(
                "proxmox", 1234, "true", "/tmp/x.log", timeout=300)
        self.assertEqual(result.rc, 0)



class WaitForAgentDiagnosis(unittest.TestCase):
    def test_failed_wait_reports_vm_state_instead_of_being_silent(self):
        api = _FakeApi()
        out = []

        with patch.object(guest_exec, "proxmox_api", api), \
             patch.object(guest_exec.time, "sleep"), \
             patch.object(guest_exec.time, "time", side_effect=[0, 0, 999]), \
             patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            ok = infra.guest_exec.wait_for_guest_agent("proxmox", 147, timeout=600)
        self.assertFalse(ok)
        joined = "\n".join(out)
        self.assertIn("vmid 147", joined)
        self.assertIn("status=running", joined)      # the diagnosis, not just False
        self.assertIn("name=golden-web01", joined)

    def test_diagnosis_never_raises_when_the_api_is_gone(self):
        def dead(*a, **kw):
            raise RuntimeError("API unreachable")

        out = []
        with patch.object(guest_exec, "proxmox_api", dead), \
             patch.object(guest_exec.time, "sleep"), \
             patch.object(guest_exec.time, "time", side_effect=[0, 0, 999]), \
             patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertFalse(infra.guest_exec.wait_for_guest_agent("proxmox", 7, timeout=5))
        self.assertIn("state unavailable", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
