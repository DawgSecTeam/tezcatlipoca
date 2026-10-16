import time

from scrim import core
from scrim.core import log


# a worker dying on its own must not let the harness exit clean: a dead feed leaves a team
# unattended, a dead monitor leaves the post-hoc report without its evidence (audit find D3)
class WorkerDiedError(RuntimeError):
    pass


# A worker may be parked inside a full opencode cycle when the window closes; 10s used to
# abandon it, and stage_capture/stage_teardown then wrote into (and destroyed infra under)
# a live cycle. One CYCLE_TIMEOUT plus slack is the honest bound (the retry is skipped
# once stop is set, so a single attempt is the worst case).
WORKER_JOIN_BUDGET = core.CYCLE_TIMEOUT + 120

# ...but that bound is a whole cycle, so joining by it lets a 25-minute event reach capture at
# minute 50 (live 2026-10-15, run-2652d9bc: the join waited out a cycle started at T+5). Past
# the deadline the join is capped by what is left of the window plus this grace, which is all
# a cycle that is already wrapping up needs.
EVENT_END_GRACE = 120


# polled rather than assumed: nothing else observed these threads, so a worker killed by
# an exception escaping its own handler otherwise goes unnoticed to run completion
def dead_workers(threads, reported):
    return [t for t in threads if not t.is_alive() and t not in reported]


# budget resolved at call time so tests (and an operator override) can shorten it
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
            # never sleep past the deadline: at a 60s poll the loop used to overrun it by
            # up to a minute, which is event time the clock-bounded deliverables lost
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
