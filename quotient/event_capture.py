"""Quotient event capture + pause: capture the final scoreboard BEFORE pausing (a paused engine still answers these APIs), and never let a capture failure skip the pause."""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from config.config_ops import write_text_atomic


# The shared login uses the `scoring` automation account for least privilege and a clean
# audit trail. A 2026-10-18 live probe did NOT reproduce same-account cookie eviction, so
# avoiding eviction is no longer the stated reason.
def login_scoring(base_url, password):
    session = requests.Session()
    r = session.post(f"{base_url}/api/login",
                     json={"username": "scoring", "password": password}, timeout=10)
    if r.status_code >= 400:
        raise RuntimeError(f"scoring login failed ({base_url}) → HTTP {r.status_code}")
    return session


def dump_scoreboard(base_url, session, comp_dir, *, out_dir=None):
    out_dir = Path(out_dir) if out_dir else Path(comp_dir) / "evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = {"captured_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    errors = []

    def _get(path, label):
        # A 200 whose body is literally `null` is a failed read too: callers do errors[-1],
        # so every failure must append an error.
        try:
            r = session.get(f"{base_url}{path}", timeout=10)
            r.raise_for_status()
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            return None, f"{label}: {type(e).__name__}: {e}"
        if payload is None:
            return None, f"{label}: empty response body (null)"
        return payload, ""

    teams, err = _get("/api/teams", "teams")
    if err:
        errors.append(err)
    if not isinstance(teams, list):
        if teams is not None:
            errors.append("teams: unexpected payload (not a list)")
        dump["teams_error"] = errors[-1] if errors else "unreadable"
        teams = []
    for t in teams:
        if not isinstance(t, dict):
            continue
        services, err = _get(f"/api/services/{t.get('ID')}", f"services/{t.get('ID')}")
        if services is None:
            if err:
                errors.append(err)
            t["services_error"] = err or "unreadable"
        else:
            t["services"] = services
    dump["teams"] = teams
    injects, err = _get("/api/injects", "injects")
    if injects is None:
        if err:
            errors.append(err)
        dump["injects_error"] = err or "unreadable"
    else:
        dump["injects"] = injects
    out = out_dir / f"event-final-{time.strftime('%Y%m%d-%H%M%S')}.json"
    # 0600 + atomic: the only copy of the final scores once the engine is torn down.
    write_text_atomic(out, json.dumps(dump, indent=2), mode=0o600)
    if errors:
        print(f"  WARNING: scoreboard capture incomplete ({len(errors)} error(s)) → {out}")
        for e in errors:
            print(f"    {e}")
    else:
        print(f"  Final scoreboard captured → {out}")
    return errors


def pause_engine(base_url, session):
    try:
        r = session.post(f"{base_url}/api/engine/pause", json={"pause": True}, timeout=10)
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {e}"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}"
    return True, ""


def end_event(base_url, session, comp_dir):
    # Non-zero exit unless capture AND pause both succeeded: an engine still scoring
    # is not "over" however green the printed schedule looks.
    errors = []
    try:
        errors = dump_scoreboard(base_url, session, comp_dir)
    except Exception as e:
        print(f"  ERROR: scoreboard capture crashed: {type(e).__name__}: {e}", file=sys.stderr)
        errors = [f"capture crashed: {type(e).__name__}: {e}"]
    ok, why = pause_engine(base_url, session)
    print(f"  END: engine/pause={'ok' if ok else 'FAILED'}" + (f" ({why})" if why else ""))
    if not ok:
        print("  ERROR: the engine is still scoring; pause it by hand before announcing "
              "the event is over")
    return 0 if (ok and not errors) else 1
