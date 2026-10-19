"""Windows bootstrap script content: the base-windows-server template ships all
three firewall profiles disabled, so firewall rules (sshd, RDP) and any later
firewall-rule effect are dead paper until the profiles are enabled
(caught live: RDP listener up, every external 3389 dial filtered).
Captured-script tests; no network."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common import windows_ops


def _capture_bootstrap_script(features=()):
    captured = {}

    def fake_exec(node, vmid, script, timeout=0):
        captured["script"] = script
        return 0, "", ""

    with patch.object(windows_ops, "_wait_for_windows_setup_complete", return_value=True), \
         patch.object(windows_ops, "guest_agent_exec_windows", side_effect=fake_exec), \
         patch.object(windows_ops, "wait_for_guest_agent", return_value=True):
        windows_ops.bootstrap_windows_box("n", 1, "192.168.125.3", "192.168.125.1",
                                          "192.168.125.2", "pw", features=features)
    return captured["script"]




class FirewallProfiles(unittest.TestCase):

    def test_profile_enable_precedes_rule_enables(self):
        script = _capture_bootstrap_script()
        self.assertLess(script.index("Set-NetFirewallProfile"),
                        script.index("Enable-NetFirewallRule"))


class IisManagement(unittest.TestCase):
    def test_management_tools_install_when_requested(self):
        script = _capture_bootstrap_script(features=("Web-Mgmt-Tools",))
        self.assertIn("Install-WindowsFeature -Name @('Web-Mgmt-Tools')", script)
        self.assertIn("-IncludeManagementTools", script)

    def test_no_feature_install_by_default(self):
        self.assertNotIn("Install-WindowsFeature", _capture_bootstrap_script())


class RdpRules(unittest.TestCase):
    def test_enables_rdp_rule_group(self):
        script = _capture_bootstrap_script()
        self.assertIn("RemoteDesktop-UserMode-In-TCP", script)
        self.assertIn("RemoteDesktop-UserMode-In-UDP", script)


if __name__ == "__main__":
    unittest.main()
