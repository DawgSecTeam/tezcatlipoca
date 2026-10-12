"""The deploy-side trail wipe must dispatch Windows boxes to the Windows wipe.

deploy_lib.phases.final built its target rows with top-level t.get("windows"),
but managed_targets carry the box record nested under t["box"] — every box got
windows=False and dc01/win02 went down the Linux wipe path, hanging out their
180 s budget (live 2026-10-11: "dc01 ... NOT cleaned" in every firewall-era
deploy; the harness-side wipe masked it pre-T0).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deploy_lib.phases.final import _wipe_targets  # noqa: E402


def _target(name, template, ip, octet=2):
    return {"team": "team1", "vmid": 1400 + octet - 1, "node": "proxmox",
            "ip": ip, "box": {"name": name, "template": template,
                              "last_octet": octet}}


class WipeTargets(unittest.TestCase):
    def test_windows_flag_comes_from_the_nested_box_template(self):
        rows = _wipe_targets([
            _target("dc01", "base-windows-server-2022-fix", "192.168.120.2"),
            _target("web01", "base-ubuntu24.04-fix", "192.168.120.4", octet=4),
        ])
        by_name = {r["name"]: r for r in rows}
        self.assertTrue(by_name["dc01"]["windows"])
        self.assertFalse(by_name["web01"]["windows"])
        self.assertEqual({r["ip"] for r in rows},
                         {"192.168.120.2", "192.168.120.4"})

    def test_unmanaged_firewall_never_appears(self):
        """fw01 is unmanaged, so it never enters managed_targets; the shaping
        must not resurface it either way."""
        rows = _wipe_targets([_target("fw01", "pfsense-provision", "192.168.120.8",
                                      octet=8)])
        self.assertEqual(len(rows), 1)                    # pass-through shaping only


if __name__ == "__main__":
    unittest.main()
