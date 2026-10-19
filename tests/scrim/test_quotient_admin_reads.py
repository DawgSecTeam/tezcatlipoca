"""The fire test's scoreboard read used the team1 account; Quotient answers
"unauthorized" to a team login on /api/teams, so the fire test could never see
the service come back (live: cde-2026 verify, 2026-10-19). The admin session can
read every team."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from scrim import quotient_api


class AdminRead(unittest.TestCase):
    def test_the_team_list_and_services_use_the_admin_account(self):
        seen = []

        def fake_qget(creds, user, jar, path):
            seen.append((user, path))
            import json
            if path == "/api/teams":
                return type("R", (), {"stdout": json.dumps([{"ID": 1, "Name": "team1"}])})()
            return type("R", (), {"stdout": json.dumps([])})()

        creds = {"RUN_DIR": "/tmp", "ADMIN_PW": "pw", "ENGINE_IP": "x"}
        with patch.object(quotient_api, "qget", side_effect=fake_qget):
            quotient_api.parsed_status(creds, "team1")
        self.assertEqual(seen[0][0], "admin")
        self.assertTrue(seen[0][1] == "/api/teams")


if __name__ == "__main__":
    unittest.main()
