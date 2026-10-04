import threading

import artifacts_ops
from scrim import blue_agent
from scrim import blue_watchdog
from scrim import endpoints
from scrim import evidence
from scrim import scoreboard_monitor
from scrim import supervisor
from scrim import teardown_stage
from scrim.core import log


def stage_run(args, creds, t0):
    stop = threading.Event()
    # One lock per distinct LLM endpoint — teams sharing an endpoint share its
    # concurrency cap; teams 3/4 used to silently inherit team1's endpoint + lock.
    endpoint_locks = {}
    for n in range(1, args.teams + 1):
        base_url, _ = endpoints.blue_ep(args, n)
        endpoint_locks.setdefault(base_url, threading.Lock())
    threads = []
    for n in range(1, args.teams + 1):
        base_url, _ = endpoints.blue_ep(args, n)
        threads.append(threading.Thread(target=blue_agent.blue_feed_loop, name=f"blue-feed-{n}",
                                        args=(n, args, creds, t0, stop,
                                              endpoint_locks[base_url], 300.0 * (n - 1))))
    threads.append(threading.Thread(target=scoreboard_monitor.monitor_loop, name="monitor",
                                    args=(args, creds, t0, stop)))
    if getattr(args, "blue_watchdog", False):
        threads.append(threading.Thread(target=blue_watchdog.blue_watchdog_loop, name="blue-watchdog",
                                        args=(args, creds, t0, stop)))
    for t in threads:
        t.start()
    reported = supervisor.supervise_workers(threads, t0 + args.duration_min * 60, stop, t0=t0)
    if reported:
        raise supervisor.WorkerDiedError(
            "worker thread(s) stopped or hung during the event: "
            + ", ".join(t.name for t in reported)
            + " — that team's feed/monitoring was not running; treat the evidence as partial")


def run_event_and_finish(args, creds, t0):
    """stage_run -> capture -> teardown -> finalize, capturing even when a worker died.

    Teardown MUST still run (the range is expensive and a live cycle can be destroyed
    under it otherwise), but the harness must not exit clean afterwards: the failure is
    returned so main() can exit non-zero after the evidence is safely on disk.
    """
    failure = None
    try:
        stage_run(args, creds, t0)
    except supervisor.WorkerDiedError as e:
        failure = str(e)
    evidence.stage_capture(args, creds)
    teardown_stage.stage_teardown(args, creds)
    # Last, and after teardown, so it also runs when --keep-range skipped the range
    # destroy: stubs + REPORT.md + index.json make the folder a standard test artifact
    # either way. Wrapped, because reporting must never turn a finished run into a crash
    # (the destroy has already happened by now).
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        log("WARNING: no test folder on this run — test artifacts not finalized")
    else:
        try:
            result = artifacts_ops.finalize(test_dir)
            for warning in (result or {}).get("warnings") or []:
                log(f"WARNING: {warning}")
            log(f"test artifacts finalized: {test_dir}")
        except Exception as e:
            log(f"WARNING: could not finalize test artifacts in {test_dir}: {e}")
    return failure
