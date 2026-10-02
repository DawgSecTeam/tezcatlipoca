"""The engine-side round-loop actor.

`round_loop.py` owns the judgement; `tools/round_loop_guard.py` owns the acting. Tested
here with a fake engine so the two dangerous properties are pinned: it must NOT touch a
loop that is merely pending or paused, and it must issue exactly the two documented
POSTs (in that order) when the loop really is stopped.
"""

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import round_loop  # noqa: E402

# tools/round_loop_guard.py is pushed to the engine by path, so load it by path here too.
_SPEC = importlib.util.spec_from_file_location(
    "round_loop_guard", _REPO / "tools" / "round_loop_guard.py")
guard = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(guard)

NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
ZERO = "0001-01-01T00:00:00Z"


class FakeEngine:
    """Records calls; answers login, /api/engine, and the two fix POSTs."""

    def __init__(self, engine_payload=None, fail_login=False, fail_fix=False):
        self.engine_payload = engine_payload if engine_payload is not None else {}
        self.fail_login = fail_login
        self.fail_fix = fail_fix
        self.calls = []

    def login(self, username, password):
        self.calls.append(("login", username))
        if self.fail_login:
            raise OSError("connection refused")
        return {"ok": True}

    def get_json(self, path):
        self.calls.append(("get", path))
        return self.engine_payload

    def post_json(self, path, payload):
        self.calls.append(("post", path, payload))
        if self.fail_fix:
            raise OSError("engine went away")
        return 200


def _stale(minutes=30):
    return {"running": True, "current_round_time": ZERO,
            "last_round": {"StartTime": (NOW - timedelta(minutes=minutes))
                           .strftime("%Y-%m-%dT%H:%M:%SZ")}}


class GuardActsOnlyOnAStaleLoop(unittest.TestCase):
    def test_a_stopped_loop_gets_exactly_the_two_documented_posts(self):
        engine = FakeEngine(_stale())
        outcome = guard.guard_once(engine, "scoring", "pw", now=NOW)
        self.assertEqual(outcome["state"], round_loop.STALE)
        self.assertEqual(outcome["action"], "fixed")
        posts = [c for c in engine.calls if c[0] == "post"]
        self.assertEqual([p[1] for p in posts],
                         ["/api/competition/start", "/api/engine/pause"])
        self.assertEqual(posts[0][2], {"started": True})
        self.assertEqual(posts[1][2], {"pause": False})

    def test_a_paused_engine_is_never_touched(self):
        """Paused is a deliberate operator state, not a fault."""
        engine = FakeEngine({"running": False, "current_round_time": ZERO})
        outcome = guard.guard_once(engine, "scoring", "pw", now=NOW)
        self.assertEqual(outcome["action"], "none")
        self.assertEqual([c for c in engine.calls if c[0] == "post"], [])

    def test_a_pending_first_round_is_never_touched(self):
        engine = FakeEngine(_stale(minutes=1))
        outcome = guard.guard_once(engine, "scoring", "pw", now=NOW)
        self.assertEqual(outcome["state"], round_loop.PENDING)
        self.assertEqual([c for c in engine.calls if c[0] == "post"], [])

    def test_an_advancing_loop_is_never_touched(self):
        current = (NOW - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        engine = FakeEngine({"running": True, "current_round_time": current,
                             "last_round": {"StartTime": current}})
        outcome = guard.guard_once(engine, "scoring", "pw", now=NOW)
        self.assertEqual(outcome["action"], "none")
        self.assertEqual([c for c in engine.calls if c[0] == "post"], [])

    def test_it_authenticates_as_the_dedicated_account(self):
        """The whole point: automation must not log in as `admin`, or it evicts the
        operator's session (one session per account)."""
        engine = FakeEngine(_stale())
        guard.guard_once(engine, "scoring", "pw", now=NOW)
        self.assertEqual(engine.calls[0], ("login", "scoring"))


class GuardFailsSafe(unittest.TestCase):
    def test_an_unreachable_engine_is_reported_not_raised(self):
        outcome = guard.guard_once(FakeEngine(fail_login=True), "scoring", "pw", now=NOW)
        self.assertEqual(outcome["state"], "unknown")
        self.assertEqual(outcome["action"], "none")
        self.assertIn("unreachable", outcome["detail"])

    def test_a_failed_fix_is_reported_not_raised(self):
        outcome = guard.guard_once(FakeEngine(_stale(), fail_fix=True), "scoring", "pw",
                                   now=NOW)
        self.assertEqual(outcome["action"], "failed")
        self.assertIn("fix POST failed", outcome["detail"])

    def test_dry_run_decides_without_posting(self):
        engine = FakeEngine(_stale())
        outcome = guard.guard_once(engine, "scoring", "pw", now=NOW, dry_run=True)
        self.assertEqual(outcome["action"], "would-fix")
        self.assertEqual([c for c in engine.calls if c[0] == "post"], [])

    def _run_main(self, engine, *extra):
        """Drive the real CLI: a temp config, the fake client injected."""
        import json
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "guard.json"
            config.write_text(json.dumps({"base_url": "http://engine",
                                          "username": "scoring", "password": "pw"}))
            with patch.object(guard, "EngineClient", return_value=engine), \
                    patch.object(guard, "log"):
                return guard.main(["--config", str(config), "--quiet", *extra])

    def test_a_healthy_loop_exits_zero(self):
        """A timer that exits nonzero on every healthy tick would drown the journal."""
        engine = FakeEngine(_stale(minutes=1))
        self.assertEqual(self._run_main(engine), 0)

    def test_a_successful_fix_exits_zero(self):
        self.assertEqual(self._run_main(FakeEngine(_stale())), 0)

    def test_a_failed_fix_exits_nonzero(self):
        """The one case an operator must see: the loop was stopped and we could not
        restart it."""
        self.assertEqual(self._run_main(FakeEngine(_stale(), fail_fix=True)), 1)

    def test_dry_run_exits_zero(self):
        self.assertEqual(self._run_main(FakeEngine(_stale()), "--dry-run"), 0)


if __name__ == "__main__":
    unittest.main()
