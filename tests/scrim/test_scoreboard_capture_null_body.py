"""Final-scoreboard capture must survive a null body and must never raise IndexError.

caught live by the campaign's two teardown subagents: every dashboard teardown
reported `[engine_capture] warned — engine capture failed (IndexError: list index out of
range)` and lost the engine's final scoreboard before the engine VM was destroyed.
`dump_scoreboard`'s `_get` returned `r.json()` — a 200 body that is literally `null` gives
`None` — WITHOUT appending to `errors`, and the next line read `errors[-1]` on an empty list.
The teardown's step wrapper catches Exception and downgrades it to a warning, so the exit
code stayed 0 while the only copy of the final scores was never written.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from quotient import event_capture  # noqa: E402


class _Response:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, ""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        # A body of literal `null` parses to None — the shape that broke the caller.
        return self._payload


class _Session:
    def __init__(self, payloads):
        self.payloads, self.paths = payloads, []

    def get(self, url, timeout=None):
        path = "/" + url.split("/", 3)[3]
        self.paths.append(path)
        return _Response(self.payloads.get(path))


class DumpScoreboardNullBody(unittest.TestCase):
    def _dump(self, payloads):
        with tempfile.TemporaryDirectory() as tmp:
            session = _Session(payloads)
            errors = event_capture.dump_scoreboard("http://engine", session, tmp)
            files = sorted((Path(tmp) / "evidence").glob("event-final-*.json"))
            self.assertEqual(len(files), 1, "the dump is written even when reads fail")
            return errors, json.loads(files[0].read_text())

    def test_null_teams_body_reports_an_error_instead_of_raising(self):
        errors, dump = self._dump({"/api/teams": None, "/api/injects": None})
        self.assertEqual(dump["teams"], [])
        self.assertEqual(dump["teams_error"], "teams: empty response body (null)")
        self.assertEqual(dump["injects_error"], "injects: empty response body (null)")
        self.assertEqual(len(errors), 2, errors)

    def test_null_services_body_records_that_team_and_keeps_going(self):
        errors, dump = self._dump({
            "/api/teams": [{"ID": 1}, {"ID": 2}],
            "/api/services/1": None,
            "/api/services/2": [{"service": "web01-http", "up": True}],
            "/api/injects": [{"ID": 1}],
        })
        self.assertEqual(dump["teams"][0]["services_error"], "services/1: empty response body (null)")
        self.assertEqual(dump["teams"][1]["services"], [{"service": "web01-http", "up": True}])
        self.assertEqual(dump["injects"], [{"ID": 1}])
        self.assertEqual(len(errors), 1, errors)



if __name__ == "__main__":
    unittest.main()
