"""The c3rberus OPERATOR — an agented red player driving the platform over its API.

The realistic-competition piece: tlaloc keeps its autonomous campaign, and this
operator behaves like the human red team that runs C3rberus at a real CDE — reads
its scoreboard, sees what blue remediated, re-plants, escalates. It runs on the
driver host exactly like a blue agent (opencode, per-cycle workdir) and talks to
red01 ONLY through the platform's HTTP API: the same surface a real operator has.
"""

import json
import re
import threading
import time
from pathlib import Path

from scrim import blue_agent, core
from scrim import endpoints
from scrim.core import log

ACCESS = """# C3rberus platform access (rendered by the harness — never commit or share)
API={api}
# sign in and keep the bearer token:
./c3r login            # once per cycle is enough; it caches .token next to this file
./c3r GET /scoreboard                       # teams, per-IOC remediation state
./c3r GET /admin/status                     # scheduler + component health
./c3r GET /c2/iocs/deployed?box_ip=<ip>     # what is live on one box (and what blue fixed)
./c3r POST /admin/deploy-all-iocs '{{"skip_active":true,"confirm":true}}'   # plant everything missing
./c3r POST /c2/iocs/deploy '{{"ioc_name":"<name>","box_ips":["<ip>"],"deployed_by":"operator"}}'
./c3r GET /c2/tasks/<task_id>               # poll a 202 job to completion
Sources of truth to READ, never run: iocs/definitions/*.py (what each IOC does and
its check), wiki/ (platform docs). The boxes are reached by the PLATFORM (NetExec
from red01) — you have no SSH and need none.
"""

CYCLE = """You are the RED TEAM OPERATOR running C3rberus, the red scoring platform, on the
live range (CDE replica, {teams} blue teams). Event minute {elapsed} of {remain} left.
Sign in with ./c3r login, then run an attack cycle:
1. `./c3r GET /scoreboard` and `./c3r GET /c2/iocs/deployed` — read the board: which
   planted IOCs blue has REMEDIATED is the whole point; blue's restorations are your gaps.
2. Choose your response: re-deploy the fixed ones by name (`/c2/iocs/deploy` or
   `deploy-and-register`; add "confirm":true on bulk calls while RUNNING), aim new
   IOCs at the boxes blue is neglecting, and if you hold sessions/beacons use them
   (`./c3r GET /c2/sessions`, `/c2/beacons`) for reach NetExec lacks.
3. Prefer stealth over volume: at most a handful of actions per cycle; destroying the
   scored services is the campaign engine's job, yours is the IOC war of persistence.
4. If cred-preflight-style failures show in task results (an IOC will not land), note
   the box and move on — do not brute force.
Everything you do goes to LOG.md (append, timestamped, one line per action with the
task ids); keep NOTEBOOK.md current with what blue seems to be doing and your plan.
Your REPORT.md is red's after-action narrative for the platform side of this event —
update it whenever you learn something an AAR author would want.
Your last lines:
{tail}
Your notebook:
{notebook}
"""


def _wd(args):
    return Path(args.run_dir) / "red-c3rberus"


def stage_operator(args, comp, run_dir):
    """Build the operator workdir from the seed document stage() left. Returns the
    summary dict (event_run spawns the feed on it) or None when the platform was
    not part of this deploy."""
    from red import c3rberus_ops
    if not c3rberus_ops.selected(comp):
        return None
    seed = Path(comp) / "red" / c3rberus_ops.SEED_FILE
    if not seed.exists():
        log("c3rberus selected but no seed document — red runs platform-less (stage failed?)")
        return None
    doc = json.loads(seed.read_text())
    wd = Path(run_dir) / "red-c3rberus"
    wd.mkdir(parents=True, exist_ok=True)
    if not (wd / "c3rberus").exists():
        import subprocess
        r = subprocess.run(["git", "clone", "--quiet", "--depth", "1",
                            c3rberus_ops.repo_url(comp), str(wd / "c3rberus")],
                           capture_output=True, timeout=300, check=False)
        if r.returncode != 0:
            log("WARNING: c3rberus repo clone failed — operator works from the API alone")
    api = f"https://{doc['red_ip']}:3000"
    (wd / "c3rberus.env").write_text(
        f"C3R_API={api}\nC3R_ADMIN_PW={doc['admin_password']}\n")
    (wd / "c3rberus.env").chmod(0o600)
    (wd / "c3r").write_text(C3R)
    (wd / "c3r").chmod(0o755)
    (wd / "ACCESS.md").write_text(ACCESS.format(api=api))
    if not (wd / "LOG.md").exists():
        (wd / "LOG.md").write_text("# Red — C3rberus operator log\n")
    if not (wd / "NOTEBOOK.md").exists():
        (wd / "NOTEBOOK.md").write_text("# Red operator notebook\n")
    if not (wd / "REPORT.md").exists():
        (wd / "REPORT.md").write_text(
            "# Red team — C3rberus platform after-action\n"
            "(operator agent narrative; the campaign engine files its own report)\n")
    key_env = endpoints.key_env_name(args.blue_base_url) or "OPENROUTER_API_KEY"
    (wd / "opencode.jsonc").write_text(
        OPENCODE_CFG.replace("{PROVIDER_KEY}", endpoints.provider_key(args.blue_base_url))
        .replace("{BASE_URL}", args.blue_base_url)
        .replace("{API_KEY_FIELD}", "{env:%s}" % key_env)
        .replace("{MODEL_ID}", args.red_model))
    log(f"red operator staged ({args.red_model} @ {args.blue_base_url} → {api})")
    return doc


