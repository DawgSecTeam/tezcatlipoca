"""Domain-infra pins (ADDS) + IIS HTTP / Alpine ssh service mappings.

ADDS is scored (Quotient Tcp 389) but must never ride the nakon machine list —
domain_ops plants it per team at phase 6 with per-team vars. Offline."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from nakonops import nakon_config_ops
from quotient.setup import build_event_conf

BOXES = [
    {"name": "dc01", "last_octet": 2, "template": "base-windows-server"},
    {"name": "win01", "last_octet": 3, "template": "base-windows-server"},
    {"name": "edge01", "last_octet": 6, "template": "base-alpine3.23-fix"},
]
TEAMS = {"team1": {"identifier": "101", "password": "x"}}

CTX = {
    "teams": TEAMS,
    "boxes_per_team": BOXES,
    "team_passwords": {"team1": "x"},
    "event_name": "t",
    "quotient_admin_password": "x",
}


class DomainInfraPins(unittest.TestCase):

    def test_event_conf_maps_adds_and_iis(self):
        conf = build_event_conf(CTX, {
            "dc01": ["ADDS", "Enable WinRM"],
            "edge01": ["nginx"],
            "win01": ["IIS HTTP", "New SMB Share"],
        })
        by_box = {b["name"]: b for b in conf["box"]}
        tcp_ports = {c["Port"] for c in by_box["dc01"]["Tcp"]}
        self.assertEqual(tcp_ports, {389, 5985})   # ADDS + WinRM, no skipped names
        web_ports = {c["Port"] for c in by_box["win01"]["Web"]}
        self.assertEqual(web_ports, {80})          # IIS HTTP


if __name__ == "__main__":
    unittest.main()
