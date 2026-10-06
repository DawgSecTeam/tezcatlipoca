import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import domain_ops
import windows_ops
from redeploy_plant_ops import _domain_config_path


class DomainOrderingTests(unittest.TestCase):
    def test_domain_rebuild_uses_full_machine_config_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            full = comp_dir / "nakon-config.json"
            stage = comp_dir / ".nakon-postclone.json"
            full.write_text("{}")
            stage.write_text("{}")
            self.assertEqual(_domain_config_path(comp_dir, stage), full)
            full.unlink()
            self.assertEqual(_domain_config_path(comp_dir, stage), stage)

    def test_wait_for_adws_returns_domain_sid(self):
        with patch.object(windows_ops, "guest_agent_exec_windows",
                          return_value=(0, "S-1-5-21-1-2-3\n", "")), \
             patch.object(windows_ops.time, "sleep"):
            self.assertEqual(
                windows_ops.wait_for_adws("node", 123, timeout=1),
                "S-1-5-21-1-2-3",
            )

    def test_adws_and_dns_gates_precede_domain_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            (comp_dir / "domain_roles.json").write_text(
                json.dumps({"dc01": "dc", "win01": "member"})
            )
            config = comp_dir / "nakon.json"
            config.write_text(json.dumps({"machines": [
                {"name": "dc01-team105", "ip": "192.168.105.6", "configurations": []},
                {"name": "win01-team105", "ip": "192.168.105.7", "configurations": []},
            ]}))
            boxes = [
                {"name": "dc01", "template": "base-windows-server"},
                {"name": "win01", "template": "base-windows-server"},
            ]
            events = []

            def run_config(machine, configs, *args, **kwargs):
                events.append(("config", machine["name"], [
                    c if isinstance(c, str) else c["name"] for c in configs
                ]))

            def wait_adws(*args, **kwargs):
                events.append(("adws",))
                return "S-1-5-21-1-2-3"

            def wait_dns(*args, **kwargs):
                events.append(("dns",))
                return True

            with patch.dict(os.environ, {"TF_VAR_proxmox_node": "node"}), \
                 patch.object(domain_ops, "guest_agent_exec_windows",
                             return_value=(0, "0|WORKGROUP", "")), \
                 patch.object(domain_ops, "_run_single_nakon_config", side_effect=run_config), \
                 patch.object(domain_ops, "wait_for_guest_agent", return_value=True), \
                 patch.object(domain_ops, "wait_for_windows_sshd"), \
                 patch.object(domain_ops, "wait_for_adws", side_effect=wait_adws), \
                 patch.object(domain_ops, "wait_for_dc_dns", side_effect=wait_dns), \
                 patch.object(domain_ops, "dns_repoint_windows_box"), \
                 patch.object(domain_ops, "_probe_joined", side_effect=[False, True]), \
                 patch.object(domain_ops.time, "sleep"):
                domain_ops.deploy_domain_configs(
                    {"team1": {"identifier": "105"}}, boxes, comp_dir,
                    config, "key", "scoring", "10.0.0.248", "password",
                )

            adws_index = next(i for i, event in enumerate(events) if event[0] == "adws")
            ad_config_index = next(i for i, event in enumerate(events)
                                   if event[0] == "config" and event[1] == "dc01-team105"
                                   and "Add User Account" in event[2])
            dns_index = next(i for i, event in enumerate(events) if event[0] == "dns")
            join_index = next(i for i, event in enumerate(events)
                              if event[0] == "config" and event[1] == "win01-team105")
            self.assertLess(adws_index, ad_config_index)
            self.assertLess(dns_index, join_index)
            self.assertEqual(events[ad_config_index][2][:2],
                             ["Add User Account", "Elevate User Account"])
            self.assertEqual(events[ad_config_index][2][2:],
                             ["Disable System Firewall", "Removing all auditing"])


if __name__ == "__main__":
    unittest.main()
