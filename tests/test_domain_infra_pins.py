"""Domain-infra pins (ADDS) + IIS HTTP / Alpine ssh service mappings.

ADDS is scored (Quotient Tcp 389) but must never ride the nakon machine list —
domain_ops plants it per team at phase 6 with per-team vars. Offline."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import nakon_ops
from constants import DOMAIN_INFRA_CONFIGS
from hardening_ops import ALPINE_SERVICES
from quotient.setup import _SERVICE_TO_CHECK, build_event_conf

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
    def test_constant_names_the_owned_configs(self):
        self.assertEqual(DOMAIN_INFRA_CONFIGS, {"ADDS", "Domain Join", "domain-join"})

    def test_adds_pin_scored_but_stripped_from_machine_list(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "box_services.json").write_text(json.dumps({
                "dc01": ["ADDS", "Enable WinRM"], "edge01": [],
            }))
            (d / "box_vulns.json").write_text(json.dumps({"dc01": [], "edge01": []}))
            path = nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, d, "pw", box_username="medic")
            cfg = json.loads(Path(path).read_text())
            dc = next(m for m in cfg["machines"] if m["name"] == "dc01-team101")
            names = [c if isinstance(c, str) else c["name"] for c in dc["configurations"]]
            self.assertNotIn("ADDS", names)          # domain_ops owns it
            self.assertIn("Enable WinRM", names)     # ordinary pins stay
            # the scored-service map keeps the pin so event.conf still sees it
            self.assertEqual(json.loads((d / "box_services.json").read_text())["dc01"],
                             ["ADDS", "Enable WinRM"])

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

    def test_service_mappings_present(self):
        self.assertEqual(_SERVICE_TO_CHECK["ADDS"], ("Tcp", {"Display": "ldap", "Port": 389}))
        self.assertEqual(_SERVICE_TO_CHECK["IIS HTTP"][0], "Web")
        self.assertEqual(_SERVICE_TO_CHECK["IIS HTTP"][1]["Port"], 80)

    def test_alpine_shim_maps_ssh(self):
        self.assertEqual(ALPINE_SERVICES["ssh"], ("openssh", "sshd"))


if __name__ == "__main__":
    unittest.main()
