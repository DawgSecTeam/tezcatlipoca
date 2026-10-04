import json
from pathlib import Path

import artifacts_ops
from config_ops import write_state
from scrim import core
from scrim.runfiles import RUN_MANIFEST




RESUME_MIN_CYCLES = 2


def save_manifest(run_dir, fields):
    """Persist the run's intent (duration, retention, watchdog, phase) atomically, 0600."""
    write_state(Path(run_dir) / RUN_MANIFEST, fields)


def load_manifest(run_dir):
    """The run manifest, or {} when absent/unreadable (older run dirs have none)."""
    try:
        data = json.loads((Path(run_dir) / RUN_MANIFEST).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_phase(run_dir, args, phase, t0=None):
    """Write/refresh the run manifest; doubles as the pre-T0 marker for --resume-event.

    Also mirrors the marker into this run's test folder (test.json) when it has one, so the
    artifact reader can see how far a run got without cross-referencing run.json. run.json
    stays the harness's own file (`save_manifest`/`load_manifest` above), written exactly as
    before; `getattr` because callers without a test folder (unit tests, legacy run dirs)
    must keep working.
    """
    save_manifest(run_dir, {
        "competition": getattr(args, "competition", None),
        "teams": getattr(args, "teams", None),
        "duration_min": getattr(args, "duration_min", None),
        "t0": t0,
        "keep_range": bool(getattr(args, "keep_range", False)),
        "blue_watchdog": bool(getattr(args, "blue_watchdog", False)),
        "phase": phase,
        "updated_at": core.now_iso(),
                  })
    test_dir = getattr(args, "test_dir", None)
    if test_dir:
        artifacts_ops.record_phase(test_dir, phase, t0=t0)


def resume_intent(args, manifest):
    """Fold the ORIGINAL run's retention/watchdog intent into the resume invocation.

    That intent lived only in the first CLI invocation, so a resume without --keep-range
    tore down a range the original run was keeping up. OR semantics: a resume cannot
    silently drop an intent the first run had, and an operator who passes the flag on the
    resume still gets it.
    """
    args.keep_range = bool(args.keep_range or manifest.get("keep_range"))
    args.blue_watchdog = bool(args.blue_watchdog or manifest.get("blue_watchdog"))
    return args


def resume_window_ok(remaining_min, min_cycles=RESUME_MIN_CYCLES):
    """True when at least `min_cycles` blue cycles still fit inside the window.

    The feed loop stops issuing cycles at duration_min - 2, so that margin is required
    on top of the cycles themselves.
    """
    return remaining_min >= min_cycles * (core.CYCLE_TARGET_PERIOD / 60.0) + 2


def resume_refusal(remaining_min, force=False):
    """None when resuming is worthwhile, otherwise the message that refuses it.

    A driver that died at T+85 of a 90-min event left remaining=5: every loop exits
    immediately, the log claims "RESUME", nothing runs — and the run then marched on to
    capture and TEAR DOWN a range the original run may have meant to keep.
    """
    if force or resume_window_ok(remaining_min):
        return None
    return (f"--resume-event refused: {remaining_min:.0f} min left of the event window "
            f"(T0 + duration), fewer than {RESUME_MIN_CYCLES} blue cycles of "
            f"{core.CYCLE_TARGET_PERIOD // 60} min can still run. A resume now would log "
            f"re-entry, run nothing, and still capture + tear down. Pass --force-resume "
            f"to capture/tear down anyway.")
