"""verify's closed-inject warning (winad-scrim2: every inject read 'Inject is closed')."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import verifier.engine as v_engine  # noqa: E402

NOW = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)


class ClosedInjects(unittest.TestCase):
    def test_flags_only_past_close_times(self):
        injects = [
            {"Title": "old", "CloseTime": "2026-09-26T21:22:23Z"},
            {"Title": "future", "CloseTime": "2026-09-27T01:00:00Z"},
            {"Title": "nokey"},
            {"Title": "junk", "CloseTime": "not-a-date"},
        ]
        self.assertEqual(v_engine.closed_injects(injects, now=NOW),
                         [("old", "2026-09-26T21:22:23Z")])


if __name__ == "__main__":
    unittest.main()
