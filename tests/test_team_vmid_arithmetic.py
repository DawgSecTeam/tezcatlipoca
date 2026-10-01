"""Team identifier vs engine-derived-slot vmid arithmetic (live-found 2026-09-29:
engine 1080 + default identifiers 101/102 put team2's first box vmid exactly on the
engine-template slot (engine+140 = 1220) — undetectable at preflight time because the
template doesn't exist yet, surfacing hours later at terraform apply #2). Offline."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from config_ops import collect_teams


class TeamVmidArithmetic(unittest.TestCase):
    def test_engine_slot_collision_refused(self):
        # identifier 102's block starts at 1220 = engine 1080's template slot
        import os
        old = os.environ.pop("TF_VAR_team_identifiers", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                collect_teams(2, engine_vmid=1080)  # defaults to identifiers 101,102
            self.assertIn("engine template", str(cm.exception))
        finally:
            if old is not None:
                os.environ["TF_VAR_team_identifiers"] = old

    def test_safe_identifiers_accepted(self):
        import os
        os.environ["TF_VAR_team_identifiers"] = "120,121"
        try:
            teams = collect_teams(2, engine_vmid=1080)
            self.assertEqual([t["identifier"] for t in teams.values()], ["120", "121"])
        finally:
            os.environ.pop("TF_VAR_team_identifiers", None)

    def test_golden_block_collision_refused(self):
        # identifier 103's block starts at 1230 = engine 1080's golden block
        import os
        os.environ["TF_VAR_team_identifiers"] = "103"
        try:
            with self.assertRaises(SystemExit):
                collect_teams(1, engine_vmid=1080)
        finally:
            os.environ.pop("TF_VAR_team_identifiers", None)


if __name__ == "__main__":
    unittest.main()
