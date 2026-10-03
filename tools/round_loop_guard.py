#!/usr/bin/env python3
"""Restart Quotient's scoring round loop when an engine reboot has stopped it.

Runs ON the engine, from a systemd timer, and authenticates as the dedicated `scoring`
account — never `admin`. That account exists precisely so this actor cannot evict the
operator's, `verify-competition`'s, or the scrim harness's session: Quotient allows ONE
session per account, so a scheduled login on `admin` would kill their cookie every time
it checked (see docs/known-issues.md "Scoring round loop does not auto-resume").

The decision is `round_loop.round_loop_state` — the same function verify's gate uses, so
the two cannot disagree about what "stopped" means. This file is only the actor: log in,
ask, and if the loop is stale, issue the two POSTs verify documents.

Stdlib only: the engine image is not guaranteed to carry `requests`.
"""

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Pushed next to this file by engine_ops.install_round_loop_guard().
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import round_loop
except ImportError:  # pragma: no cover - only on a broken install
    round_loop = None

DEFAULT_CONFIG = "/opt/quotient/round-loop-guard.json"
LOG_TAG = "round-loop-guard"


def log(message):
    print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {LOG_TAG}: {message}",
          flush=True)


class EngineClient:
    """Minimal cookie-session JSON client for the engine's own API."""

    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _json(self, request):
        with self.opener.open(request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}

    def get_json(self, path):
        return self._json(urllib.request.Request(f"{self.base_url}{path}"))

    def post_json(self, path, payload):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        return self._json(request)

    def login(self, username, password):
        return self.post_json("/api/login", {"username": username, "password": password})


def guard_once(client, username, password, now=None, dry_run=False):
    """One check-and-maybe-fix cycle. Returns a dict describing what happened.

    Never raises for an ordinary engine hiccup: a timer that dies loudly every 60s is
    worse than one that reports and waits. The caller decides what to do with the
    outcome; the exit code stays 0 unless something is genuinely wrong.
    """
    outcome = {"state": None, "action": "none", "detail": ""}
    try:
        client.login(username, password)
        payload = client.get_json("/api/engine")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        outcome.update(state="unknown", action="none", detail=f"engine unreachable: {exc}")
        return outcome

    verdict = round_loop.round_loop_state(payload, now=now)
    outcome["state"] = verdict["state"]
    if verdict["state"] != round_loop.STALE:
        outcome["detail"] = f"loop {verdict['state']} — nothing to do"
        return outcome

    age_min = (verdict["age_seconds"] or 0) / 60
    outcome["detail"] = f"loop stopped; last round {age_min:.0f} min ago"
    if dry_run:
        outcome["action"] = "would-fix"
        return outcome

    # The same two POSTs verify's --fix-round-loop issues, in the same order.
    try:
        start = client.post_json("/api/competition/start", {"started": True})
        unpause = client.post_json("/api/engine/pause", {"pause": False})
    except (urllib.error.URLError, OSError, ValueError) as exc:
        outcome["action"] = "failed"
        outcome["detail"] += f"; fix POST failed: {exc}"
        return outcome
    outcome["action"] = "fixed"
    outcome["detail"] += f"; start={start} unpause={unpause}"
    return outcome


def _load_config(path):
    with open(path) as handle:
        config = json.load(handle)
    for key in ("base_url", "username", "password"):
        if not config.get(key):
            raise SystemExit(f"{LOG_TAG}: {path} is missing {key!r}")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"JSON with base_url/username/password (default {DEFAULT_CONFIG})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without issuing the fix")
    parser.add_argument("--quiet", action="store_true",
                        help="log only when the loop is stale or something failed")
    args = parser.parse_args(argv)

    if round_loop is None:
        log("FAIL: round_loop.py is not next to this script — reinstall the guard")
        return 1
    config = _load_config(args.config)
    client = EngineClient(config["base_url"])
    outcome = guard_once(client, config["username"], config["password"], dry_run=args.dry_run)

    interesting = outcome["state"] == round_loop.STALE or outcome["action"] in ("failed",)
    if interesting or not args.quiet:
        log(f"{outcome['state']}: {outcome['detail']} ({outcome['action']})")
    return 1 if outcome["action"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
