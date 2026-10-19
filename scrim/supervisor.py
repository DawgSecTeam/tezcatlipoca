import time

from scrim import core
from scrim.core import log


# a dead feed leaves a team unattended and a dead monitor strips the report's evidence:
# the harness must not exit clean with a worker down
class WorkerDiedError(RuntimeError):
    pass


# a worker may be parked in a full opencode cycle when the window closes; joining less abandons
# it and capture/teardown race the live cycle. Worst case: one CYCLE_TIMEOUT (no retry once stop is set)
WORKER_JOIN_BUDGET = core.CYCLE_TIMEOUT + 120

# but joining by a whole-cycle bound let a 25-min event reach capture at minute 50 (caught live:
# the join waited a cycle started at T+5); past the deadline only this grace is needed
EVENT_END_GRACE = 120


# nothing else observes these threads: without polling, a dead worker goes unnoticed
def dead_workers(threads, reported):
    return [t for t in threads if not t.is_alive() and t not in reported]


def join_workers(threads, budget=None):
    budget = WORKER_JOIN_BUDGET if budget is None else budget
    deadline = time.time() + budget
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.time()))
    return [t for t in threads if t.is_alive()]


def supervise_workers(threads, deadline, stop, poll=60, t0=None):
    reported = []
    try:
        while time.time() < deadline:
            # never sleep past the deadline: overrun is event time the clock-bounded deliverables lose
            time.sleep(min(poll, max(0.0, deadline - time.time())))
            for t in dead_workers(threads, reported):
                when = f" at T+{int((time.time() - t0) // 60)}min" if t0 else ""
                reported.append(t)
                log(f"ERROR: worker thread {t.name!r} DIED{when} — whatever it was "
                    f"supervising is now unattended")
    finally:
        stop.set()
        budget = min(WORKER_JOIN_BUDGET,
                     max(0.0, deadline - time.time()) + EVENT_END_GRACE)
        for t in join_workers(threads, budget=budget):
            log(f"ERROR: worker thread {t.name!r} did not stop within "
                f"{budget:.0f}s of the event window closing — capture/teardown "
                f"would have raced it")
            reported.append(t)
    return reported
