#!/usr/bin/env python3
"""Event-day schedule driver for a packet-compiled competition.

The engine has no schedule model — pausing IS the schedule. This reads a packet
profile's `schedule:` windows, prints the wall-clock plan from T0, and (with
--execute) drives the engine through start / lunch-freeze / resume / end:

  python3 run-schedule.py packets/cde-2026/packet.yaml --t0 "2026-10-03 09:30"
  python3 run-schedule.py packets/cde-2026/packet.yaml --execute freeze
  python3 run-schedule.py packets/cde-2026/packet.yaml --execute end

`end` also dumps the final scoreboard + inject state into <comp>/evidence/ BEFORE
the pause kills the loop's last round (the stage_capture lesson: capture first)."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

from common.comp_paths import resolve_comp_dir
from quotient.event_capture import end_event

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent
ACTIONS = ("start", "freeze", "resume", "end")


def load_profile(profile_arg):
    from config.packet_ops import PACKETS_DIR, load_profile
    path = Path(profile_arg)
    if not path.exists():
        candidate = PACKETS_DIR / profile_arg / "packet.yaml"
        if not candidate.exists():
            sys.exit(f"ERROR: no profile at {path} (or {candidate})")
        path = candidate
    return load_profile(path)


def engine_ctx(args, comp_dir):
    ip = args.engine_ip
    if not ip:
        tf_dir = comp_dir / "terraform"
        if not (tf_dir / "terraform.tfstate").exists():
            sys.exit(f"ERROR: {tf_dir}/terraform.tfstate not found — pass --engine-ip")
        raw = subprocess.run(["terraform", "output", "-json"], cwd=str(tf_dir),
                             capture_output=True, text=True, check=True).stdout
        ip = json.loads(json.loads(raw)["agent_context"]["value"])["scoring_engine_ip"]
    password = args.admin_password
    if not password:
        creds = comp_dir / "credentials.txt"
        if creds.exists():
            for line in creds.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "admin":
                    password = parts[1]
                    break
    if not password:
        sys.exit("ERROR: no admin password (credentials.txt missing or empty) — "
                 "pass --admin-password")
    return f"http://{ip}", password


def print_plan(schedule, t0):
    print(f"\n  Schedule plan (T0 = {t0:%Y-%m-%d %H:%M %Z})")
    print("  " + "-" * 56)
    for w in schedule:
        at = w.get("at_min")
        if at is None:
            print(f"    {'—':>14}  {w.get('label', '?')}: {w.get('note', '')}")
            continue
        when = t0 + timedelta(minutes=int(at))
        print(f"    {when:%a %H:%M}  {w.get('label', '?')} (T0+{at}m): {w.get('note', '')}")
    print()


def main():
    # Every file this driver writes (the evidence dump and anything the engine layer
    # creates) is private from the start rather than depending on the operator's umask.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile", help="packets/<event>/packet.yaml (or event name)")
    parser.add_argument("--competition", default=None,
                        help="competitions/<id> (default: profile event.comp_id)")
    parser.add_argument("--engine-ip", default=None, help="override scoring-engine IP")
    parser.add_argument("--admin-password", default=None)
    parser.add_argument("--t0", default=None, dest="t0",
                        help="competition start, local time 'YYYY-MM-DD HH:MM' (default: now)")
    parser.add_argument("--execute", choices=ACTIONS, default=None,
                        help="drive the engine (default: print the plan only)")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    profile = load_profile(args.profile)
    comp_id = (profile.get("event") or {}).get("comp_id")
    comp_dir = resolve_comp_dir(args.competition or comp_id)
    if not comp_dir.is_dir():
        sys.exit(f"ERROR: competition dir not found: {comp_dir}")

    t0 = (datetime.strptime(args.t0, "%Y-%m-%d %H:%M") if args.t0
          else datetime.now().replace(second=0, microsecond=0))
    print_plan(profile.get("schedule") or [], t0)

    if not args.execute:
        return 0

    base_url, password = engine_ctx(args, comp_dir)
    session = requests.Session()
    r = session.post(f"{base_url}/api/login",
                     json={"username": "admin", "password": password}, timeout=10)
    r.raise_for_status()
    action = args.execute
    if action == "start":
        r1 = session.post(f"{base_url}/api/competition/start",
                          json={"started": True}, timeout=10)
        r2 = session.post(f"{base_url}/api/engine/pause",
                          json={"pause": False}, timeout=10)
        print(f"  START: competition/start={r1.status_code} engine/pause(unpause)={r2.status_code}")
        if r1.status_code >= 400 or r2.status_code >= 400:
            return 1
    elif action in ("freeze", "resume"):
        r = session.post(f"{base_url}/api/engine/pause",
                         json={"pause": action == "freeze"}, timeout=10)
        print(f"  {action.upper()}: engine/pause={r.status_code}")
        if r.status_code >= 400:
            return 1
    elif action == "end":
        # Capture BEFORE the pause: the round loop stopping is what freezes the
        # scoreboard's last round, and a paused engine answers the same APIs — but
        # capture first, exactly like stage_capture. end_event() cannot raise out of
        # main(): the pause below is the whole point of `end` and must always run.
        return end_event(base_url, session, comp_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
