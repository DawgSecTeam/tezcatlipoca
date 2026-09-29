"""Multiple same-check-TYPE pins on one box + per-pin check overrides.

The regression-4x1 collapse: apache + roundcube both mapped to ("Web", 80) and the
per-box (TYPE, port) dedup silently dropped the second pin — 12 pins, 11 checks.
Identity is now (box, Display), matching Quotient's unique <box>-<Display> check-name
rule; overrides let one catalog config plant once and score twice. Offline."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import nakon_ops
from quotient.setup import build_event_conf, expected_service_names

BOXES = [
    {"name": "web01", "last_octet": 4, "template": "base-ubuntu24.04-fix"},
    {"name": "win01", "last_octet": 5, "template": "base-windows-server"},
]
TEAMS = {"team1": {"identifier": "120", "password": "x"}}

CTX = {
    "teams": TEAMS,
    "boxes_per_team": BOXES,
    "team_passwords": {"team1": "x"},
    "event_name": "t",
    "quotient_admin_password": "x",
}


def _web_displays(conf, box):
    by_box = {b["name"]: b for b in conf["box"]}
    return [c["Display"] for c in by_box[box].get("Web", [])]


class SameTypePins(unittest.TestCase):
    def test_two_web_pins_on_one_box_both_register(self):
        conf = build_event_conf(CTX, {"web01": ["apache", "roundcube", "bind"]})
        self.assertEqual(_web_displays(conf, "web01"), ["http", "roundcube"])
        self.assertEqual([c["Port"] for c in
                          next(b for b in conf["box"] if b["name"] == "web01")["Dns"]], [53])

    def test_duplicate_display_rejected_loudly(self):
        with self.assertRaises(SystemExit):
            build_event_conf(CTX, {"web01": ["apache", "nginx"]})

    def test_override_keys_split_plant_from_check(self):
        pins = ["Enable WinRM", "IIS HTTP", {"name": "IIS HTTP", "display": "iis-alt"}]
        conf = build_event_conf(CTX, {"win01": pins})
        self.assertEqual(_web_displays(conf, "win01"), ["iis", "iis-alt"])
        # same catalog config pinned twice plants once (nakon resolver dedups name+vars)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "box_services.json").write_text(json.dumps({"win01": pins}))
            (d / "box_vulns.json").write_text(json.dumps({"win01": []}))
            path = nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, d, "pw", box_username="medic")
            cfg = json.loads(Path(path).read_text())
            win = next(m for m in cfg["machines"] if m["name"] == "win01-team120")
            planted = win["configurations"]
            self.assertEqual(planted.count("IIS HTTP") +
                             sum(1 for c in planted if isinstance(c, dict) and c["name"] == "IIS HTTP"), 2)
            self.assertTrue(all("display" not in c for c in planted if isinstance(c, dict)))
            # write-back preserves the override for push_event_conf
            self.assertEqual(json.loads((d / "box_services.json").read_text())["win01"], pins)

    def test_port_path_status_overrides_shape_the_check(self):
        conf = build_event_conf(CTX, {"web01": [
            {"name": "apache", "display": "app", "port": 8080, "path": "/app/", "status": 200},
        ]})
        web = next(b for b in conf["box"] if b["name"] == "web01")["Web"][0]
        self.assertEqual((web["Display"], web["Port"], web["Url"][0]["Path"]), ("app", 8080, "/app/"))

    def test_path_override_on_non_url_check_rejected(self):
        with self.assertRaises(SystemExit):
            build_event_conf(CTX, {"web01": [{"name": "ssh", "path": "/x"}]})

    def test_expected_service_names_matches_emitted_checks(self):
        pins = {"web01": ["apache", "roundcube"],
                "win01": ["IIS HTTP", {"name": "IIS HTTP", "display": "iis-alt"}]}
        conf = build_event_conf(CTX, pins)
        emitted = {f'{b["name"]}-{c["Display"]}'
                   for b in conf["box"] for key, cfgs in b.items()
                   if key not in ("name", "ip") for c in cfgs}
        self.assertEqual(expected_service_names(pins, BOXES), emitted)
        self.assertEqual(expected_service_names(pins, BOXES),
                         {"web01-http", "web01-roundcube", "win01-iis", "win01-iis-alt"})

    def test_unknown_pin_still_warns_and_skips(self):
        conf = build_event_conf(CTX, {"web01": ["nosuchservice", "apache"]})
        self.assertEqual(_web_displays(conf, "web01"), ["http"])

    def test_unknown_override_key_warns_not_crashes(self):
        conf = build_event_conf(CTX, {"web01": [{"name": "apache", "displya": "typo"}]})
        self.assertEqual(_web_displays(conf, "web01"), ["http"])


if __name__ == "__main__":
    unittest.main()
