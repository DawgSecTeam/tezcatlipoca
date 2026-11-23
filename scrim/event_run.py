import threading

from artifacts_lib import artifacts_ops
from scrim import blue_agent
from scrim import blue_watchdog
from scrim import endpoints
from scrim import evidence
from scrim import red_c3rberus
from scrim import red_watchdog
from scrim import run_manifest
from scrim import scoreboard_monitor
from scrim import supervisor
from scrim import teardown_stage
from scrim.core import CYCLE_TIMEOUT, log


def _endpoint_gates(args, teams=None):
    """One gate per (base_url, model), sized by --blue-concurrency.

    A llama.cpp slot serialises, so one lock per endpoint was right there; a vLLM backend
    serving N sessions concurrently does not, and serialising its teams throws that away.
    Default 1 keeps the old single-lock behaviour.
    """
    teams = args.teams if teams is None else teams
    capacity = max(1, int(getattr(args, "blue_concurrency", 1) or 1))
    gates, shared = {}, {}
    for n in range(1, teams + 1):
        key = endpoints.blue_ep(args, n)
        gates.setdefault(key, threading.Semaphore(capacity))
        shared[key] = shared.get(key, 0) + 1
    return gates, shared


def stage_run(args, creds, t0):
    stop = threading.Event()
    # One lock per distinct LLM endpoint MODEL — teams sharing an endpoint+model share its
    # concurrency cap; teams 3/4 used to silently inherit team1's endpoint + lock.
    # Keyed by model as well as URL because a gateway multiplexes its models: two teams on
    # two models of one gateway contend with each other's upstreams, not a llama.cpp slot
    # (run-84c0e45d serialised both blues on one gateway lock, then both starved on one
    # model's stream drops).
    endpoint_locks, shared = _endpoint_gates(args)
    capacity = max(1, int(getattr(args, "blue_concurrency", 1) or 1))
    for (url, model), count in shared.items():
        if count > capacity:
            log(f"blue: {count} teams share {model} @ {url} with {capacity} concurrent "
                f"session(s) — TEZ_CYCLE_TIMEOUT={CYCLE_TIMEOUT}s over "
                f"{args.duration_min} min gives each ~{args.duration_min // count} min. "
                f"Give a team its own model, a larger --blue-concurrency, or a longer "
                f"window for a full rehearsal.")
    threads = []
    for n in range(1, args.teams + 1):
        threads.append(threading.Thread(target=blue_agent.blue_feed_loop, name=f"blue-feed-{n}",
                                        args=(n, args, creds, t0, stop,
                                              endpoint_locks[endpoints.blue_ep(args, n)],
                                              300.0 * (n - 1))))
    threads.append(threading.Thread(target=scoreboard_monitor.monitor_loop, name="monitor",
                                    args=(args, creds, t0, stop)))
    # The c3rberus operator: supervised like a blue team — its death IS a broken event
    # when the platform is in play (tlaloc alone would hide that half went silent).
    if getattr(args, "red_c3rberus", None):
        threads.append(threading.Thread(target=red_c3rberus.red_feed_loop,
                                        name="red-operator", args=(args, stop, t0)))
    if getattr(args, "blue_watchdog", False):
        threads.append(threading.Thread(target=blue_watchdog.blue_watchdog_loop, name="blue-watchdog",
                                        args=(args, creds, t0, stop)))
    # red-loop watchdog (A5): best-effort guardian, deliberately NOT in the supervised
    # list — a dead watchdog must degrade the event's protection, never WorkerDiedError
    # it. stop is shared, so it ends with the event; daemon as a second guarantee.
    threading.Thread(target=red_watchdog.watch_loop, name="red-watchdog",
                     args=(args, stop), daemon=True).start()
    for t in threads:
        t.start()
    reported = supervisor.supervise_workers(threads, t0 + args.duration_min * 60, stop, t0=t0)
    if reported:
        raise supervisor.WorkerDiedError(
            "worker thread(s) stopped or hung during the event: "
            + ", ".join(t.name for t in reported)
            + " — that team's feed/monitoring was not running; treat the evidence as partial")


# teardown MUST still run even when a worker died (the range is expensive and a live cycle
# can otherwise be destroyed under it), but the failure is returned so main() exits
# non-zero after the evidence is safely on disk
def run_event_and_finish(args, creds, t0):
    failure = None
    try:
        stage_run(args, creds, t0)
    except supervisor.WorkerDiedError as e:
        failure = str(e)
    # Before the capture, so a blue team that finished without writing REPORT.md still gets a
    # narrative into the artifact set (the harness holds the cycle record; blue was present).
    try:
        blue_agent.finalize_blue_narratives(args)
    except Exception as e:
        log(f"WARNING: blue narrative finalize failed: {e}")
    run_manifest.record_phase(args.run_dir, args, "capture")
    evidence.stage_capture(args, creds)
    run_manifest.record_phase(args.run_dir, args, "teardown")
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
