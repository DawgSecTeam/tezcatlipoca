"""verify's closed-inject warning (winad-scrim2: every inject read 'Inject is closed')."""

import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("verify_competition", _REPO / "verify-competition.py")
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)

NOW = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)


class ClosedInjects(unittest.TestCase):
    def test_flags_only_past_close_times(self):
        injects = [
            {"Title": "old", "CloseTime": "2026-09-26T21:22:23Z"},
            {"title": "future", "close_time": "2026-09-27T01:00:00Z"},
            {"Title": "nokey"},
            {"Title": "junk", "CloseTime": "not-a-date"},
        ]
        self.assertEqual(verify.closed_injects(injects, now=NOW),
                         [("old", "2026-09-26T21:22:23Z")])


if __name__ == "__main__":
    unittest.main()
