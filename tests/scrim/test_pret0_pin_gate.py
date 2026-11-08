"""The pre-T0 gate must refuse an estate whose scored pins are already down.

An availability competition that starts with pins DOWN scores nothing: red cannot take
down what is down, blue restores nothing it did not break, and the scoreboard runs flat
(live: scrim-one, 6/12 and 5/12 at T0 and again at the bell, run-1b74a06f). The
authoritative verify judged only red's summary lines and rode the services gate on the
same rc it deliberately ignores. Offline: a fake verify result, no network."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from scrim import compworld, red_stage  # noqa: E402

GOOD = """  team1: 12/12 services UP
  EXPECTED-PINS  all 12 pinned checks registered
  red_identity      : PASS  red01 is 10.0.0.246
  red_teams         : PASS  red01 reached all 2 teams
  services          : PASS
"""

DOWN = """  team1: 6/12 services UP
      DOWN: win02-ftp
      DOWN: dc01-ldap
  team2: 5/12 services UP
      DOWN: splunk01-splunk
  EXPECTED-PINS  all 12 pinned checks registered
  red_identity      : PASS  red01 is 10.0.0.246
  red_teams         : PASS  red01 reached all 2 teams
  services          : FAIL  some DOWN/unscored (query ok)
"""

UNSCORED = """  (no services reported yet — engine may not have scored a round)
  red_identity      : PASS  red01 is 10.0.0.246
  red_teams         : PASS  red01 reached all 2 teams
  services          : FAIL  no service has been scored yet
"""


def _verify(stdout):
    return subprocess.CompletedProcess(["verify"], 1, stdout, "")


class PreT0PinGate(unittest.TestCase):

    def _run(self, stdout, seen=None):
        args = type("A", (), {"red_ip": "10.0.0.246", "test_dir": None})()
        creds = {"ENGINE_IP": "10.0.0.243"}

        def cmd(comp, creds_, *extra):
            if seen is not None:
                seen["extra"] = list(extra)
            return ["verify"]

        with patch.object(compworld, "verify_cmd", side_effect=cmd), \
             patch.object(red_stage.procs, "run", return_value=_verify(stdout)), \
             patch.object(red_stage.fire_test, "write_verify_log", lambda *a, **k: None), \
             patch.object(red_stage, "log", lambda *a, **k: None):
            red_stage.verify_red_reaches_teams(args, "/nonexistent", creds)

    def test_a_clean_estate_passes(self):
        self._run(GOOD)

    def test_a_degraded_estate_is_refused_and_names_the_pins(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(DOWN)
        msg = str(ctx.exception)
        self.assertIn("not green", msg)
        self.assertIn("team1:win02-ftp", msg)
        self.assertIn("team2:splunk01-splunk", msg)

    def test_an_unscored_scoreboard_is_refused_too(self):
        # a scoreboard with no scored round has no DOWN lines, so the DOWN scan alone
        # would pass it — the services row is what makes that a refusal
        with self.assertRaises(RuntimeError) as ctx:
            self._run(UNSCORED)
        self.assertIn("not green", str(ctx.exception))

    def test_the_verify_runs_strict_on_services(self):
        seen = {}
        self._run(GOOD, seen)
        self.assertIn("--strict-services", seen["extra"])
