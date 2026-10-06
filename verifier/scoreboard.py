"""Quotient scoreboard gates: logins and per-team service UP/DOWN (with freshness and pin registration)."""

import json
from datetime import datetime, timezone

import requests

from verifier.model import Status, bool_gate, gate_fail, gate_skip


# Round cadence: quotient/setup.py build_event_conf's MiscSettings.Delay. Used as
# the freshness unit — a scored round older than _FRESHNESS_ROUNDS × Delay means
# the scoreboard is frozen and an old UP is not a live UP (see D7 / the
# pfsense-ad frozen-scoreboard incident).
_ROUND_DELAY_SECONDS = 60


_FRESHNESS_ROUNDS = 5


def _rfc3339(value):
    """Parse an RFC3339 timestamp; None when absent/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


# Quotient's Last10Rounds key casing is not guaranteed; tolerate the variants the
# codebase already sees elsewhere (check_round_loop reads the capital form).
_ROUND_START_KEYS = ("StartTime", "start_time", "startTime")


def _round_start(round_entry):
    """StartTime of one Last10Rounds entry, however it's cased."""
    if not isinstance(round_entry, dict):
        return None
    raw = next((round_entry[k] for k in _ROUND_START_KEYS if round_entry.get(k)), None)
    return _rfc3339(raw)


def check_logins(base_url, teams, admin_password):
    """POST /api/login for admin + every team. Returns (all_ok, admin_session)."""
    print("\n[1/5] LOGIN")
    all_ok = True

    def try_login(username, password):
        session = requests.Session()
        try:
            r = session.post(f"{base_url}/api/login",
                             json={"username": username, "password": password}, timeout=10)
        except requests.RequestException as e:
            print(f"  FAIL  {username:<10} — request error: {e}")
            return None
        ok = r.status_code == 200
        print(f"  {'PASS' if ok else 'FAIL'}  {username:<10} — HTTP {r.status_code}")
        return session if ok else None

    admin_session = try_login("admin", admin_password)
    if admin_session is None:
        all_ok = False
    for team_name, data in teams.items():
        if try_login(team_name, data.get("password", "")) is None:
            all_ok = False
    return all_ok, admin_session


def _check_passed(check):
    """Check Result truthiness with string normalization ("false"/"0" -> False)."""
    result = check.get("Result")
    if isinstance(result, str):
        return result.strip().lower() not in ("", "0", "false")
    return bool(result)


