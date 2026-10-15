"""Quotient event capture + pause, shared by run-schedule.py and competition-ops.py.

`end` semantics (the stage_capture lesson): capture the final scoreboard FIRST —
the pause is what freezes the loop's last round, but a paused engine still answers
these APIs — then pause. Capture is deliberately tolerant: the engine can answer 500
or a non-JSON body mid-round, and the old `.json()` calls propagated straight out of
main() BEFORE the pause ran, so the schedule said the event was over while scoring
kept running (audit find D7). Every failure is recorded inside the dump instead, so
the pause always gets its turn and the operator still gets (partial) evidence on disk.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from config.config_ops import write_text_atomic


def dump_scoreboard(base_url, session, comp_dir):
# Capture the final scoreboard; returns the list of capture errors ([] = clean).
    out_dir = Path(comp_dir) / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = {"captured_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    errors = []

    def _get(path, label):
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            errors.append(f"{label}: {type(e).__name__}: {e}")
            return None

    teams = _get("/api/teams", "teams")
    if not isinstance(teams, list):
        if teams is not None:
            errors.append("teams: unexpected payload (not a list)")
        dump["teams_error"] = errors[-1] if errors else "unreadable"
        teams = []
    for t in teams:
        if not isinstance(t, dict):
            continue
        services = _get(f"/api/services/{t.get('ID')}", f"services/{t.get('ID')}")
        if services is None:
            t["services_error"] = errors[-1]
        else:
            t["services"] = services
    dump["teams"] = teams
    injects = _get("/api/injects", "injects")
    if injects is None:
        dump["injects_error"] = errors[-1]
    else:
        dump["injects"] = injects
    out = out_dir / f"event-final-{time.strftime('%Y%m%d-%H%M%S')}.json"
    # Atomic + 0600 at creation: this is the only copy of the final scores once the
    # engine is torn down, and a torn write here is unrecoverable (config_ops helper).
    write_text_atomic(out, json.dumps(dump, indent=2), mode=0o600)
    if errors:
        print(f"  WARNING: scoreboard capture incomplete ({len(errors)} error(s)) → {out}")
        for e in errors:
            print(f"    {e}")
    else:
        print(f"  Final scoreboard captured → {out}")
    return errors


def pause_engine(base_url, session):
# POST engine/pause True; returns (ok, detail). Never raises.
    try:
        r = session.post(f"{base_url}/api/engine/pause", json={"pause": True}, timeout=10)
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    return True, ""


def end_event(base_url, session, comp_dir):
# Capture, then pause — the pause is attempted even when the capture fails.
#
#     Returns the process exit code: non-zero when the capture was incomplete OR the pause
#     failed, because an event whose engine keeps scoring is not "over" however green the
#     printed schedule looks.
    errors = []
    try:
        errors = dump_scoreboard(base_url, session, comp_dir)
    except Exception as e:
        # Nothing the capture does may skip the pause below.
        print(f"  ERROR: scoreboard capture crashed: {type(e).__name__}: {e}", file=sys.stderr)
        errors = [f"capture crashed: {type(e).__name__}: {e}"]
    ok, why = pause_engine(base_url, session)
    print(f"  END: engine/pause={'ok' if ok else 'FAILED'}" + (f" ({why})" if why else ""))
    if not ok:
        print("  ERROR: the engine is still scoring; pause it by hand before announcing "
              "the event is over")
    return 0 if (ok and not errors) else 1
