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


class HeldRangePreflight(unittest.TestCase):
    """A range whose clock has never run has no scoreboard rows: /api/services/<tid> answers
    literal null while the engine drives the same pins as preflight rounds. The pre-T0 fire
    test read the null and died — every fire test that ever passed had run on a range that
    had already been started at least once (cde-2026 run-84c0e45d, first fresh deploy)."""

    PF = {"round": 3, "checks": [
        {"team_id": 1, "team_name": "team1", "service_name": "web01-http", "result": False,
         "error": "connection refused"},
        {"team_id": 1, "team_name": "team1", "service_name": "ad01-dns", "result": True, "error": ""},
        {"team_id": 2, "team_name": "team2", "service_name": "ad01-dns", "result": True, "error": ""}]}

    def _creds_read(self, services_payload):
        import json

        def fake_qget(creds, user, jar, path):
            seen.append(path)
            body = json.dumps({"1": services_payload}.get(path.rsplit("/", 1)[-1],
                                                         self.PF if path == "/api/engine/preflight" else None))
            return type("R", (), {"stdout": body})()
        seen = []
        creds = {"RUN_DIR": "/tmp", "ADMIN_PW": "pw", "ENGINE_IP": "x"}
        with patch.object(quotient_api, "_team_tid", lambda *a: "1"), \
                patch.object(quotient_api, "qget", side_effect=fake_qget):
            return quotient_api.parsed_status(creds, "team1"), seen

    def test_null_scoreboard_rows_are_read_from_the_preflight_round(self):
        rows, seen = self._creds_read(None)
        self.assertIn("/api/engine/preflight", seen)
        self.assertEqual({r["service"]: r["up"] for r in rows},
                         {"web01-http": False, "ad01-dns": True})

    def test_a_started_range_never_consults_preflight(self):
        rows, seen = self._creds_read([{"ServiceName": "web01-http",
                                        "Last10Rounds": [{"Checks": [{"Result": True}]}]}])
        self.assertEqual([r["service"] for r in rows], ["web01-http"])
        self.assertNotIn("/api/engine/preflight", seen)

    def test_preflight_without_the_team_is_still_an_error(self):
        # the fallback must not turn a genuinely empty engine into a quiet pass
        def fake_qget(creds, user, jar, path):
            return type("R", (), {"stdout": "null"})()
        creds = {"RUN_DIR": "/tmp", "ADMIN_PW": "pw", "ENGINE_IP": "x"}
        with patch.object(quotient_api, "_team_tid", lambda *a: "1"), \
                patch.object(quotient_api, "qget", side_effect=fake_qget):
            with self.assertRaises(ValueError):
                quotient_api.parsed_status(creds, "team1")


if __name__ == "__main__":
    unittest.main()
