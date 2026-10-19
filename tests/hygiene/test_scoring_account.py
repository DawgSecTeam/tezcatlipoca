"""The `scoring` automation account is seeded alongside admin — two independent identities, each with its own minted password."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from quotient.setup import _admin_accounts, build_event_conf  # noqa: E402

BOXES = [{"name": "web01", "last_octet": 2}]


def _ctx(**overrides):
    ctx = {
        "teams": {"team1": "130"},
        "boxes_per_team": BOXES,
        "team_passwords": {"team1": "teampw"},
        "event_name": "probe",
        "quotient_admin_password": "adminpw",
    }
    ctx.update(overrides)
    return ctx


class AdminAccounts(unittest.TestCase):
    def test_both_accounts_are_seeded_when_a_scoring_password_is_supplied(self):
        accounts = _admin_accounts(_ctx(quotient_scoring_password="scoringpw"))
        self.assertEqual([a["name"] for a in accounts], ["admin", "scoring"])
        self.assertEqual(accounts[1]["pw"], "scoringpw")

    def test_a_caller_without_the_key_still_gets_the_single_admin(self):
        accounts = _admin_accounts(_ctx())
        self.assertEqual([a["name"] for a in accounts], ["admin"])

    def test_the_scoring_password_is_not_the_admin_password(self):
        accounts = _admin_accounts(_ctx(quotient_scoring_password="scoringpw"))
        by_name = {a["name"]: a["pw"] for a in accounts}
        self.assertNotEqual(by_name["admin"], by_name["scoring"])





if __name__ == "__main__":
    unittest.main()
