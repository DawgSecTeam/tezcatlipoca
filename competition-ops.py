#!/usr/bin/env python3
"""Competition lifecycle driver: start / freeze / resume / end / status.

Deploy and start are separate steps: `create-competition.py --hold-start` finishes with
everything deployed, teams seeded, the portal up — but the competition clock unstarted
and the engine paused. This drives the engine afterwards:

  python3 competition-ops.py start  --comp-dir competitions/<id>
  python3 competition-ops.py freeze --comp-dir competitions/<id> --engine-ip 10.0.0.150
  python3 competition-ops.py end    --comp-dir competitions/<id>

Auth is always the `scoring` automation account (password from the estate state or
credentials.txt): Quotient allows ONE session per account, and a lifecycle login on
`admin` would evict the operator's browser session.

`start` also creates the deferred injects — their open/due/close windows anchor at
creation time, so creating them at deploy time would open them before T0.

`end` captures the final scoreboard into <comp-dir>/evidence/ BEFORE pausing: the
pause is what freezes the loop's last round, and a paused engine still answers these
APIs — capture first (stage_capture lesson). It is terminal: resuming afterwards is a
deliberate reset-event, never an accident.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import urllib3
from dotenv import load_dotenv

from config.injects_config import injects_fingerprint, load_injects, resolve_inject_times
from estate import state as estate_state
from estate.engine_setup import engine_ctx
from quotient.event_capture import end_event, login_scoring
from quotient.setup import create_injects

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent
ACTIONS = ("start", "freeze", "resume", "end", "status")


def load_state(comp_dir):
    return estate_state.load_flat(comp_dir)


def engine_base(args, comp_dir):
    ip = args.engine_ip
    if not ip:
        ip = engine_ctx(comp_dir)["scoring_engine_ip"]
        if not ip:
            sys.exit(f"ERROR: no engine management IP recorded in "
                     f"{estate_state.state_path(comp_dir)} — pass --engine-ip")
    host = ip if ip.startswith("http") else f"http://{ip}"
    return host.rstrip("/")


def scoring_password(args, state, comp_dir):
    pw = args.scoring_password or state.get("scoring_password")
    if pw:
        return pw
    creds = Path(comp_dir) / "credentials.txt"
    if creds.exists():
        for line in creds.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "scoring":
                return parts[1]
    sys.exit("ERROR: no scoring password (estate state / credentials.txt empty) — "
             "pass --scoring-password")


def login(base_url, password):
    try:
        return login_scoring(base_url, password)
    except RuntimeError as e:
        sys.exit(f"ERROR: {e} (the password lives in the estate state / credentials.txt; "
                 "override with --scoring-password)")


def engine_status(session, base_url):
    r = session.get(f"{base_url}/api/engine", timeout=10)
    r.raise_for_status()
    return r.json()


def cmd_start(args, comp_dir):
    state = load_state(comp_dir)
    base_url = engine_base(args, comp_dir)
    session = login(base_url, scoring_password(args, state, comp_dir))
    eng = engine_status(session, base_url)
    rc = 0

    if eng.get("competition_started"):
        print("  Competition clock already started.")
    else:
        r = session.post(f"{base_url}/api/competition/start", json={"started": True}, timeout=10)
        r.raise_for_status()
        print(f"  START: competition/start={r.status_code} (the clock is running)")

    if eng.get("running"):
        print("  Engine already scoring.")
    else:
        r = session.post(f"{base_url}/api/engine/pause", json={"pause": False}, timeout=10)
        if r.status_code >= 400 and engine_status(session, base_url).get("running"):
            # The engine unpaused between the status GET and this POST — it 400'd on the
            # stale transition; the re-GET above proves the state we wanted.
            print("  Engine unpaused concurrently — treating as done.")
        else:
            r.raise_for_status()
            print(f"  START: engine unpause={r.status_code} (rounds are scoring)")

    injects = load_injects(comp_dir)
    if injects:
        want = injects_fingerprint(injects)
        if state.get("injects_created") and state.get("injects_fingerprint") == want:
            print("  Injects already created (fingerprint match) — skipping.")
        else:
            print(f"  Creating {len(injects)} inject(s), windows anchored at now (T0)...")
            resolve_inject_times(injects)
            _created, failed = create_injects(base_url, state.get("scoring_password"),
                                              injects, username="scoring")
            if failed:
                print(f"  WARNING: {len(failed)} inject(s) failed to create: "
                      f"{', '.join(failed)} — re-run start to retry (existing injects are deduped)")
                rc = 1
            else:
                state["injects_created"] = True
                state["injects_fingerprint"] = want

    state["started"] = True
    state["engine_unpaused"] = True
    document = estate_state.EstateState.load(comp_dir)
    document.absorb(state)
    document.save()
    print(f"  Competition live at {base_url} — admin pages: /admin, /announcements, /injects")
    return rc


def cmd_freeze(args, comp_dir, pause):
    state = load_state(comp_dir)
    base_url = engine_base(args, comp_dir)
    session = login(base_url, scoring_password(args, state, comp_dir))
    r = session.post(f"{base_url}/api/engine/pause", json={"pause": pause}, timeout=10)
    if r.status_code >= 400:
        # 400 on a pause toggle is the engine refusing a stale transition; ask it
        # whether the state we asked for already holds before calling it a failure.
        running = bool(engine_status(session, base_url).get("running"))
        if running == (not pause):
            print(f"  Engine already {'frozen' if pause else 'scoring'} — nothing to do.")
            return 0
        r.raise_for_status()
    print(f"  {'FREEZE' if pause else 'RESUME'}: engine/pause={r.status_code} — "
          f"rounds {'frozen' if pause else 'scoring again'}")
    return 0


def cmd_end(args, comp_dir):
    state = load_state(comp_dir)
    base_url = engine_base(args, comp_dir)
    session = login(base_url, scoring_password(args, state, comp_dir))
    rc = end_event(base_url, session, comp_dir)
    if rc == 0:
        # Only a clean capture AND pause may record the event as over; a failed one
        # leaves the panel showing a still-live engine (run rc tells the operator).
        state["ended"] = True
        document = estate_state.EstateState.load(comp_dir)
        document.absorb(state)
        document.save()
        print("  The competition is over — scoring is frozen at the last round. "
              "The range stays up until teardown.")
    return rc


def cmd_status(args, comp_dir):
    state = load_state(comp_dir)
    base_url = engine_base(args, comp_dir)
    out = {"base_url": base_url,
           "hold_start": bool(state.get("hold_start")),
           "seeded": bool(state.get("seeded")),
           "ended": bool(state.get("ended")),
           "reachable": False}
    try:
        session = login(base_url, scoring_password(args, state, comp_dir))
        out.update(engine_status(session, base_url))
        out["reachable"] = True
    except SystemExit as e:
        out["error"] = str(e)
        print(json.dumps(out, indent=2))
        return 1
    except requests.RequestException as e:
        out["error"] = f"{type(e).__name__}: {e}"
        print(json.dumps(out, indent=2))
        return 1
    print(json.dumps(out, indent=2))
    return 0


def main():
    # The state write below updates a 0600 file; keep everything it touches private
    # from the start rather than depending on the operator's umask.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--comp-dir", dest="comp_dir", required=True,
                        help="the competition dir (or a per-run workspace copy) holding "
                             "estate-state.json, credentials.txt and injects/")
    parser.add_argument("--engine-ip", default=None,
                        help="override the scoring-engine IP (default: the state document)")
    parser.add_argument("--scoring-password", default=None, dest="scoring_password",
                        help="override the scoring account password")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    comp_dir = Path(args.comp_dir).resolve()
    if not comp_dir.is_dir():
        sys.exit(f"ERROR: competition dir not found: {comp_dir}")

    if args.action == "start":
        return cmd_start(args, comp_dir)
    if args.action == "freeze":
        return cmd_freeze(args, comp_dir, pause=True)
    if args.action == "resume":
        return cmd_freeze(args, comp_dir, pause=False)
    if args.action == "end":
        return cmd_end(args, comp_dir)
    return cmd_status(args, comp_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
