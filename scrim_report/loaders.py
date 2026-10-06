import json
from pathlib import Path

from scrim_report import timefmt
from scrim.runfiles import ALERTS_FILENAME, FINAL_SCOREBOARD, RUN_MANIFEST, SCOREBOARD_STATE


def load_alerts(run_dir):
    """Run-dir alert journal (red_llm_* notices) — [] when the run wrote none.

    These used to exist only as log() lines in the driver's stdout, which the post-hoc
    report never sees: an event that ran red-LLM-blind for an hour left no trace here.
    """
    path = Path(run_dir) / "evidence" / ALERTS_FILENAME
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def load_red_events(run_dir):
    """Merge final events.jsonl with in-run snapshots, window-filtered and deduplicated."""
    red = Path(run_dir) / "evidence" / "red"
    files = sorted(red.glob("events*.jsonl")) if red.is_dir() else []
    events, seen = [], set()
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            key = (ev.get("ts"), ev.get("kind"), ev.get("tactic"),
                   ev.get("target"), ev.get("detail"))
            if key in seen:
                continue
            seen.add(key)
            events.append(ev)
    t0 = load_event_start(run_dir)
    if t0 is not None:
        events = [e for e in events
                  if t0 - 120 <= timefmt.parse_ts(e.get("ts", "")) <= t0 + 8 * 3600]
    events.sort(key=lambda e: timefmt.parse_ts(e.get("ts", "1970")))
    return events, t0


def load_event_start(run_dir):
    """The event's T0, harness-first.

    The harness's run.json t0 is when the event ACTUALLY started (written once, after
    red01 was up). bad-auto's world.json event_start is written on the FIRST harness
    contact — including a failed stage_red attempt whose red01 never deployed — so on a
    retried red deploy it predates red01's existence and every red event reads as
    shifted late (live-found 2026-10-03: a 14-minute-early t0 manufactured a phantom
    "27-minute opening stall" out of red's normal pre-T0 sprint). Harness wins; the
    world.json value remains the fallback for a run dir without run.json."""
    try:
        run = json.loads((Path(run_dir) / RUN_MANIFEST).read_text())
        if run.get("t0"):
            return float(run["t0"])
    except (OSError, ValueError):
        pass
    for cand in (Path(run_dir) / "evidence" / "red" / "world.json",
                 Path(run_dir) / "bad-auto-state" / "world.json"):
        try:
            w = json.loads(cand.read_text())
            t0 = (w.get("meta") or {}).get("event_start")
            if t0:
                return float(t0)
        except (OSError, ValueError):
            continue
    return None


def load_world(run_dir):
    for cand in (Path(run_dir) / "evidence" / "red" / "world.json",
                 Path(run_dir) / "bad-auto-state" / "world.json"):
        try:
            return json.loads(cand.read_text())
        except (OSError, ValueError):
            continue
    return {}


def load_scoreboard(run_dir):
    """[(t_plus_sec, {team: {service: up}})] or [] when the run predates C1."""
    path = Path(run_dir) / SCOREBOARD_STATE
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        out.append((rec.get("t_plus_sec", 0),
                    {t: {s["service"]: s["up"] for s in (svcs or [])}
                     for t, svcs in (rec.get("teams") or {}).items()}))
    return sorted(out, key=lambda rec: rec[0])


def load_pacing(run_dir):
    """red's concurrent-down caps from the run manifest ({} for legacy run dirs)."""
    try:
        run = json.loads((Path(run_dir) / RUN_MANIFEST).read_text())
        pacing = run.get("pacing")
        return pacing if isinstance(pacing, dict) else {}
    except (OSError, ValueError):
        return {}


def load_final_scoreboard(run_dir):
    """The stage_capture evidence dump (all teams, taken before teardown destroys the DB)."""
    try:
        data = json.loads((Path(run_dir) / "evidence" / FINAL_SCOREBOARD).read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
