"""The blue feed loop's cycle budget is shorter than the event window; exiting at that budget
made the supervisor report a DIED worker and fail an otherwise healthy run
(live: cde run-65e60fc7, blue-feed-1 at T+44min). The worker must stay alive until the
supervisor stops it."""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from scrim import blue_agent


def test_the_worker_waits_for_stop_after_its_cycle_budget(tmp_path):
    args = SimpleNamespace(duration_min=2, run_dir=str(tmp_path))
    stop = threading.Event()
    t = threading.Thread(target=blue_agent.blue_feed_loop,
                         args=(1, args, {}, time.time(), stop, threading.Lock()))
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), "worker exited at its cycle budget instead of waiting for stop"
    stop.set()
    t.join(5)
    assert not t.is_alive()
