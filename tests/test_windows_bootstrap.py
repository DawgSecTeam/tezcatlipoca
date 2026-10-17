"""Windows bootstrap script content: the base-windows-server template ships all
three firewall profiles disabled, so firewall rules (sshd, RDP) and any later
firewall-rule effect are dead paper until the profiles are enabled
(caught live: RDP listener up, every external 3389 dial filtered).
Captured-script tests; no network."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from common import windows_ops


def _capture_bootstrap_script():
    captured = {}

    def fake_exec(node, vmid, script, timeout=0):
        captured["script"] = script
        return 0, "", ""

    with patch.object(windows_ops, "_wait_for_windows_setup_complete", return_value=True), \
         patch.object(windows_ops, "guest_agent_exec_windows", side_effect=fake_exec), \
         patch.object(windows_ops, "wait_for_guest_agent", return_value=True):
        windows_ops.bootstrap_windows_box("n", 1, "192.168.125.3", "192.168.125.1",
                                          "192.168.125.2", "pw")
    return captured["script"]


class WindowsTemplateRule(unittest.TestCase):
    def test_rule(self):
        for template, expected in [("tmpl-ubuntu-22", False), ("", False), ("win", True),
                                   ("WIN-2022", True), ("windows-server-2022", True)]:
            self.assertIs(windows_ops.is_windows_template(template), expected)


class FirewallProfiles(unittest.TestCase):
    def test_enables_all_profiles(self):
        script = _capture_bootstrap_script()
        self.assertIn("Set-NetFirewallProfile -All -Enabled True", script)

    def test_profile_enable_precedes_rule_enables(self):
        script = _capture_bootstrap_script()
        self.assertLess(script.index("Set-NetFirewallProfile"),
                        script.index("Enable-NetFirewallRule"))


class RdpRules(unittest.TestCase):
    def test_enables_rdp_rule_group(self):
        script = _capture_bootstrap_script()
        self.assertIn("RemoteDesktop-UserMode-In-TCP", script)
        self.assertIn("RemoteDesktop-UserMode-In-UDP", script)


if __name__ == "__main__":
    unittest.main()
