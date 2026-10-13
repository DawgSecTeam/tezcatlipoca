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

from guests import domain_ops  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


destroy = _load("destroy_comp_s3", "destroy-competition.py")
from teardown import machinery as destroy_sweep_ops  # noqa: E402


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


class WindowsPreStop(unittest.TestCase):
    def test_hard_stops_every_team_clone(self):
    # Operator call 2026-09-30: forced stop + remove, no clean shutdowns —
    #         the pre-stop covers Linux clones too (their ACPI path just burns minutes
    #         on a disk that is about to be destroyed).
        calls = []

        def fake_api(method, path, **kwargs):
            calls.append((method, path))
            if method == "GET":
                return {"data": [{"name": "team1-dc01", "vmid": 1400, "status": "running"},
                                 {"name": "team1-win01", "vmid": 1401, "status": "running"},
                                 {"name": "team1-web01", "vmid": 1402, "status": "running"},
                                 {"name": "other-vm", "vmid": 999, "status": "running"}]}
            return {"data": "UPID:stop"}

        out = io.StringIO()
        with patch.object(destroy_sweep_ops, "proxmox_api", side_effect=fake_api), \
             patch.object(destroy_sweep_ops, "wait_for_proxmox_task"), \
             patch.dict(os.environ, {"TF_VAR_proxmox_node": "node"}), \
             contextlib.redirect_stdout(out):
            destroy_sweep_ops.pre_stop_windows_boxes(
                {"team1": {"identifier": "120"}},
                [{"name": "dc01", "template": "base-windows-server"},
                 {"name": "web01", "template": "base-ubuntu24.04-fix"}],
                default_node="node")
        stops = sorted(p for m, p in calls if m == "POST" and "/status/stop" in p)
        # dc01 (windows) AND web01 (linux) — every team clone, foreign VMs untouched
        self.assertEqual(stops, ["/nodes/node/qemu/1400/status/stop",
                                 "/nodes/node/qemu/1402/status/stop"])
        self.assertIn("Pre-stopped team1-dc01", out.getvalue())
        self.assertIn("Pre-stopped team1-web01", out.getvalue())


if __name__ == "__main__":
    unittest.main()
