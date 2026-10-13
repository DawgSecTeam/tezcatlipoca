"""Quotient's scoring round loop: is it cycling, and how do we tell?

Extracted so there is ONE definition of "the loop is stopped" (2026-10-02). It lived
only inside `verify-competition.py`'s gate, and an engine-side watchdog would have had
to re-derive it — two implementations of the same judgement, drifting apart, which is
exactly the failure mode this repo keeps paying for.

The signature of a stopped loop (live-confirmed 2026-09-29, pfsense-ad):
`GET /api/engine` reports the Go zero time in `current_round_time`
(`0001-01-01T00:00:00Z` means "not cycling") and a `last_round.StartTime` older than
freshness window. After an engine reboot, `running` may be false as well as the loop
stopping; that paused state is healable only with this old-round signature. An intentional
pause with a real `current_round_time` remains paused and is never restarted.

The containers came back after the reboot; the loop did not.

Nothing here talks to the network.
"""

from datetime import datetime, timezone
from typing import Optional

# A scored round older than FRESHNESS_ROUNDS x DELAY means the scoreboard is frozen and
# an old UP is not a live UP. Delay 60 + Jitter 10 is what build_event_conf sets.
ROUND_DELAY_SECONDS = 60
FRESHNESS_ROUNDS = 5

# The states a caller has to act on, in the order the gate checks them.
PAUSED = "paused"          # engine intentionally paused; no stale reboot signature
ADVANCING = "advancing"    # current_round_time is real: the loop is cycling
PENDING = "pending"        # not cycling yet, but within the freshness window
STALE = "stale"            # not cycling and past the window: the loop is STOPPED
UNKNOWN = "unknown"        # /api/engine did not answer usably


def parse_rfc3339(value) -> Optional[datetime]:
# Parse an RFC3339 timestamp; None when absent or unparseable.
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
# Classify `/api/engine` into one of the states above.
#
#     Returns {"state": ..., "age_seconds": float|None, "started": datetime|None}.
#     `age_seconds` is the age of the last round (None when there is nothing to age), so a
#     caller can print it without re-deriving.
    if not isinstance(engine_payload, dict):
        return {"state": UNKNOWN, "age_seconds": None, "started": None}
    # `/api/engine` always reports `running`; its absence means we did not get the
    # document we expect, which is not the same as "no round recorded yet".
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
# The single question an unattended actor asks: should I restart the loop?
    return round_loop_state(engine_payload, now=now,
                            freshness_seconds=freshness_seconds)["state"] == STALE
