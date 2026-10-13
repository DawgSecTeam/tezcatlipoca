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
