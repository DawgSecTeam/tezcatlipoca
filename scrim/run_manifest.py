import json
from pathlib import Path

from artifacts_lib import artifacts_ops
from config.config_ops import write_state
from scrim import core
from scrim.red_stage import red_pacing_caps
from scrim.runfiles import RUN_MANIFEST




RESUME_MIN_CYCLES = 2


def save_manifest(run_dir, fields):
    write_state(Path(run_dir) / RUN_MANIFEST, fields)


def load_manifest(run_dir):
    try:
        data = json.loads((Path(run_dir) / RUN_MANIFEST).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Doubles as the pre-T0 marker for --resume-event; mirrored into the run's test.json so
# the artifact reader sees progress without cross-referencing run.json.
def record_phase(run_dir, args, phase, t0=None):
    # `phases` feeds the dashboard's per-stage events (dashboard/runner.py `_scrim_phases`);
    # `phase` is the latest marker the resume path keys on.
    manifest = load_manifest(run_dir)
    manifest.update({
        "competition": getattr(args, "competition", None),
        "teams": getattr(args, "teams", None),
        "duration_min": getattr(args, "duration_min", None),
        "t0": t0,
        "keep_range": bool(getattr(args, "keep_range", False)),
        "keep_templates": bool(getattr(args, "keep_templates", False)),
        "blue_watchdog": bool(getattr(args, "blue_watchdog", False)),
        # the scrim report's max_simultaneous_down gate judges against max_concurrent_down_end here
        "pacing": red_pacing_caps(),
        "phase": phase,
        "updated_at": core.now_iso(),
    })
    manifest.setdefault("phases", []).append(
        {"phase": phase, "at": core.now_iso(), "t0": t0})
    save_manifest(run_dir, manifest)
    test_dir = getattr(args, "test_dir", None)
    if test_dir:
        artifacts_ops.record_phase(test_dir, phase, t0=t0)


# OR semantics: a resume omitting --keep-range/--keep-templates must not drop the first
# run's intent and tear down or reclaim what it was keeping.
def resume_intent(args, manifest):
    args.keep_range = bool(getattr(args, "keep_range", False) or manifest.get("keep_range"))
    args.keep_templates = bool(getattr(args, "keep_templates", False)
                               or manifest.get("keep_templates"))
    args.blue_watchdog = bool(getattr(args, "blue_watchdog", False)
                              or manifest.get("blue_watchdog"))
    return args


# the feed loop stops issuing cycles at duration_min - 2, hence the +2 margin on top of the cycles
def resume_window_ok(remaining_min, min_cycles=RESUME_MIN_CYCLES):
    return remaining_min >= min_cycles * (core.CYCLE_TARGET_PERIOD / 60.0) + 2


def resume_refusal(remaining_min, force=False):
    if force or resume_window_ok(remaining_min):
        return None
    return (f"--resume-event refused: {remaining_min:.0f} min left of the event window "
            f"(T0 + duration), fewer than {RESUME_MIN_CYCLES} blue cycles of "
            f"{core.CYCLE_TARGET_PERIOD // 60} min can still run. A resume now would log "
            f"re-entry, run nothing, and still capture + tear down. Pass --force-resume "
            f"to capture/tear down anyway.")
