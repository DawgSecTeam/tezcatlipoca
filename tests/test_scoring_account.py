"""The dedicated `scoring` automation account.

Why a second admin account exists (2026-10-02): Quotient allows **ONE session per
account** — every login kills that account's previous cookie. Automation that has to
authenticate on its own schedule (the round-loop watchdog, an unattended verify) would
therefore evict the operator's or the harness's `admin` session every time it ran. A
separate account makes the two independent, because the rule is per account.

`event.conf` seeds accounts as explicit per-role lists, and `inject` was already a
second account of a different role — the model supports this. The password is minted
per competition like every other secret (never a fixed literal) and lands in
credentials.txt.
"""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
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
        """Older callers and the pin/compile tests build a dict ctx without it."""
        accounts = _admin_accounts(_ctx())
        self.assertEqual([a["name"] for a in accounts], ["admin"])

    def test_the_scoring_password_is_not_the_admin_password(self):
        """If they were ever the same, the separation would be a no-op — and the whole
        point is that these are two independent accounts."""
        accounts = _admin_accounts(_ctx(quotient_scoring_password="scoringpw"))
        by_name = {a["name"]: a["pw"] for a in accounts}
        self.assertNotEqual(by_name["admin"], by_name["scoring"])

    def test_event_conf_carries_both_admins(self):
        conf = build_event_conf(_ctx(quotient_scoring_password="scoringpw"), {"web01": []})
        names = [a["name"] for a in conf["admin"]]
        self.assertEqual(names, ["admin", "scoring"])

    def test_event_conf_without_the_key_is_unchanged(self):
        conf = build_event_conf(_ctx(), {"web01": []})
        self.assertEqual([a["name"] for a in conf["admin"]], ["admin"])

    def test_no_fixed_literal_leaks_in(self):
        """The repo removed fixed literals on purpose (`ubuntu/ubuntu`,
        `admin/changeme123`) — the automation account must follow the same rule."""
        conf = build_event_conf(_ctx(quotient_scoring_password="generated-if-you-see-this"),
                                {"web01": []})
        for account in conf["admin"]:
            self.assertNotIn(account["pw"], ("ubuntu", "changeme123", "admin", "password"))


if __name__ == "__main__":
    unittest.main()
