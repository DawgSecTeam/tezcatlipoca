"""Team identifier vs engine-derived-slot vmid arithmetic (caught live:
engine 1080 + default identifiers 101/102 put team2's first box vmid exactly on the
engine-template slot (engine+140 = 1220) — undetectable at preflight time because the
template doesn't exist yet, surfacing hours later when the template is cloned). Offline."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from config.config_ops import collect_teams


class TeamVmidArithmetic(unittest.TestCase):
    def test_engine_slot_collision_refused(self):
        # identifier 102's block starts at 1220 = engine 1080's template slot
        import os
        old = os.environ.pop("TEZ_TEAM_IDENTIFIERS", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                collect_teams(2, engine_vmid=1080)  # defaults to identifiers 101,102
            self.assertIn("engine template", str(cm.exception))
        finally:
            if old is not None:
                os.environ["TEZ_TEAM_IDENTIFIERS"] = old


    def test_environment_override_takes_precedence_over_identifier_defaults(self):
        import os
        os.environ["TEZ_TEAM_IDENTIFIERS"] = "130,131"
        try:
            teams = collect_teams(2, engine_vmid=1080,
                                  identifier_defaults=[120, 121])
            self.assertEqual([t["identifier"] for t in teams.values()], ["130", "131"])
        finally:
            os.environ.pop("TEZ_TEAM_IDENTIFIERS", None)


    def test_golden_block_collision_refused(self):
        # identifier 103's block starts at 1230 = engine 1080's golden block
        import os
        os.environ["TEZ_TEAM_IDENTIFIERS"] = "103"
        try:
            with self.assertRaises(SystemExit):
                collect_teams(1, engine_vmid=1080)
        finally:
            os.environ.pop("TEZ_TEAM_IDENTIFIERS", None)

    def test_explicit_per_run_identifiers_beat_the_environment_override(self):
        # Two ranges of one competition on one node need different blocks, but the env is
        # one value for the whole run host (caught live: a run held 120 while
        # scrim-one-light had no way to ask for another).
        import os
        os.environ["TEZ_TEAM_IDENTIFIERS"] = "120,121"
        try:
            teams = collect_teams(1, engine_vmid=1300, team_identifiers="130")
            self.assertEqual([t["identifier"] for t in teams.values()], ["130"])
        finally:
            os.environ.pop("TEZ_TEAM_IDENTIFIERS", None)


if __name__ == "__main__":
    unittest.main()
