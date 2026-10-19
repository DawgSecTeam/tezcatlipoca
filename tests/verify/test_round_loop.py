"""One definition of "the scoring loop is stopped".

This decision used to live only inside `verify-competition.py`'s gate. An engine-side
watchdog would have had to re-derive it, and two implementations of the same judgement
drift — the failure mode this repo keeps paying for. `round_loop.py` is now canonical
and verify's gate uses it.

The live signature (caught live, and the guaranteed aftermath of the
owner-confirmed .193 hard-downs): after an engine VM reboots, Docker brings the
containers back but the round loop stays stopped. `/api/engine` then reports the engine
as NOT paused, the Go zero time in `current_round_time`, and a stale
`last_round.StartTime`. The scoreboard freezes while everything that reads it keeps
working — so an old UP is reported as a live UP.
"""

import base64
import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from engine import round_loop  # noqa: E402

ZERO = "0001-01-01T00:00:00Z"
NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def _engine(running=True, current=ZERO, started_min_ago=0, competition_started=True):
    payload = {"running": running, "current_round_time": current,
               "competition_started": competition_started}
    if started_min_ago is not None:
        started = NOW - timedelta(minutes=started_min_ago)
        payload["last_round"] = {"StartTime": started.strftime("%Y-%m-%dT%H:%M:%SZ")}
    return payload


class RoundLoopState(unittest.TestCase):
    def state(self, payload, **kw):
        return round_loop.round_loop_state(payload, now=NOW, **kw)["state"]

    def test_a_paused_engine_with_a_real_round_is_not_stale(self):
    # An intentional pause has a real current round time; never restart it.
        current = (NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.state(_engine(running=False, current=current,
                                            started_min_ago=600)), round_loop.PAUSED)

    def test_paused_engine_with_zero_time_and_stale_round_is_stale(self):
    # The reboot signature remains healable when the engine reports paused.
        self.assertEqual(self.state(_engine(running=False, started_min_ago=30)),
                         round_loop.STALE)

    def test_paused_engine_with_zero_time_and_fresh_round_is_paused(self):
        self.assertEqual(self.state(_engine(running=False, started_min_ago=1)),
                         round_loop.PAUSED)

    def test_a_real_current_round_time_means_advancing(self):
        current = (NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.state(_engine(current=current, started_min_ago=90)),
                         round_loop.ADVANCING)

    def test_zero_time_with_a_fresh_start_is_only_pending(self):
    # A fresh deploy has not scored its first round yet; Delay+Jitter has to elapse
    #         before that means anything.
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
    # No last round at all is not evidence of a stopped loop.
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
        self.assertTrue(round_loop.is_stale(_engine(running=False, started_min_ago=30),
                                           now=NOW))
        current = (NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertFalse(round_loop.is_stale(_engine(running=False, current=current,
                                                    started_min_ago=30), now=NOW))

    def test_a_naive_now_is_accepted(self):
    # The engine-side actor has no reason to build a tz-aware clock.
        result = round_loop.round_loop_state(_engine(started_min_ago=30),
                                             now=datetime(2026, 10, 2, 12, 0, 0))
        self.assertEqual(result["state"], round_loop.STALE)


class HealActions(unittest.TestCase):
# The unattended repair: only a STALE loop whose competition has already started.

    def actions(self, payload):
        return round_loop.heal_actions(payload, now=NOW)

    def test_a_stale_started_loop_gets_the_two_posts_in_order(self):
        self.assertEqual(
            self.actions(_engine(started_min_ago=30)),
            [("/api/competition/start", {"started": True}),
             ("/api/engine/pause", {"pause": False})])

    def test_a_stale_paused_engine_is_healed(self):
    # The reboot signature: running=False, zero current time, stale last round.
        self.assertEqual(len(self.actions(_engine(running=False, started_min_ago=30))), 2)

    def test_an_unstarted_competition_is_never_started(self):
    # A held pre-T0 range stays held, whatever the loop looks like.
        for payload in (_engine(started_min_ago=30, competition_started=False),
                        _engine(running=False, started_min_ago=30,
                                competition_started=False),
                        {"running": False, "current_round_time": ZERO}):
            self.assertEqual(self.actions(payload), [])

    def test_every_non_stale_state_is_left_alone(self):
        current = (NOW - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payloads = [
            _engine(running=False, current=current, started_min_ago=600),   # PAUSED
            _engine(started_min_ago=1),                                    # PENDING
            _engine(current=current, started_min_ago=90),                  # ADVANCING
            None, "nope", {},                                              # UNKNOWN
        ]
        for payload in payloads:
            self.assertEqual(self.actions(payload), [])


class HealthcheckRoundLoopHeal(unittest.TestCase):
# The install renders both the watchdog script and the canonical classifier it imports.

    def test_healthcheck_script_calls_the_helper_and_keeps_the_existing_checks(self):
        from engine.engine_health_ops import healthcheck_script
        script = healthcheck_script()
        self.assertIn("/usr/local/sbin/range-round-loop-heal.py", script)
        for check in ("quotient-containers", "quotient-api", "isolation-rule", "nat-rule"):
            self.assertIn(check, script)

    def test_helper_reads_the_engine_config_and_applies_heal_actions(self):
        from engine.engine_health_ops import round_loop_heal_script
        script = round_loop_heal_script()
        self.assertIn("/opt/quotient/config/event.conf", script)
        self.assertIn("heal_actions", script)
        self.assertIn("/api/login", script)
        self.assertIn("/api/engine", script)
        for line in script.splitlines():
            if "log(" in line:
                for secret in ("password", "username", "config"):
                    self.assertNotIn(secret, line)

    def test_install_writes_the_helper_and_the_canonical_classifier(self):
        from engine import engine_health_ops
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(engine_health_ops.subprocess, "run", side_effect=fake_run):
            engine_health_ops.install_range_healthcheck(
                {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.10",
                 "vm_username": "ubuntu"})
        remote = calls[0][-1]
        for path in ("/usr/local/sbin/range-healthcheck.sh",
                     "/usr/local/sbin/range-round-loop-heal.py",
                     "/usr/local/lib/tezcatlipoca/round_loop.py",
                     "/etc/systemd/system/range-healthcheck.service",
                     "/etc/systemd/system/range-healthcheck.timer"):
            self.assertIn(path, remote)
        blobs = re.findall(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d", remote)
        decoded = [base64.b64decode(blob).decode() for blob in blobs]
        self.assertIn(engine_health_ops.round_loop_heal_script(), decoded)
        self.assertIn(engine_health_ops.healthcheck_script(), decoded)
        self.assertIn((_REPO / "engine" / "round_loop.py").read_text(encoding="utf-8"),
                      decoded)


class ParseRfc3339(unittest.TestCase):
    def test_parses_the_z_suffix(self):
        self.assertEqual(round_loop.parse_rfc3339("2026-10-02T12:00:00Z").year, 2026)

    def test_absent_or_junk_is_none(self):
        for value in (None, "", "not-a-date", 17):
            self.assertIsNone(round_loop.parse_rfc3339(value))


if __name__ == "__main__":
    unittest.main()
