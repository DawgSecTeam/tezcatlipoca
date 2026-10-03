"""One definition of "the scoring loop is stopped".

This decision used to live only inside `verify-competition.py`'s gate. An engine-side
watchdog would have had to re-derive it, and two implementations of the same judgement
drift — the failure mode this repo keeps paying for. `round_loop.py` is now canonical
and both callers use it: verify's gate and the watchdog
(`tools/round_loop_guard.py`).

The live signature (pfsense-ad 2026-09-28, and the guaranteed aftermath of the
owner-confirmed .193 hard-downs): after an engine VM reboots, Docker brings the
containers back but the round loop stays stopped. `/api/engine` then reports the engine
as NOT paused, the Go zero time in `current_round_time`, and a stale
`last_round.StartTime`. The scoreboard freezes while everything that reads it keeps
working — so an old UP is reported as a live UP.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import round_loop  # noqa: E402

ZERO = "0001-01-01T00:00:00Z"
NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def _engine(running=True, current=ZERO, started_min_ago=0):
    payload = {"running": running, "current_round_time": current}
    if started_min_ago is not None:
        started = NOW - timedelta(minutes=started_min_ago)
        payload["last_round"] = {"StartTime": started.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return payload


class RoundLoopState(unittest.TestCase):
    def state(self, payload, **kw):
        return round_loop.round_loop_state(payload, now=NOW, **kw)["state"]

    def test_a_paused_engine_is_not_stale(self):
        """Paused is a deliberate operator state — never restart the loop under it."""
        self.assertEqual(self.state(_engine(running=False, started_min_ago=600)),
                         round_loop.PAUSED)

    def test_a_real_current_round_time_means_advancing(self):
        current = (NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.state(_engine(current=current, started_min_ago=90)),
                         round_loop.ADVANCING)

    def test_zero_time_with_a_fresh_start_is_only_pending(self):
        """A fresh deploy has not scored its first round yet; Delay+Jitter has to elapse
        before that means anything."""
        self.assertEqual(self.state(_engine(started_min_ago=1)), round_loop.PENDING)

    def test_zero_time_with_an_old_start_is_stale(self):
        self.assertEqual(self.state(_engine(started_min_ago=30)), round_loop.STALE)

    def test_the_boundary_is_the_freshness_window_not_a_guess(self):
        window_min = (round_loop.FRESHNESS_ROUNDS * round_loop.ROUND_DELAY_SECONDS) / 60
        just_inside = _engine(started_min_ago=0)
        just_inside["last_round"]["StartTime"] = (
            NOW - timedelta(minutes=window_min) + timedelta(seconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        just_outside = _engine(started_min_ago=0)
        just_outside["last_round"]["StartTime"] = (
            NOW - timedelta(minutes=window_min) + timedelta(seconds=-1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.state(just_inside), round_loop.PENDING)
        self.assertEqual(self.state(just_outside), round_loop.STALE)

    def test_a_missing_start_time_cannot_be_judged(self):
        """No last round at all is not evidence of a stopped loop."""
        payload = {"running": True, "current_round_time": ZERO}
        self.assertEqual(self.state(payload), round_loop.ADVANCING)

    def test_a_junk_payload_is_unknown_not_stale(self):
        for payload in (None, "nope", [], {}):
            self.assertEqual(self.state(payload), round_loop.UNKNOWN)

    def test_age_is_reported_so_callers_do_not_re_derive_it(self):
        result = round_loop.round_loop_state(_engine(started_min_ago=30), now=NOW)
        self.assertAlmostEqual(result["age_seconds"], 1800, delta=2)

    def test_is_stale_is_the_single_question(self):
        self.assertTrue(round_loop.is_stale(_engine(started_min_ago=30), now=NOW))
        self.assertFalse(round_loop.is_stale(_engine(started_min_ago=1), now=NOW))
        self.assertFalse(round_loop.is_stale(_engine(running=False, started_min_ago=30),
                                            now=NOW))

    def test_a_naive_now_is_accepted(self):
        """The engine-side actor has no reason to build a tz-aware clock."""
        result = round_loop.round_loop_state(_engine(started_min_ago=30),
                                             now=datetime(2026, 10, 2, 12, 0, 0))
        self.assertEqual(result["state"], round_loop.STALE)


class ParseRfc3339(unittest.TestCase):
    def test_parses_the_z_suffix(self):
        self.assertEqual(round_loop.parse_rfc3339("2026-10-02T12:00:00Z").year, 2026)

    def test_absent_or_junk_is_none(self):
        for value in (None, "", "not-a-date", 17):
            self.assertIsNone(round_loop.parse_rfc3339(value))


if __name__ == "__main__":
    unittest.main()
