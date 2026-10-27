#!/usr/bin/env python3
"""Event-day schedule driver for a packet-compiled competition."""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from common.comp_paths import resolve_comp_dir

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent
# The engine has no schedule model; pausing IS the schedule.
ACTIONS = ("start", "freeze", "resume", "end")


def load_profile(profile_arg):
    from config.packet_ops import load_profile, packet_yaml_for_event, resolve_packet_source
    resolved = resolve_packet_source(profile_arg)
    if resolved is None or resolved.is_dir():
        resolved = packet_yaml_for_event(profile_arg)
    if not Path(resolved).exists():
        sys.exit(f"ERROR: no packet profile at {profile_arg} "
                 "(path, comp-dir/comps-root relative, or an event name)")
    return load_profile(resolved)


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
    # Evidence and engine-layer writes must not depend on the operator's umask.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile", help="path to a packet.yaml (comp-dir/comps-root "
                        "relative, e.g. cde-2026/packet.yaml) or a bare event name")
    parser.add_argument("--competition", default=None,
                        help="competitions/<id> (default: profile event.comp_id)")
    parser.add_argument("--engine-ip", default=None, help="override scoring-engine IP")
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
    # Delegate to the one lifecycle driver: it logs in as `scoring` (so the operator's admin
    # cookie survives), treats "already in that state" as success, creates the packet's
    # injects with the event clock anchored at now, and records the run's flags in state.
    # This file only owns the schedule PLAN.
    from quotient.lifecycle_ops import cmd_end, cmd_freeze, cmd_start

    ns = argparse.Namespace(engine_ip=args.engine_ip, scoring_password=None)
    if args.execute == "start":
        return cmd_start(ns, comp_dir)
    if args.execute in ("freeze", "resume"):
        return cmd_freeze(ns, comp_dir, pause=args.execute == "freeze")
    return cmd_end(ns, comp_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
