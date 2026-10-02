"""Known-broken catalog configs are a machine-readable list
(constants.KNOWN_BROKEN_CONFIGS) enforced at generate/compile time, replacing the prose +
per-competition-JSON pruning that let a new comp silently re-pin tftpd (Noble dpkg wedge),
sshd-force-sftp (kills SSH) or a Windows user-policy config. A competition that already
records such a pin keeps deploying — warning, not error — so the historical comps that
predate the gate (cde-2026, pfsense-rvb, scrim-live all pin local-user-win) stay
re-deployable. Offline."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import nakon_ops
from constants import KNOWN_BROKEN_CONFIGS
from packet_ops import validate_profile

NINE = {
    "tftpd-hpa-anon-write",
    "postgresql-remote-access",
    "postgresql-no-auth",
    "sshd-force-sftp-broken-chroot",
    "local-user-win",
    "powershell-execution-unrestricted",
    "rpc-proxy-on-dc-web-win",
    "unauth-kiosk-app-startup-win",
    "mailenable-cleartext-mail-win",
}


class KnownBrokenList(unittest.TestCase):
    def test_exactly_the_nine_named_configs(self):
        self.assertEqual(set(KNOWN_BROKEN_CONFIGS), NINE)

    def test_every_entry_carries_a_reason(self):
        for name, reason in KNOWN_BROKEN_CONFIGS.items():
            self.assertTrue(reason.strip(), name)

    def test_unrealircd_is_conditional_and_not_banned(self):
        # rc=127 only on a box without docker (conditional on the lineup), and verify's
        # plant-coverage gate catches the failed step, so it stays warning-level.
        self.assertNotIn("unrealircd-backdoor-container", KNOWN_BROKEN_CONFIGS)


class ValidateKnownBrokenPins(unittest.TestCase):
    def test_bare_name_raises_naming_config_reason_and_where(self):
        with self.assertRaises(SystemExit) as cm:
            nakon_ops._validate_known_broken_pins(
                ["sshd-force-sftp-broken-chroot"], "pins for 'web01'")
        msg = str(cm.exception)
        self.assertIn("sshd-force-sftp-broken-chroot", msg)
        self.assertIn("sftpusers", msg)
        self.assertIn("pins for 'web01'", msg)

    def test_dict_pin_is_checked_too(self):
        with self.assertRaises(SystemExit):
            nakon_ops._validate_known_broken_pins(
                [{"name": "tftpd-hpa-anon-write", "vars": {}}], "pins for 'web01'")

    def test_exempt_pin_warns_instead_of_raising(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(nakon_ops._validate_known_broken_pins(
                ["local-user-win"], "pins for 'ad01'", exempt={"local-user-win"}))
        self.assertIn("WARNING", out.getvalue())

    def test_healthy_config_is_silent(self):
        self.assertIsNone(nakon_ops._validate_known_broken_pins(
            ["apache"], "pins for 'web01'"))


BOXES = [{"name": "web01", "template": "base-ubuntu24.04-fix", "last_octet": 10}]
TEAMS = {"team1": {"identifier": "101"}}


def _comp(tmp, vulns=None):
    comp = Path(tmp) / "comp"
    comp.mkdir()
    if vulns is not None:
        (comp / "box_vulns.json").write_text(json.dumps(vulns))
    return comp


class GenerateNakonConfigGate(unittest.TestCase):
    def test_recorded_pin_keeps_existing_competition_deployable(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp, {"web01": ["local-user-win"]})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")
            self.assertIn("WARNING", out.getvalue())
            machines = json.loads((comp / "nakon-config.json").read_text())["machines"]
            self.assertIn("local-user-win", machines[0]["configurations"])

    def test_fresh_randomized_selection_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            with patch.object(nakon_ops, "_nakon_randomize",
                              return_value=(["apache"], ["local-user-win"])):
                with self.assertRaises(SystemExit) as cm:
                    nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")
            self.assertIn("local-user-win", str(cm.exception))


class PacketCompilerGate(unittest.TestCase):
    def test_packet_pinning_a_broken_config_fails_validate(self):
        profile = {
            "event": {"comp_id": "pkt-broken", "name": "Broken Pin",
                      "scenario": "scenario line", "difficulty": 1},
            "boxes": [{"name": "web01", "template": "base-ubuntu24.04-fix",
                       "last_octet": 4, "fidelity": "exact"}],
            "services": [{"box": "web01", "name": "Web", "port": 80,
                          "pin": "tftpd-hpa-anon-write", "display": "http",
                          "fidelity": "exact"}],
        }
        errors = validate_profile(profile)
        self.assertTrue(any("known-broken" in e and "tftpd-hpa-anon-write" in e
                            for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
