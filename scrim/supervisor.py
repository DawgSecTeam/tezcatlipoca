import time

from scrim import core
from scrim.core import log


class WorkerDiedError(RuntimeError):
    """A monitor/feed/watchdog worker thread stopped on its own during the event.

    The event itself keeps running (the surviving teams still need their feeds and the
    watchdog is still keeping services up), but the harness must not report a clean run:
    a dead feed thread means that team's agent was unattended, and a dead monitor thread
    means the evidence the post-hoc report depends on simply is not there (audit find D3).
    """


# A worker may be parked inside a full opencode cycle when the window closes; 10s used to
# abandon it, and stage_capture/stage_teardown then wrote into (and destroyed infra under)
# a live cycle. One CYCLE_TIMEOUT plus slack is the honest bound (the retry is skipped
# once stop is set, so a single attempt is the worst case).
WORKER_JOIN_BUDGET = core.CYCLE_TIMEOUT + 120


def dead_workers(threads, reported):
    """Threads that stopped without being asked to, excluding already-reported ones.

    Liveness is polled rather than assumed: nothing observed these threads before, so a
    worker killed by a StopIteration on an error body (or any other exception escaping
    its own handler) left the harness running to completion with no indication that its
    monitoring had stopped.
    """
    return [t for t in threads if not t.is_alive() and t not in reported]


def join_workers(threads, budget=None):
    """Join every worker against ONE shared deadline; return the still-alive ones.

    Budget is resolved at call time so tests (and an operator override) can shorten it.
    """
    budget = WORKER_JOIN_BUDGET if budget is None else budget
    deadline = time.time() + budget
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.time()))
    return [t for t in threads if t.is_alive()]


def supervise_workers(threads, deadline, stop, poll=60, t0=None):
    """Poll worker liveness until `deadline`, then stop and join them all.

    Returns the workers that died on their own or refused to stop within the join budget.
    An empty list means every worker ran to the end of the window and shut down cleanly.
    """
    reported = []
    try:
        while time.time() < deadline:
            time.sleep(poll)
            for t in dead_workers(threads, reported):
                when = f" at T+{int((time.time() - t0) // 60)}min" if t0 else ""
                reported.append(t)
                log(f"ERROR: worker thread {t.name!r} DIED{when} — whatever it was "
                    f"supervising is now unattended")
    finally:
        stop.set()
        for t in join_workers(threads):
            log(f"ERROR: worker thread {t.name!r} did not stop within "
                f"{WORKER_JOIN_BUDGET}s of the event window closing — capture/teardown "
                f"would have raced it")
            reported.append(t)
    return reported
