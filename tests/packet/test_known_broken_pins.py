"""The machine-readable KNOWN_BROKEN_CONFIGS list is enforced at generate/compile/validate time. Offline."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from nakonops import nakon_config_ops
from nakonops import nakon_pin_ops
from common.constants import KNOWN_BROKEN_CONFIGS
from config.packet_ops import validate_profile

REMAINING = {
    "mailenable-cleartext-mail-win",
    "MSSQL",
}




class ValidateKnownBrokenPins(unittest.TestCase):

    def test_dict_pin_is_checked_too(self):
        with self.assertRaises(SystemExit):
            nakon_pin_ops._validate_known_broken_pins(
                [{"name": "mailenable-cleartext-mail-win", "vars": {}}], "pins for 'web01'")




BOXES = [{"name": "web01", "template": "base-ubuntu24.04-fix", "last_octet": 10}]
TEAMS = {"team1": {"identifier": "101"}}


def _comp(tmp, vulns=None):
    comp = Path(tmp) / "comp"
    comp.mkdir()
    if vulns is not None:
        (comp / "box_vulns.json").write_text(json.dumps(vulns))
    return comp


class WindowsLocalUserPasswordGate(unittest.TestCase):
    def test_invalid_password_refused_before_machine_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, {"web01": [{"name": "local-user-win", "vars": {
                "USERNAME": "svc_imaging", "PASSWORD": "Imaging2026!",
                "FULLNAME": "Imaging Sync Service"}}]})
            with self.assertRaisesRegex(SystemExit, "full name"):
                nakon_config_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")



class GenerateNakonConfigGate(unittest.TestCase):
    def test_recorded_pin_keeps_existing_competition_deployable(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, {"web01": ["mailenable-cleartext-mail-win"]})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                nakon_config_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")
            self.assertIn("WARNING", out.getvalue())
            machines = json.loads((comp / "nakon-config.json").read_text())["machines"]
            self.assertIn("mailenable-cleartext-mail-win", machines[0]["configurations"])

    def test_fresh_randomized_selection_is_filtered_not_fatal(self):
        # Fresh picks of broken configs are filtered, not fatal — no abort over a pin nobody chose.
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            out = io.StringIO()
            with patch.object(nakon_config_ops, "_nakon_randomize",
                              return_value=(["apache", "airship-webapp"],
                                             ["mailenable-cleartext-mail-win", "suid-find",
                                              "rc-local-persistence"])), \
                 patch.object(nakon_config_ops, "catalog_required_var_names",
                              return_value=frozenset({"rc-local-persistence"})):
                with contextlib.redirect_stdout(out):
                    nakon_config_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")
            self.assertIn("known-broken", out.getvalue())
            self.assertIn("mailenable-cleartext-mail-win", out.getvalue())
            self.assertIn("catalog-var", out.getvalue())
            machines = json.loads((comp / "nakon-config.json").read_text())["machines"]
            self.assertIn("suid-find", machines[0]["configurations"])
            self.assertNotIn("mailenable-cleartext-mail-win", machines[0]["configurations"])
            self.assertNotIn("rc-local-persistence", machines[0]["configurations"])
            self.assertIn("apache", machines[0]["configurations"])
            self.assertNotIn("airship-webapp", machines[0]["configurations"])


class DropUnplantableBare(unittest.TestCase):
    def test_keeps_healthy_and_drops_string_and_dict_pins(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = nakon_pin_ops._drop_unplantable_bare(
                ["suid-find", "mailenable-cleartext-mail-win",
                 {"name": "mailenable-cleartext-mail-win", "vars": {}}],
                "fresh selection for 'web01'")
        self.assertEqual(kept, ["suid-find"])
        msg = out.getvalue()
        for name in ("mailenable-cleartext-mail-win", "mailenable-cleartext-mail-win"):
            self.assertIn(name, msg)
        self.assertIn("fresh selection for 'web01'", msg)


    def test_literal_var_config_is_dropped_but_identity_only_survives(self):
        # The drop predicate is literal-scoped, not name-based: hosts-redirect-linux has a literal (HOSTS).
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = nakon_pin_ops._drop_unplantable_bare(
                ["hosts-redirect-linux", "apache"], "fresh selection for 'web01'")
        self.assertEqual(kept, ["apache"])
        self.assertIn("requires operator vars", out.getvalue())
        self.assertIn("hosts-redirect-linux", out.getvalue())

    def test_catalog_var_configs_dropped_from_fresh_selection(self):
        # Regression: catalog required_vars missing from REQUIRED_VARS were picked bare and
        # the bundle lint aborted the whole deploy.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = nakon_pin_ops._drop_unplantable_bare(
                ["raw-socket-beacon", "desktop-autostart", "nginx"],
                "fresh selection for 'web01'")
        self.assertEqual(kept, ["nginx"])
        self.assertIn("requires operator vars", out.getvalue())



class PacketCompilerGate(unittest.TestCase):
    def test_packet_pinning_a_broken_config_fails_validate(self):
        profile = {
            "event": {"comp_id": "pkt-broken", "name": "Broken Pin",
                      "scenario": "scenario line", "difficulty": 1},
            "boxes": [{"name": "web01", "template": "base-ubuntu24.04-fix",
                       "last_octet": 4, "fidelity": "exact"}],
            "services": [{"box": "web01", "name": "Web", "port": 80,
                          "pin": "mailenable-cleartext-mail-win", "display": "http",
                          "fidelity": "exact"}],
        }
        errors = validate_profile(profile)
        self.assertTrue(any("known-broken" in e and "mailenable-cleartext-mail-win" in e
                            for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
