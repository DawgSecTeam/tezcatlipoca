"""Portal logins come from Quotient's own event.conf (portal/auth.py) — never a Quotient login."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import toml  # noqa: E402

from portal.auth import EventConfAuth  # noqa: E402
from quotient.setup import build_event_conf  # noqa: E402


def _conf(team_pw="t1pw", admin_pw="adminpw"):
    """A real build_event_conf output — the portal must parse what the pipeline writes."""
    ctx = {"teams": {"team1": "101", "team2": "102"},
           "boxes_per_team": [{"name": "web01", "last_octet": 10}],
           "team_passwords": {"team1": team_pw, "team2": "t2pw"},
           "event_name": "probe", "quotient_admin_password": admin_pw,
           "quotient_scoring_password": "scoringpw", "inject_password": "injectpw"}
    return toml.dumps(build_event_conf(ctx, {}))


class EventConfAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "event.conf"
        self.path.write_text(_conf())
        self.auth = EventConfAuth(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_team_and_admin_roles(self):
        self.assertEqual(self.auth.check("team1", "t1pw"), ("team", "team1"))
        self.assertEqual(self.auth.check("admin", "adminpw"), ("admin", "admin"))
        self.assertEqual(self.auth.teams(), ["team1", "team2"])

    def test_wrong_password_and_unknown_user(self):
        self.assertIsNone(self.auth.check("team1", "t2pw"))
        self.assertIsNone(self.auth.check("team9", "t1pw"))
        self.assertIsNone(self.auth.check("", ""))

    def test_automation_and_inject_accounts_are_refused(self):
        self.assertIsNone(self.auth.check("scoring", "scoringpw"))
        self.assertIsNone(self.auth.check("inject", "injectpw"))

    def test_password_rotation_is_picked_up_on_mtime_change(self):
        self.assertIsNotNone(self.auth.check("team1", "t1pw"))
        self.path.write_text(_conf(team_pw="rotated"))
        future = time.time() + 5
        os.utime(self.path, (future, future))
        self.assertIsNone(self.auth.check("team1", "t1pw"))
        self.assertEqual(self.auth.check("team1", "rotated"), ("team", "team1"))

    def test_missing_file_means_nobody_logs_in(self):
        self.assertIsNotNone(self.auth.check("team1", "t1pw"))
        self.path.unlink()
        self.assertIsNone(self.auth.check("team1", "t1pw"))


if __name__ == "__main__":
    unittest.main()
