"""Competition lifecycle: start / freeze / resume / end / status, over the Quotient API.

`competition-ops.py` is the CLI over this; `run-schedule.py` calls the same functions, so the
two drivers cannot disagree about the account they use, what counts as "already in that
state", or whether injects are created.
"""

import json
import sys
from pathlib import Path

import requests

from config.injects_config import injects_fingerprint, load_injects, resolve_inject_times
from estate import state as estate_state
from estate.engine_setup import engine_ctx
from quotient.event_capture import end_event, login_scoring
from quotient.setup import create_injects


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


# a --hold-start deploy leaves tlaloc enabled-but-stopped (a held range must sleep —
# run-14ac7f14's held deploy had red stripping freshly-provisioned forwarders hours
# before T0); releasing the hold is the moment red begins. Returns 1 when red did NOT
# come up, so `start` tells the operator to intervene rather than open red-less.
def release_held_red(state):
    if not state.get("red_loop_held"):
        return 0
    try:
        from estate.config import ssh_key_path
        from red import tlaloc_ops
        red_ip = (state.get("red") or {}).get("red_ip")
        if not red_ip:
            print("  START WARNING: red loop was held but red01's ip is not in state — "
                  "start it yourself: sudo systemctl start tlaloc.service on red01")
            return 1
        tlaloc_ops.start_director(str(ssh_key_path()), red_ip)
        state["red_loop_held"] = False
        print(f"  START: tlaloc loop released on red01 ({red_ip}) — red is attacking")
        return 0
    except Exception as e:                                   # noqa: BLE001
        print(f"  START WARNING: releasing the red loop failed ({e}) — run "
              "`sudo systemctl start tlaloc.service` on red01 before play begins")
        return 1


def cmd_start(args, comp_dir):
    state = load_state(comp_dir)
    base_url = engine_base(args, comp_dir)
    password = scoring_password(args, state, comp_dir)
    session = login(base_url, password)
    eng = engine_status(session, base_url)
    rc = 0

    # Implants deploy with the event: the deploy left the range prepared but not armed, so
    # nothing is compromised and no beacon exists until here. Before the clock, so T0 opens
    # on a range red already owns.
    if state.get("red_staged") and not state.get("red_deployed"):
        from estate.config import ssh_key_path
        from red import tlaloc_ops
        print("  START: deploying the staged implants onto the range...")
        summary = tlaloc_ops.deploy_implants(comp_dir, state,
                                             ssh_key=str(ssh_key_path()))
        if summary:
            state["red_deployed"] = True
            state.pop("red_staged", None)
            print(f"  START: implants deployed on {summary['red_ip']} — red is in the range")

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
            # Engine 400s a stale transition — it unpaused between GET and POST; the re-GET proves the wanted state.
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
            _created, failed = create_injects(base_url, password, injects,
                                              username="scoring")
            if failed:
                print(f"  WARNING: {len(failed)} inject(s) failed to create: "
                      f"{', '.join(failed)} — re-run start to retry (existing injects are deduped)")
                rc = 1
            else:
                state["injects_created"] = True
                state["injects_fingerprint"] = want

    held_red_rc = release_held_red(state)
    rc = rc or held_red_rc

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
        # 400 on a toggle = the engine refusing a stale transition; confirm the state before failing.
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
    # end_event captures the scoreboard BEFORE pausing — a paused engine still answers the capture APIs.
    rc = end_event(base_url, session, comp_dir)
    if rc == 0:
        # Never record `ended` on a failed capture — the panel must still show a live engine.
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