def check_services(base_url, admin_session, teams, strict, expected_names=frozenset()):
    """Report per-team service UP/DOWN from the latest scored round.

    Returns a list of GateResult: the service gate plus (when box_services.json pins
    exist) a separate `pins_registered` result. Every path returns the same shape —
    the old code returned a 2-tuple on the /api/teams failure path while main
    unpacked 3, so a half-deployed or restarting engine (exactly the state this gate
    exists for) crashed verify with ValueError: not enough values to unpack, dumping
    a traceback and suppressing the SUMMARY and every other gate result.

    pins_registered gates the scoreboard's actual ServiceName set against the pins'
    expected set — a pin that never registered (the regression-4x1 same-TYPE collapse:
    12 pins, 11 checks) scores nothing and silent-tallies as UP-absent, so it fails
    the exit code regardless of --strict.

    Under --strict-services nothing-scored is a FAIL, not a vacuous pass (live-found
    2026-10-02: a range where the engine had never scored a round still passed strict
    mode because every service was skipped as "not yet scored"), and the newest scored
    round must be within _FRESHNESS_ROUNDS × Delay — otherwise the scoreboard is frozen
    and an old UP is not a live UP."""
    print("\n[2/5] SERVICES")
    name = "services(strict)" if strict else "services"

    def _pins_status(ok, detail):
        return bool_gate("pins_registered", ok, detail) if expected_names else None

    if admin_session is None:
        print("  SKIP  — no admin session (login failed).")
        out = [gate_skip(name, "no admin session (login failed)", gating=strict, label="services")]
        pins = _pins_status(False, "no admin session")
        if pins:
            # Could not be evaluated, not a proven regression — but pins are always
            # gating, so the operator must see SKIP, never a silent pass.
            pins.status = Status.SKIP_UNAVAILABLE
            out.append(pins)
        return out
    try:
        r = admin_session.get(f"{base_url}/api/teams", timeout=10)
        r.raise_for_status()
        api_teams = r.json()
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        print(f"  FAIL  — could not fetch /api/teams: {e}")
        out = [gate_fail(name, f"could not fetch /api/teams ({str(e)[:60]})",
                     gating=strict, label="services")]
        pins = _pins_status(False, "could not fetch /api/teams")
        if pins:
            out.append(pins)
        return out

    query_ok = True
    all_up = True
    actual_names = set()
    any_service = False
    any_scored = False
    newest_round = None
    for t in api_teams:
        tid, tname = t.get("ID"), t.get("Name", t.get("Identifier"))
        try:
            r = admin_session.get(f"{base_url}/api/services/{tid}", timeout=10)
            r.raise_for_status()
            services = r.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            print(f"  WARN  {tname}: could not fetch services: {e}")
            query_ok = False
            all_up = False
            continue
        actual_names |= {svc.get("ServiceName", "?") for svc in services or []}
        up = 0
        unscored = 0
        down_names = []
        for svc in services or []:
            any_service = True
            name_ = svc.get("ServiceName", "?")
            rounds = svc.get("Last10Rounds") or []
            for rnd in rounds:
                when = _round_start(rnd)
                if when is not None and (newest_round is None or when > newest_round):
                    newest_round = when
            first_round = rounds[0] if rounds else None
            checks = (first_round.get("Checks") if isinstance(first_round, dict) else None) or []
            if not checks:
                unscored += 1
                continue
            any_scored = True
            if all(_check_passed(c) for c in checks):
                up += 1
            else:
                down_names.append(name_)
        total = len(services or []) - unscored
        note = f" ({unscored} not yet scored)" if unscored else ""
        print(f"  {tname}: {up}/{total} services UP{note}")
        for down in down_names:
            print(f"      DOWN: {down}")
        if down_names:
            all_up = False
    if not any_service:
        print("  (no services reported yet — engine may not have scored a round)")
    if strict and not any_service:
        print("  FAIL  --strict-services: the engine reports no services for any team.")
        all_up = False
    elif strict and not any_scored:
        print("  FAIL  --strict-services: no service has been scored yet (the engine may "
              "never have run a round) — an unscored scoreboard is not a passing one.")
        all_up = False
    elif strict and newest_round is not None:
        age_s = (datetime.now(timezone.utc) - newest_round).total_seconds()
        if age_s > _FRESHNESS_ROUNDS * _ROUND_DELAY_SECONDS:
            print(f"  FAIL  --strict-services: newest scored round started "
                  f"{age_s / 60:.0f} min ago (> {_FRESHNESS_ROUNDS}×"
                  f"{_ROUND_DELAY_SECONDS}s) — the scoreboard is stale/frozen, so this "
                  "UP is not a live UP (engine rebooted? the loop does not self-resume).")
            all_up = False
    elif strict and newest_round is None:
        print("  FAIL  --strict-services: no parseable round StartTime in Last10Rounds — "
              "cannot establish the scoreboard is fresh.")
        all_up = False
    if strict and not all_up:
        print("  --strict-services: some services DOWN/unscored/stale -> counts against "
              "exit code")
    pins_ok = True
    if expected_names:
        missing = sorted(expected_names - actual_names)
        extras = sorted(actual_names - expected_names)
        if missing:
            print(f"  FAIL  pin(s) never registered on the scoreboard: {', '.join(missing)}")
            print("        (a pin whose check never registered scores nothing — the "
                  "regression-4x1 same-TYPE collapse shape)")
            pins_ok = False
        else:
            print(f"  EXPECTED-PINS  all {len(expected_names)} pinned checks registered")
        if extras:
            print(f"  WARN  scoreboard services not derived from box_services.json: {', '.join(extras)}")
    detail = f"{'UP' if all_up else 'some DOWN/unscored/stale'} " \
             f"({'query ok' if query_ok else 'query failed'})"
    out = [bool_gate(name, all_up, detail, gating=strict, label="services")]
    pins = _pins_status(pins_ok, f"{len(expected_names)} pinned checks")
    if pins:
        out.append(pins)
    return out
