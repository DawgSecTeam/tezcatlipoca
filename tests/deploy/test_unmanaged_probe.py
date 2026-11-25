"""An unmanaged in-path firewall must not be waited on as if it were a team box.

The firewall has no box account — the deploy configures it over its own channel — so a probe
as the box user can only burn the whole deadline and then report a healthy firewall as
unreachable (live: scrim-one prepared reset, run-de63a452, 15 minutes into every reset on a
firewall comp).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common import ssh_ops  # noqa: E402


class _Ok:
    returncode = 0


class UnmanagedFirewallTest(unittest.TestCase):
    def test_only_managed_boxes_are_probed(self):
        targets = [
            # fw01 is in_path + unmanaged: it has an address but no box account
            {"ip": "192.168.158.8", "vmid": 1786,
             "box": {"name": "fw01", "template": "pfsense-provision", "unmanaged": True}},
            {"ip": "192.168.158.4", "vmid": 1782,
             "box": {"name": "web01", "template": "base-debian13-lite"}},
        ]
        probed = []

        def fake_ssh(ctx, ip, cmd, **kw):
            probed.append(ip)
            return _Ok()

        with patch.object(ssh_ops, "ssh_via_gateway", side_effect=fake_ssh), \
                patch.object(ssh_ops, "record_degradation") as degrade:
            ssh_ops.wait_for_boxes_ssh({"box_username": "medic"}, targets, timeout=5)
        self.assertEqual(probed, ["192.168.158.4"])
        self.assertFalse(degrade.called, "the firewall must not be recorded as unreachable")
