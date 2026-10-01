"""Stream-3 deploy-robustness fixes from the shakedown-5x4 closeout: AD-misconfig
replay on resume (plants keyed on their own marker, not first promotion), the
stale scoring-round-loop WARN (+ --fix-round-loop), and the Windows pre-stop
that keeps a dead-agent DC from hanging terraform destroy."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import domain_ops  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verify = _load("verify_comp_s3", "verify-competition.py")
destroy = _load("destroy_comp_s3", "destroy-competition.py")


class AdMisconfigReplay(unittest.TestCase):
    COMP = {"team1": {"identifier": "105"}}
    BOXES = [
        {"name": "dc01", "template": "base-windows-server"},
        {"name": "win01", "template": "base-windows-server"},
    ]

    def _run(self, tmp, promoted):
        comp_dir = Path(tmp)
        (comp_dir / "domain_roles.json").write_text(
            json.dumps({"dc01": "dc", "win01": "member"}))
        config = comp_dir / "nakon.json"
        config.write_text(json.dumps({"machines": [
            {"name": "dc01-team105", "ip": "192.168.105.6", "configurations": []},
            {"name": "win01-team105", "ip": "192.168.105.7", "configurations": []},
        ]}))
        events = []

        def run_config(machine, configs, *args, **kwargs):
            events.append((machine["name"], [c if isinstance(c, str) else c["name"]
                                             for c in configs]))

        # DC probe answer "role|domain": role 0 = WORKGROUP member, 4 = promoted
        dc_probe = "0|WORKGROUP" if not promoted else "4|team105.local"
        with patch.dict(os.environ, {"TF_VAR_proxmox_node": "node"}), \
             patch.object(domain_ops, "guest_agent_exec_windows",
                          return_value=(0, dc_probe, "")), \
             patch.object(domain_ops, "_run_single_nakon_config", side_effect=run_config), \
             patch.object(domain_ops, "wait_for_guest_agent", return_value=True), \
             patch.object(domain_ops, "wait_for_windows_sshd"), \
             patch.object(domain_ops, "wait_for_adws", return_value="S-1-5-21-1-2-3"), \
             patch.object(domain_ops, "wait_for_dc_dns", return_value=True), \
             patch.object(domain_ops, "dns_repoint_windows_box"), \
             patch.object(domain_ops, "_probe_joined", return_value=True), \
             patch.object(domain_ops.time, "sleep"):
            domain_ops.deploy_domain_configs(
                self.COMP, self.BOXES, comp_dir, config,
                "key", "scoring", "10.0.0.248", "password")
        return events

    def test_promoted_dc_with_lost_marker_still_plants(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = self._run(tmp, promoted=True)
            dc = [cfgs for name, cfgs in events if name == "dc01-team105"]
            # no ADDS re-promotion, but the 4 AD misconfigs DO plant
            self.assertEqual(dc, [["Add User Account", "Elevate User Account",
                                   "Disable System Firewall", "Removing all auditing"]])

    def test_promoted_dc_with_marker_skips_plants(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".nakon-domain-team1-ad-misconfigs.json").write_text("{}")
            events = self._run(tmp, promoted=True)
            self.assertEqual([name for name, _ in events if name == "dc01-team105"], [])


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self._payload, self.posts = payload, []

    def get(self, url, timeout=None):
        return _Resp(self._payload)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return _Resp({"ok": True})


class RoundLoopCheck(unittest.TestCase):
    # /api/engine real schema (live-confirmed 2026-09-29 on the .150 orphan engine):
    # snake_case, current_round_time is an RFC3339 string whose Go zero value
    # ("0001-01-01T00:00:00Z") means the loop is stopped; paused = running: false.
    def test_paused_engine_not_warned(self):
        s = _Session({"running": False, "competition_started": True,
                      "current_round_time": "0001-01-01T00:00:00Z"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verify.check_round_loop("http://e", s)
        self.assertIn("paused", out.getvalue())
        self.assertEqual(s.posts, [])

    def test_stale_zero_time_warns_with_remediation(self):
        s = _Session({"running": True, "competition_started": True,
                      "last_round": {"StartTime": "2020-01-01T00:00:00Z"},
                      "current_round_time": "0001-01-01T00:00:00Z"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verify.check_round_loop("http://e", s)
        self.assertIn("WARN", out.getvalue())
        self.assertIn("/api/competition/start", out.getvalue())
        self.assertEqual(s.posts, [])

    def test_fix_round_loop_runs_both_posts(self):
        s = _Session({"running": True, "competition_started": True,
                      "last_round": {"StartTime": "2020-01-01T00:00:00Z"},
                      "current_round_time": "0001-01-01T00:00:00Z"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verify.check_round_loop("http://e", s, fix=True)
        self.assertIn("--fix-round-loop", out.getvalue())
        self.assertEqual([u for u, _ in s.posts], ["http://e/api/competition/start",
                                                   "http://e/api/engine/pause"])

    def test_healthy_loop_passes(self):
        s = _Session({"running": True, "competition_started": True,
                      "last_round": {"StartTime": "2020-01-01T00:00:00Z"},
                      "current_round_time": "2026-09-29T07:00:00Z"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            verify.check_round_loop("http://e", s)
        self.assertIn("advancing", out.getvalue())


class WindowsPreStop(unittest.TestCase):
    def test_hard_stops_every_team_clone(self):
        """Operator call 2026-09-30: forced stop + remove, no clean shutdowns —
        the pre-stop covers Linux clones too (their ACPI path just burns minutes
        on a disk that is about to be destroyed)."""
        calls = []

        def fake_api(method, path, **kwargs):
            calls.append((method, path))
            if method == "GET":
                return {"data": [{"name": "120-dc01", "vmid": 1400, "status": "running"},
                                 {"name": "120-win01", "vmid": 1401, "status": "running"},
                                 {"name": "120-web01", "vmid": 1402, "status": "running"},
                                 {"name": "other-vm", "vmid": 999, "status": "running"}]}
            return {"data": "UPID:stop"}

        out = io.StringIO()
        with patch.object(destroy, "proxmox_api", side_effect=fake_api), \
             patch.object(destroy, "wait_for_proxmox_task"), \
             patch.dict(os.environ, {"TF_VAR_proxmox_node": "node"}), \
             contextlib.redirect_stdout(out):
            destroy.pre_stop_windows_boxes(
                {"team1": {"identifier": "120"}},
                [{"name": "dc01", "template": "base-windows-server"},
                 {"name": "web01", "template": "base-ubuntu24.04-fix"}],
                default_node="node")
        stops = sorted(p for m, p in calls if m == "POST" and "/status/stop" in p)
        # dc01 (windows) AND web01 (linux) — every team clone, foreign VMs untouched
        self.assertEqual(stops, ["/nodes/node/qemu/1400/status/stop",
                                 "/nodes/node/qemu/1402/status/stop"])
        self.assertIn("Pre-stopped 120-dc01", out.getvalue())
        self.assertIn("Pre-stopped 120-web01", out.getvalue())


if __name__ == "__main__":
    unittest.main()
