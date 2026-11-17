"""Blue's LLM lock is keyed by endpoint MODEL, not just URL.

caught live (run-84c0e45d): both blues rode one gateway URL, so one lock serialised
them — and when that model's stream kept dropping mid-cycle, the event got three cut
cycles total instead of six short parallel ones. A gateway multiplexes its models;
teams on different models of one gateway are not competing for a llama.cpp slot, so
they must not share a lock. Teams on the same model still do.
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from scrim import blue_agent, endpoints, event_run, scoreboard_monitor, supervisor


def _locks_for(eps, teams=2):
    seen = {}

    def feed(n, a, creds, t0, stop, lock, first_delay=0.0):
        seen[n] = lock

    args = SimpleNamespace(duration_min=0.01, teams=teams, blue_watchdog=False)
    with mock.patch.object(blue_agent, "blue_feed_loop", feed), \
            mock.patch.object(scoreboard_monitor, "monitor_loop", lambda *a: None), \
            mock.patch.object(endpoints, "blue_ep", side_effect=lambda a, n: eps[n - 1]), \
            mock.patch.object(supervisor, "EVENT_END_GRACE", 0.2):
        try:
            event_run.stage_run(args, {}, time.time())
        except supervisor.WorkerDiedError:      # the stub feeds exit immediately
            pass
    return seen


class LocksAreKeyedByModel(unittest.TestCase):

    def test_two_teams_on_two_models_of_one_gateway_get_their_own_lanes(self):
        seen = _locks_for([("https://gw/v1", "k2"), ("https://gw/v1", "qwen3.8-27b")])
        self.assertIsNot(seen[1], seen[2])

    def test_two_teams_on_the_same_model_still_share_one_lock(self):
        seen = _locks_for([("https://gw/v1", "k2"), ("https://gw/v1", "k2")])
        self.assertIs(seen[1], seen[2])

    def test_a_local_endpoint_is_one_lane_whatever_the_model_name(self):
        # llama.cpp serves one model per URL; teams pointed at it serialise as before.
        seen = _locks_for([("http://10.0.0.212:4000/v1", "k2"),
                           ("http://10.0.0.212:4000/v1", "k2")])
        self.assertIs(seen[1], seen[2])


if __name__ == "__main__":
    unittest.main()
