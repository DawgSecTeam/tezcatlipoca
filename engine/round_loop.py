"""Quotient's scoring round loop: is it cycling, and how do we tell?"""

from datetime import datetime, timezone
from typing import Optional

# build_event_conf sets Delay 60 + Jitter 10; a round older than this many rounds means a frozen scoreboard.
ROUND_DELAY_SECONDS = 60
FRESHNESS_ROUNDS = 5

PAUSED = "paused"          # intentionally paused; no stale reboot signature
ADVANCING = "advancing"
PENDING = "pending"
STALE = "stale"            # not cycling and past the window: the loop is STOPPED
UNKNOWN = "unknown"


def parse_rfc3339(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _naive_utc(when: Optional[datetime]) -> Optional[datetime]:
    if when is None:
        return None
    return when.replace(tzinfo=None) if when.tzinfo else when


def round_loop_state(engine_payload, now=None, freshness_seconds=None) -> dict:
    if not isinstance(engine_payload, dict):
        return {"state": UNKNOWN, "age_seconds": None, "started": None}
    # `/api/engine` always reports `running`: its absence means we did not get that document.
    if "running" not in engine_payload:
        return {"state": UNKNOWN, "age_seconds": None, "started": None}

    window = FRESHNESS_ROUNDS * ROUND_DELAY_SECONDS if freshness_seconds is None \
        else freshness_seconds
    now = _naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)

    current = parse_rfc3339(engine_payload.get("current_round_time"))
    zero_current = current is not None and current.year <= 1
    if zero_current:
        current = None  # the Go zero time — the loop is not cycling
    started = parse_rfc3339((engine_payload.get("last_round") or {}).get("StartTime"))

    age_seconds = None if started is None else (now - _naive_utc(started)).total_seconds()
    stale = age_seconds is not None and age_seconds >= window

    if engine_payload.get("running") is False:
        state = STALE if zero_current and stale else PAUSED
        return {"state": state, "age_seconds": age_seconds if state == STALE else None,
                "started": started if state == STALE else None}
    if current is not None:
        return {"state": ADVANCING, "age_seconds": None, "started": started}
    if started is None:
        return {"state": ADVANCING, "age_seconds": None, "started": None}

    state = STALE if stale else PENDING
    return {"state": state, "age_seconds": age_seconds, "started": started}


def is_stale(engine_payload, now=None, freshness_seconds=None) -> bool:
    return round_loop_state(engine_payload, now=now,
                            freshness_seconds=freshness_seconds)["state"] == STALE
