"""Quotient engine gates: inject count vs injects/ dir, and the scoring round loop."""

import json
from datetime import datetime, timezone

import requests

import round_loop
from verifier.loaders import count_local_injects
from verifier.model import gate_fail, gate_pass, gate_skip


def check_injects(base_url, admin_session, comp_dir):
    """Compare engine inject count to the competition's injects/ dir. Returns (relevant, ok)."""
    print("\n[5/5] INJECTS")
    expected = count_local_injects(comp_dir)
    if expected is None:
        print("  SKIP  — competition ships no injects/ dir.")
        return False, True
    if admin_session is None:
        print("  FAIL  — no admin session to query /api/injects.")
        return True, False
    try:
        r = admin_session.get(f"{base_url}/api/injects", timeout=10)
        r.raise_for_status()
        injects = r.json() or []
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  FAIL  — could not fetch /api/injects: {e}")
        return True, False
    for inj in injects:
        title = inj.get("Title") or inj.get("title") or "?"
        print(f"      - {title}")
    ok = len(injects) == expected
    print(f"  {'PASS' if ok else 'FAIL'}  engine has {len(injects)} inject(s), "
          f"expected {expected} from injects/ dir")
    closed = closed_injects(injects)
    if closed:
        print(f"  WARN  {len(closed)} inject(s) already CLOSED — submissions return 'Inject is closed'. "
              "Offsets are anchored at the phase-7 deploy time, so a reset/rerun long after "
              f"deploy finds them expired: {', '.join(t for t, _ in closed)}")
    return True, ok


_INJECT_CLOSE_KEYS = ("CloseTime", "close_time", "CloseAt", "close_at", "Close", "close")


def check_round_loop(base_url, admin_session, fix=False):
    """Scoring round loop vs an engine reboot. The Docker containers restart after
    a reboot but the round loop stays stopped (pfsense-ad: frozen scoreboard) —
    and verify reads the LAST SCORED round, reporting stale UP/DOWN as if live.
    /api/engine is snake_case (live-confirmed 2026-09-29): `running` (False =
    paused), `competition_started`, `current_round_time` (RFC3339; the Go zero
    time "0001-01-01T00:00:00Z" means the loop is NOT cycling), and
    `last_round.StartTime`.

    Returns a GateResult and main CONSUMES it: a stopped loop used to WARN and
    return True while main discarded the return value, so a frozen scoreboard
    never affected the exit code. --fix-round-loop runs the two POSTs, but a
    stopped loop is still FAIL for this run (re-run to confirm a fresh round)."""
    print("\n  (scoring round loop — stops silently after an engine reboot)")
    if admin_session is None:
        print("  SKIP  — no admin session.")
        return gate_skip("round_loop", "no admin session")
    try:
        r = admin_session.get(f"{base_url}/api/engine", timeout=10)
        r.raise_for_status()
        eng = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  WARN  — could not read /api/engine: {e}")
        return gate_skip("round_loop", "could not read /api/engine")
    if not isinstance(eng, dict):
        return gate_skip("round_loop", "unexpected /api/engine payload")
    if eng.get("running") is False:
        print("  PASS  — engine paused (round loop not expected to advance)")
        return gate_pass("round_loop", "engine paused")

    # The judgement itself lives in round_loop.py so the engine-side watchdog
    # (tools/round_loop_guard.py) cannot drift from this gate — two definitions of "the
    # loop is stopped" is precisely the failure mode this repo keeps paying for.
    verdict = round_loop.round_loop_state(eng)
    if verdict["state"] == round_loop.UNKNOWN:
        print("  SKIP  — /api/engine did not answer a usable document")
        return gate_skip("round_loop", "unexpected /api/engine payload")
    if verdict["state"] == round_loop.ADVANCING:
        print("  PASS  — round loop advancing")
        return gate_pass("round_loop", "loop advancing")
    if verdict["state"] == round_loop.PENDING:
        print("  PASS  — round loop starting (first round pending within Delay)")
        return gate_pass("round_loop", "first round pending")
    age_min = (verdict["age_seconds"] or 0) / 60
    print(f"  WARN  — round loop looks STOPPED: last round started {age_min:.0f} min ago "
          "and current_round_time is the zero time (engine rebooted? the loop does not "
          "self-resume)")
    print('         fix: POST /api/competition/start {"started":true} then '
          'POST /api/engine/pause {"pause":false} — a fresh round lands within Delay s')
    if fix:
        r1 = admin_session.post(f"{base_url}/api/competition/start",
                                json={"started": True}, timeout=10)
        r2 = admin_session.post(f"{base_url}/api/engine/pause",
                                json={"pause": False}, timeout=10)
        print(f"  --fix-round-loop: start={r1.status_code} unpause={r2.status_code} — "
              f"re-run verify to confirm a fresh round landed")
    print("  FAIL  — a frozen scoreboard makes every service verdict above stale; main "
          "now consumes this result, so this run does not pass. Re-run verify once a "
          "fresh round lands.")
    return gate_fail("round_loop", f"loop stopped; last round {age_min:.0f} min ago")


def closed_injects(injects, now=None):
    """[(title, close_time)] for injects whose close time is already past. Tolerant of the
    engine's JSON key casing; injects with no parseable close time are ignored."""
    now = now or datetime.now(timezone.utc)
    out = []
    for inj in injects:
        raw = next((inj[k] for k in _INJECT_CLOSE_KEYS if inj.get(k)), None)
        if not isinstance(raw, str):
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < now:
            out.append((inj.get("Title") or inj.get("title") or "?", raw))
    return out
