"""The phase-6 credlist SQL re-ensure: mysql final-stage plants rebuild the auth
tables, so the users fix_services created pre-plant must be re-added after it."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import service_fixup_ops
from service_fixup_ops import mysql_credlist_reensure_script, reensure_mysql_credlist_users


CREDS = {"lead": "pw-lead", "records": "pw-records"}


class ScriptTest(unittest.TestCase):
    def test_creates_every_credlist_account(self):
        script = mysql_credlist_reensure_script(CREDS.items())
        for name in CREDS:
            self.assertIn(f"CREATE USER IF NOT EXISTS '{name}'@'%'", script)
            self.assertIn(f"GRANT ALL PRIVILEGES ON *.* TO '{name}'@'%'", script)
        self.assertIn("FLUSH PRIVILEGES;", script)

    def test_survives_client_tls_refusal(self):
        script = mysql_credlist_reensure_script(CREDS.items())
        self.assertIn("--skip-ssl", script)
        # heredoc target + chmod + 4 fallback redirects + cleanup
        self.assertEqual(script.count("/tmp/tz-credlist.sql"), 7)
        self.assertIn("rm -f /tmp/tz-credlist.sql", script)


class ReensureTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("TF_VAR_proxmox_node", "test-pve")
        self.calls = []

        def fake_ssh(ctx, ip, cmd, timeout=60, user=None):
            self.calls.append(("ssh", ip, cmd))
            out = type("R", (), {"returncode": 0, "stderr": ""})()
            return out

        def fake_agent(node, vmid, script, timeout=120):
            self.calls.append(("agent", vmid, script))
            return (0, "", "")

        self._orig = (service_fixup_ops.ssh_via_gateway, service_fixup_ops.guest_agent_exec_root)
        service_fixup_ops.ssh_via_gateway = fake_ssh
        service_fixup_ops.guest_agent_exec_root = fake_agent

    def tearDown(self):
        service_fixup_ops.ssh_via_gateway, service_fixup_ops.guest_agent_exec_root = self._orig

    def _comp_dir(self, services):
        d = tempfile.mkdtemp(prefix="tz-credlist-")
        (Path(d) / "box_services.json").write_text(json.dumps(services))
        return Path(d)

    def test_only_mysql_pinned_boxes_touched(self):
        comp = self._comp_dir({"db01": ["ssh", "mysql"], "web01": ["ssh", "nginx"]})
        targets = [
            {"box_name": "db01", "ip": "192.168.120.6", "vmid": 1404},
            {"box_name": "web01", "ip": "192.168.120.4", "vmid": 1402},
        ]
        reensure_mysql_credlist_users(comp, targets, {"box_username": "medic"}, CREDS)
        ips = [ip for kind, ip, _ in self.calls if kind == "ssh"]
        self.assertEqual(ips, ["192.168.120.6"])

    def test_ssh_payload_carries_the_sql(self):
        comp = self._comp_dir({"db01": ["mysql"]})
        targets = [{"box_name": "db01", "ip": "192.168.120.6", "vmid": 1404}]
        reensure_mysql_credlist_users(comp, targets, {"box_username": "medic"}, CREDS)
        kind, _ip, cmd = self.calls[0]
        self.assertEqual(kind, "ssh")
        self.assertIn("tz-mysql-credlist.sh", cmd)

    def test_no_mysql_pins_means_no_calls(self):
        comp = self._comp_dir({"web01": ["ssh", "nginx"]})
        targets = [{"box_name": "web01", "ip": "192.168.120.4", "vmid": 1402}]
        reensure_mysql_credlist_users(comp, targets, {"box_username": "medic"}, CREDS)
        self.assertEqual(self.calls, [])

    def test_var_dict_pins_count_as_mysql(self):
        comp = self._comp_dir({"db01": [{"name": "mysql", "vars": {"x": "y"}}]})
        targets = [{"box_name": "db01", "ip": "192.168.120.6", "vmid": 1404}]
        reensure_mysql_credlist_users(comp, targets, {"box_username": "medic"}, CREDS)
        self.assertEqual(len([c for c in self.calls if c[0] == "ssh"]), 1)


if __name__ == "__main__":
    unittest.main()
