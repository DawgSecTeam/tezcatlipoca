"""Known-broken catalog configs are a machine-readable list
(constants.KNOWN_BROKEN_CONFIGS) enforced at generate/compile time, replacing the prose +
per-competition-JSON pruning that let a new comp silently re-pin one. The four Linux rows
(tftpd, postgresql pair, sshd-force-sftp) were fixed and verified live on 2026-10-02, and so
were four of the five "Windows user-policy" rows (local-user-win,
powershell-execution-unrestricted, rpc-proxy-on-dc-web-win, unauth-kiosk-app-startup-win):
the user-policy/account-ordering diagnosis was wrong and none of them is an account defect.
Only mailenable-cleartext-mail-win is still broken (its install is a wrong package id). A
competition that already records a pin of a still-listed config keeps deploying (warning, not
error) so historical comps stay re-deployable. Offline."""

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
import nakon_config_ops
import nakon_ops
from constants import KNOWN_BROKEN_CONFIGS
from packet_ops import validate_profile

REMAINING = {
    "mailenable-cleartext-mail-win",
}


class KnownBrokenList(unittest.TestCase):
    def test_exactly_the_remaining_configs(self):
        self.assertEqual(set(KNOWN_BROKEN_CONFIGS), REMAINING)

    def test_every_entry_carries_a_reason(self):
        for name, reason in KNOWN_BROKEN_CONFIGS.items():
            self.assertTrue(reason.strip(), name)

    def test_fixed_rows_are_no_longer_banned(self):
        # The four Linux rows were fixed and verified live 2026-10-02, as was unrealircd's
        # rc=127 (it now installs a runtime or exits rc=1 with a reason), as were four of the
        # five "Windows user-policy" rows (the whole account/policy diagnosis was wrong — see
        # docs/upstream-defects-handoff.md §5 and docs/vulndb-fixes/).
        for name in ("tftpd-hpa-anon-write", "postgresql-no-auth", "postgresql-remote-access",
                     "sshd-force-sftp-broken-chroot", "unrealircd-backdoor-container",
                     "local-user-win", "powershell-execution-unrestricted",
                     "rpc-proxy-on-dc-web-win", "unauth-kiosk-app-startup-win"):
            self.assertNotIn(name, KNOWN_BROKEN_CONFIGS)


class ValidateKnownBrokenPins(unittest.TestCase):
    def test_bare_name_raises_naming_config_reason_and_where(self):
        name = "mailenable-cleartext-mail-win"
        with self.assertRaises(SystemExit) as cm:
            nakon_ops._validate_known_broken_pins([name], "pins for 'web01'")
        msg = str(cm.exception)
        self.assertIn(name, msg)
        self.assertIn(KNOWN_BROKEN_CONFIGS[name][:30], msg)  # reason carried verbatim
        self.assertIn("pins for 'web01'", msg)

    def test_dict_pin_is_checked_too(self):
        with self.assertRaises(SystemExit):
            nakon_ops._validate_known_broken_pins(
                [{"name": "mailenable-cleartext-mail-win", "vars": {}}], "pins for 'web01'")

    def test_exempt_pin_warns_instead_of_raising(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(nakon_ops._validate_known_broken_pins(
                ["mailenable-cleartext-mail-win"], "pins for 'ad01'", exempt={"mailenable-cleartext-mail-win"}))
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
            comp = _comp(tmp, {"web01": ["mailenable-cleartext-mail-win"]})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")
            self.assertIn("WARNING", out.getvalue())
            machines = json.loads((comp / "nakon-config.json").read_text())["machines"]
            self.assertIn("mailenable-cleartext-mail-win", machines[0]["configurations"])

    def test_fresh_randomized_selection_is_filtered_not_fatal(self):
        # randomize cannot know this driver-side table, so a fresh pick of a broken config is
        # dropped with a notice — aborting create-competition for a pin nobody chose was the
        # worse behaviour (found in integration, 2026-10-02).
        with tempfile.TemporaryDirectory() as tmp:
            comp = _comp(tmp)
            out = io.StringIO()
            with patch.object(nakon_config_ops, "_nakon_randomize",
                              return_value=(["apache", "airship-webapp"],
                                             ["mailenable-cleartext-mail-win", "suid-find"])):
                with contextlib.redirect_stdout(out):
                    nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, comp, "pw")
            self.assertIn("known-broken", out.getvalue())
            self.assertIn("mailenable-cleartext-mail-win", out.getvalue())
            machines = json.loads((comp / "nakon-config.json").read_text())["machines"]
            self.assertIn("suid-find", machines[0]["configurations"])
            self.assertNotIn("mailenable-cleartext-mail-win", machines[0]["configurations"])
            self.assertIn("apache", machines[0]["configurations"])
            self.assertNotIn("airship-webapp", machines[0]["configurations"])


class DropUnplantableBare(unittest.TestCase):
    def test_keeps_healthy_and_drops_string_and_dict_pins(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = nakon_ops._drop_unplantable_bare(
                ["suid-find", "mailenable-cleartext-mail-win",
                 {"name": "mailenable-cleartext-mail-win", "vars": {}}],
                "fresh selection for 'web01'")
        self.assertEqual(kept, ["suid-find"])
        msg = out.getvalue()
        for name in ("mailenable-cleartext-mail-win", "mailenable-cleartext-mail-win"):
            self.assertIn(name, msg)
        self.assertIn("fresh selection for 'web01'", msg)

    def test_all_healthy_is_silent(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = nakon_ops._drop_unplantable_bare(["apache", "suid-find"], "x")
        self.assertEqual(kept, ["apache", "suid-find"])
        self.assertEqual(out.getvalue(), "")

    def test_literal_var_config_is_dropped_but_identity_only_survives(self):
        # hosts-redirect-linux has a literal (HOSTS) -> unplantable bare; a config with only
        # identity vars would be plantable, so the predicate is literal-scoped, not name-based.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = nakon_ops._drop_unplantable_bare(
                ["hosts-redirect-linux", "apache"], "fresh selection for 'web01'")
        self.assertEqual(kept, ["apache"])
        self.assertIn("requires operator vars", out.getvalue())
        self.assertIn("hosts-redirect-linux", out.getvalue())


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