C3R = """#!/usr/bin/env bash
# c3r — one-token curl against the C3rberus API. ./c3r GET /scoreboard
set -euo pipefail
cd "$(dirname "$0")"
. ./c3rberus.env
M=${1:?usage: c3r METHOD PATH [json]}; P=${2:?path}; B=${3:-}
if [ ! -s .token ] || ! curl -sk -m 8 -H "Authorization: Bearer $(cat .token)" \
     "$C3R_API/me" >/dev/null 2>&1; then
  curl -sk -m 15 -X POST "$C3R_API/login" -H 'Content-Type: application/json' \\
    -d "{\\"username\\":\\"admin\\",\\"password\\":\\"$C3R_ADMIN_PW\\"}" \\
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])' > .token
fi
AUTH="Authorization: Bearer $(cat .token)"
if [ -n "$B" ]; then
  curl -sk -m 60 -X "$M" -H "$AUTH" -H 'Content-Type: application/json' -d "$B" "$C3R_API$P"
else
  curl -sk -m 60 -X "$M" -H "$AUTH" "$C3R_API$P"
fi
echo
"""

OPENCODE_CFG = """{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "{PROVIDER_KEY}": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "tezcatlipoca club gateway",
      "options": { "baseURL": "{BASE_URL}", "apiKey": "{API_KEY_FIELD}" },
      "models": { "{MODEL_ID}": { "limit": { "context": 120000, "output": 8000 } } }
    }
  },
  "permission": "allow"
}
"""


def red_feed_loop(args, stop, t0, first_delay=240.0):
    """The operator's cycle cadence: same clock discipline as blue feeds (the platform's
    scheduler covers dwell; cycles are decisions, not polling)."""
    import os
    import socket
    wd = _wd(args)
    if first_delay:
        stop.wait(first_delay)
    if stop.is_set():
        return
    base_url = args.blue_base_url
    lock = threading.Lock()          # red runs its own lane on the shared gateway
    while not stop.is_set() and (time.time() - t0) < (args.duration_min - 2) * 60:
        started = time.time()
        elapsed = int((time.time() - t0) / 60)
        remain = args.duration_min - elapsed
        home = wd / ".opencode-home"
        for sub in ("data", "config", "cache"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        svc_cfg = home / "config" / "opencode"
        svc_cfg.mkdir(parents=True, exist_ok=True)
        with socket.socket() as sock:                    # fresh port, as blues do
            sock.bind(("127.0.0.1", 0))
            svc_cfg.joinpath("service.json").write_text(
                json.dumps({"port": sock.getsockname()[1]}))
        _KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL",
                 "TMPDIR", "USER", "LOGNAME")
        env = {k: os.environ[k] for k in _KEEP if k in os.environ}
        env.update({"HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
                    "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_CACHE_HOME": str(home / "cache"),
                    endpoints.key_env_name(base_url) or "OPENROUTER_API_KEY":
                        endpoints.api_key(base_url)})
        try:
            tail = "\n".join((wd / "LOG.md").read_text().splitlines()[-8:]) or "(empty)"
            notebook = (wd / "NOTEBOOK.md").read_text()[:2500]
        except OSError:
            tail, notebook = "(no log yet)", "(no notebook yet)"
        prompt = CYCLE.format(elapsed=elapsed, remain=remain, tail=tail,
                              notebook=notebook, teams=getattr(args, "teams", 2))
        cycles = wd / "cycles"
        cycles.mkdir(exist_ok=True)
        (cycles / f"cycle-T+{elapsed:03d}.prompt.txt").write_text(prompt)
        r = blue_agent.run_cycle_with_retry(args, prompt, wd, env, lock, stop,
                                            team="c3r", ep=(base_url, args.red_model))
        out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
        (cycles / f"cycle-T+{elapsed:03d}.output.log").write_text(out[-8000:])
        with (wd / "feed.log").open("a") as f:
            f.write(f"\n===== cycle T+{elapsed} rc={r.returncode} =====\n{out[-2000:]}\n")
        log(f"red-c3rberus cycle T+{elapsed} rc={r.returncode}")
        took = time.time() - started
        # slower than blue's 8-min heartbeat: the platform's own 5-min scheduler covers
        # dwell, and an operator cycle is a handful of API calls, not box-by-box triage
        stop.wait(min(900, max(120, int(core.CYCLE_TARGET_PERIOD * 1.5) - took)))
    stop.wait()
