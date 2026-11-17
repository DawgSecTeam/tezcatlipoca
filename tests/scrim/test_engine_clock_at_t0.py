"""--hold-start defers the scoring engine's clock to `competition-ops start`; a --skip-deploy
scrim never calls it either, so the two correct halves combined into an event played entirely
against preflight rounds — checks ran, nothing recorded (cde-2026 run-7998e766: blue cycles rc=0
while /api/engine answered competition_started:false at T0+5)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from quotient import lifecycle_ops as lo


class _Session:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(url)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    def get(self, url, timeout=None):
        raise AssertionError("unused")


def _run(engine_status, session):
    doc = SimpleNamespace(absorb=lambda s: None, save=lambda: None)
    with patch.object(lo, "load_state", return_value={}), \
            patch.object(lo, "engine_base", return_value="http://e"), \
            patch.object(lo, "scoring_password", return_value="pw"), \
            patch.object(lo, "login", return_value=session), \
            patch.object(lo, "engine_status", return_value=engine_status), \
            patch.object(lo, "load_injects", return_value=[]), \
            patch.object(lo, "estate_state", SimpleNamespace(
                EstateState=SimpleNamespace(load=lambda cd: doc))):
        return lo.start_engine_clock("/tmp/comp", engine_ip="10.0.0.251")


class EngineClock(unittest.TestCase):
    def test_a_preflight_range_gets_its_clock_started(self):
        s = _Session()
        rc = _run({"competition_started": False, "running": True}, s)
        self.assertEqual(rc, 0)
        self.assertIn("http://e/api/competition/start", s.posts)

    def test_a_live_range_is_not_started_twice(self):
        s = _Session()
        rc = _run({"competition_started": True, "running": True}, s)
        self.assertEqual(rc, 0)
        self.assertEqual(s.posts, [])


if __name__ == "__main__":
    unittest.main()
