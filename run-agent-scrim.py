#!/usr/bin/env python3
"""run-agent-scrim.py — deploy, verify, and run an agent-manned scrim end to end.

Whole-lifecycle orchestrator for a tezcatlipoca red-vs-blue practice competition run
by LLM agents: author (optional) -> deploy -> verify + fire test -> blue agents
(opencode, fresh session per cycle) -> red agent (bad-auto) -> scheduled feeds and
monitoring until the deadline -> evidence capture -> teardown (red01 + range).

Born from the agent-scrim-2026-09-16 debrief; bake-in lessons:
  - fresh opencode session per cycle with a compact prompt (survives small-context
    local models, works even better on cloud models)
  - exact SSH/inject-submit commands EMBEDDED in every cycle prompt (blue agents
    failed to re-derive them from the briefing)
  - helper scripts (mybox/myscore/submit-inject) in each blue workdir
  - blue sessions launched SEQUENTIALLY (concurrent opencode boots race its DB)
  - red via OpenRouter direct from red01 (internet) — reverse tunnels only needed
    for tailnet-local endpoints
  - teardown of the whole range at the end unless --keep-range

Usage:
  python3 run-agent-scrim.py --competition agent-scrim-2026-09-16 \
      [--new NAME --from-template DIR] [--teams 2] [--duration-min 90]
      [--blue-model openai/gpt-5.6-luna] [--red-model openai/gpt-5.6-luna]
      [--reasoning-effort minimal] [--llm-base-url https://openrouter.ai/api/v1]
      [--blue-base-url http://100.64.0.9:8080/v1]
      [--skip-deploy] [--keep-range]
      [--resume-from deploy|blues|red|run|teardown]

Requires: .env (TF_VAR_*), vendor/nakon v0.1.5+, bad-auto checkout next to this
repo, opencode on PATH, and BAuto_LLM_API_KEY (or OPENROUTER_API_KEY) in
bad-auto/.env or the environment.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from utils import load_users_config

REPO = Path(__file__).resolve().parent
BAD_AUTO = REPO.parent / "bad-auto"
DEFAULT_TEMPLATE = REPO / "competitions" / "agent-scrim-2026-09-16"
RUNTIME_FILES = {  # regenerated per deploy; never copied into a fresh competition
    "teams.json", ".deploy_state.json", "credentials.txt", "nakon-config.json",
    "cloned_vms.json", "packet.md", "event.conf",
}

MYBOX = """#!/usr/bin/env bash
# mybox <box> "<command>" — run a command on one of your boxes (linux: key auth; windows: password).
set -euo pipefail
cd "$(dirname "$0")"
BOX=${1:?box: dc01|win01|web01|app01|db01}; shift
source ./scrim.env
case "$BOX" in
  dc01|win01) OCT=2; [ "$BOX" = win01 ] && OCT=3
    exec sshpass -p "$BOX_PW" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
      -o "ProxyCommand=ssh -i $KEY_PATH -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p $VM_USER@$ENGINE_IP" \\
      "Administrator@192.168.$MY_TID.$OCT" "$@" ;;
  web01|app01|db01) case "$BOX" in web01) OCT=4;; app01) OCT=5;; db01) OCT=6;; esac
    exec ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
      -o "ProxyCommand=ssh -i $KEY_PATH -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -W %h:%p $VM_USER@$ENGINE_IP" \\
      "$BOX_USER@192.168.$MY_TID.$OCT" "$@" ;;
  *) echo "unknown box $BOX" >&2; exit 2 ;;
esac
"""

QLOGIN = """#!/usr/bin/env bash
# qlogin — refresh the shared team cookie jar ($JAR). Quotient allows ONE session
# per account: every login kills that account's previous cookie, so NEVER log in
# ad hoc; run ./qlogin only when a request returns {"error":"Forbidden"}.
set -euo pipefail
cd "$(dirname "$0")"
source ./scrim.env
tmp=$(mktemp)
curl -s --max-time 15 -c "$tmp" -X POST "http://$ENGINE_IP/api/login" \\
  -H 'Content-Type: application/json' \\
  -d "{\\"username\\":\\"$MY_TEAM\\",\\"password\\":\\"$MY_PW\\"}" >/dev/null
mv "$tmp" "$JAR"
"""

SCORE_PY = '''#!/usr/bin/env python3
"""Print your team's live scoreboard status as plain text. Env from scrim.env (exported)."""
import json
import os
import subprocess
import urllib.request

engine = os.environ["ENGINE_IP"]
team = os.environ["MY_TEAM"]
jar = os.environ.get("JAR") or f"/tmp/jar.{team}"
qlogin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qlogin")


def call(path, cookie=None):
    req = urllib.request.Request(f"http://{engine}{path}",
                                 headers={"Cookie": cookie} if cookie else {})
    return urllib.request.urlopen(req, timeout=15)


def jar_cookie():
    # curl Netscape jar: the session cookie is on the last non-comment line
    try:
        for line in reversed(open(jar).read().splitlines()):
            if line and not line.startswith("#"):
                name, value = line.split()[-2:]
                return f"{name}={value}"
    except OSError:
        pass
    return None


def load_teams(cookie):
    data = json.loads(call("/api/teams", cookie).read())
    if not isinstance(data, list):
        raise ValueError(f"unexpected payload: {str(data)[:80]}")
    return data


# Quotient allows ONE session per account (every login kills the previous
# cookie) — read the shared jar, and on rejection refresh it via ./qlogin.
# Never log in directly from this script.
cookie = jar_cookie()
if cookie:
    try:
        teams = load_teams(cookie)
    except Exception:
        cookie = None
if not cookie:
    subprocess.run([qlogin], check=False)
    cookie = jar_cookie()
teams = load_teams(cookie)
tid = next(t["ID"] for t in teams if t["Name"] == team)
for s in json.loads(call(f"/api/services/{tid}", cookie).read()):
    rounds = s.get("Last10Rounds") or []
    checks = (rounds[0] if rounds else {}).get("Checks") or []
    up = bool(checks) and all(c.get("Result") for c in checks)
    err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
    print(f"{'UP  ' if up else 'DOWN'} {s['ServiceName']:16s} {err[:70]}")
'''

MYSCORE = """#!/usr/bin/env bash
# myscore — print your team's live scoreboard status as plain text.
cd "$(dirname "$0")"
set -a; source ./scrim.env; set +a
exec python3 "$(dirname "$0")/score.py"
"""

SUBMIT_INJECT = """#!/usr/bin/env bash
# submit-inject <injectId> <file> — submit a written deliverable to the scoreboard.
cd "$(dirname "$0")"; source ./scrim.env
submit() { curl -s --max-time 30 -b "$JAR" -F "file=@$2" -X POST "http://$ENGINE_IP/api/injects/$1/submit"; }
out=$(submit "$1" "$2")
case "$out" in
  *'"error"'*) ./qlogin; out=$(submit "$1" "$2") ;;
esac
echo "$out"
"""

OPENCODE_PROJECT_CFG = """{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "{PROVIDER_KEY}": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Scrim LLM",
      "options": {
        "baseURL": "{BASE_URL}",
        "apiKey": "{API_KEY_FIELD}"
      },
      "models": {
        "{MODEL_ID}": {
          "name": "{MODEL_ID}",
          "limit": { "context": {CTX}, "output": {OUT} }{EFFORT}
        }
      }
    }
  }
}
"""


CYCLE_TIMEOUT = 1500


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd, cwd=None, env=None, timeout=None, check=True, tail=None):
    log("$ " + " ".join(str(c) for c in cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, timeout=timeout,
                       capture_output=True, text=True)
    if tail and r.stdout:
        print("\n".join(r.stdout.splitlines()[-tail:]))
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed rc={r.returncode}: {r.stderr[-800:]}")
    return r


# -- stages ---------------------------------------------------------------------------

def stage_author(args):
    src = Path(args.from_template or DEFAULT_TEMPLATE)
    dst = REPO / "competitions" / args.new
    if dst.exists():
        raise RuntimeError(f"{dst} already exists")
    log(f"authoring {dst} from template {src}")
    (REPO / "competitions" / args.new).mkdir(parents=True)
    for item in src.iterdir():
        if item.name in RUNTIME_FILES or item.name in ("injects", "LOG.md") \
                or item.name.startswith("sub-") or item.name.startswith(".nakon-domain-"):
            continue
        if item.name == ".phase6-swept":  # resume marker: must never leak into a fresh comp
            continue
        if item.is_dir():
            subprocess.run(["cp", "-r", str(item), str(dst / item.name)], check=True)
        else:
            (dst / item.name).write_bytes(item.read_bytes())
    injects_src = src / "injects"
    if injects_src.exists():
        (dst / "injects").mkdir(exist_ok=True)
        for item in injects_src.iterdir():
            subprocess.run(["cp", "-r", str(item), str(dst / "injects" / item.name)], check=True)
    compfile = (dst / "Compfile").read_text().splitlines()
    compfile[0] = f"name {args.new}"
    (dst / "Compfile").write_text("\n".join(compfile) + "\n")
    log("authored (scenario/creds carry over; edit Compfile/box_vulns.json to re-theme)")


def creds_from_files(comp):
    """All secrets/logins from the post-deploy artifacts (no terraform output needed)."""
    state = json.loads((comp / ".deploy_state.json").read_text())
    teams = json.loads((comp / "teams.json").read_text())
    engine_ip = re.search(r"http://([0-9.]+)", (comp / "credentials.txt").read_text()).group(1)
    box_username, _credlist = load_users_config(comp)
    return {
        "ENGINE_IP": engine_ip,
        "ADMIN_PW": state["admin_password"],
        "INJECT_PW": state.get("inject_password") or "",
        "BOX_PW": state["box_password"],
        "BOX_USER": box_username,
        "KEY_PATH": str((REPO / "proxmox").resolve()),
        "VM_USER": os.environ.get("TF_VAR_vm_username", "sysadmin"),
        **{f"{k.upper()}_PW": v["password"] for k, v in teams.items()},
        **{f"{k.upper()}_ID": v["identifier"] for k, v in teams.items()},
    }


def stage_deploy(args, comp):
    cmd = ["python3", "-u", "create-competition.py", "--competition", comp.name,
           "--teams", str(args.teams), "--yes"]
    if comp.joinpath(".deploy_state.json").exists() and args.resume:
        cmd += ["--from-phase", str(args.resume)]
    r = run(cmd, cwd=REPO, timeout=6 * 3600, check=False)
    # keep the whole pipeline output — the tail-15 above is not enough to
    # diagnose a mid-phase failure (e.g. the real terraform error)
    if r.returncode != 0 and args.run_dir:
        (Path(args.run_dir) / "deploy.log").write_text(r.stdout or "")
    for line in (r.stdout or "").splitlines()[-15:]:
        print("   ", line)
    if r.returncode != 0:
        raise RuntimeError(f"deploy failed rc={r.returncode} (log above); fix and resume with --skip-deploy/--from-phase")
    if "last_phase: 7" not in (r.stdout or "") and "Deploy complete" not in (r.stdout or ""):
        state = json.loads((comp / ".deploy_state.json").read_text())
        if state.get("last_phase") != 7:
            raise RuntimeError("deploy did not reach phase 7")


def stage_verify(args, comp, creds):
    log("verify-competition + fire test")
    r = run(["python3", "verify-competition.py", str(comp.relative_to(REPO)),
             "--engine-ip", creds["ENGINE_IP"], "--admin-password", creds["ADMIN_PW"]],
            cwd=REPO, timeout=1800, check=False)
    print("\n".join((r.stdout or "").splitlines()[-25:]))
    if r.returncode != 0:
        log("WARNING: verify reported failures (continuing — investigate before event)")
    # fire test: kill nginx on team1 web01, expect DOWN, restore, expect UP
    proxy = (f"ssh -i {creds['KEY_PATH']} -o StrictHostKeyChecking=no "
             f"-o UserKnownHostsFile=/dev/null -W %h:%p {creds['VM_USER']}@{creds['ENGINE_IP']}")
    base = ["ssh", "-i", creds["KEY_PATH"], "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", f"ProxyCommand={proxy}"]
    def ssh_web01(cmd):
        return subprocess.run(base + [f"{creds['BOX_USER']}@192.168.{creds['TEAM1_ID']}.4", cmd],
                              capture_output=True, text=True, timeout=60)
    ssh_web01("echo %s | sudo -S systemctl stop nginx" % creds["BOX_PW"])
    time.sleep(150)
    down = team_down(creds, "team1")
    ssh_web01("echo %s | sudo -S systemctl start nginx" % creds["BOX_PW"])
    time.sleep(150)
    up = not team_down(creds, "team1")
    log(f"fire test: down_detected={down} restored={up}")
    if not (down and up):
        log("WARNING: fire test incomplete — scoring path may be broken")


def _qlogin(creds, user, jar):
    """Refresh a Quotient cookie jar for one account.

    Quotient allows ONE session per account — every login invalidates the
    account's previous cookie — so every client (monitor, status, blues,
    capture) shares one jar per account and nobody logs in on the happy path.
    The newest login always wins, so concurrent refreshes converge on the only
    valid cookie; no locking needed.
    """
    subprocess.run(["curl", "-s", "--max-time", "15", "-c", jar, "-X", "POST",
                    f"http://{creds['ENGINE_IP']}/api/login", "-H", "Content-Type: application/json",
                    "-d", json.dumps({"username": user, "password": creds[user.upper() + "_PW"]})],
                   capture_output=True)


def qget(creds, user, jar, path):
    """GET a Quotient path with the account's shared jar. An invalidated cookie
    comes back as {"error": ...} — re-login into the jar and retry once."""
    def _get():
        return subprocess.run(["curl", "-s", "--max-time", "15", "-b", jar,
                               f"http://{creds['ENGINE_IP']}{path}"],
                              capture_output=True, text=True)
    r = _get()
    if '"error"' not in (r.stdout or ""):
        return r
    _qlogin(creds, user, jar)
    return _get()


def team_down(creds, team):
    r = qget(creds, team, f"/tmp/jar.{team}",
             f"/api/services/{'1' if team == 'team1' else '2'}")
    try:
        svcs = json.loads(r.stdout)
        return any(not (s.get("Last10Rounds") and s["Last10Rounds"][0].get("Checks")
                        and all(c.get("Result") for c in s["Last10Rounds"][0]["Checks"]))
                   for s in svcs)
    except Exception:
        return False


def stage_blues(args, comp, run_dir, creds, t0):
    api_key()
    blue_model = args.blue_model
    local_blue = "openrouter" not in args.blue_base_url
    for n in (1, 2):
        wd = run_dir / f"blue-team{n}"
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "submissions").mkdir(exist_ok=True)
        tid = creds[f"TEAM{n}_ID"]
        (wd / "scrim.env").write_text(
            f"ENGINE_IP={creds['ENGINE_IP']}\nMY_TEAM=team{n}\nMY_PW={creds[f'TEAM{n}_PW']}\n"
            f"MY_TID={tid}\nBOX_PW={creds['BOX_PW']}\nINJECT_PW={creds['INJECT_PW']}\n"
            f"KEY_PATH={creds['KEY_PATH']}\nVM_USER={creds['VM_USER']}\nBOX_USER={creds['BOX_USER']}\n"
            f"JAR=/tmp/jar.team{n}\n")
        os.chmod(wd / "scrim.env", 0o600)
        for helper, body in (("mybox", MYBOX), ("qlogin", QLOGIN), ("score.py", SCORE_PY),
                             ("myscore", MYSCORE), ("submit-inject", SUBMIT_INJECT)):
            p = wd / helper
            p.write_text(body)
            p.chmod(0o755)
        shutil_copy(comp / "packet.md", wd / "packet.md")
        (wd / "LOG.md").write_text(f"# Blue team {n} — defense log\n")
        # Local llama.cpp endpoints: opencode's own base prompt is ~20k tokens, so
        # the advertised context must leave room for it or opencode self-compacts
        # fatally (60000 verified against the slot limit); no reasoning_effort.
        (wd / "opencode.jsonc").write_text(
            OPENCODE_PROJECT_CFG
            .replace("{PROVIDER_KEY}", provider_key(args.blue_base_url))
            .replace("{BASE_URL}", args.blue_base_url)
            .replace("{API_KEY_FIELD}", "{env:OPENROUTER_API_KEY}" if not local_blue else "local")
            .replace("{MODEL_ID}", blue_model)
            .replace("{CTX}", "60000" if local_blue else "120000")
            .replace("{OUT}", "4000" if local_blue else "16000")
            .replace("{EFFORT}", "" if local_blue else effort_json(args.reasoning_effort)))
    log(f"blue workdirs ready (model {blue_model} @ {args.blue_base_url})")


def shutil_copy(src, dst):
    dst.write_bytes(src.read_bytes())


def api_key():
    for var in ("OPENROUTER_API_KEY",):
        if os.environ.get(var):
            return os.environ[var]
    env_file = BAD_AUTO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("BAuto_LLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no API key: set OPENROUTER_API_KEY or BAuto_LLM_API_KEY in bad-auto/.env")


def provider_key(base_url):
    return "openrouter" if "openrouter" in base_url else "scrim-llm"


def effort_json(effort):
    """opencode.jsonc fragment requesting a reasoning effort for the model."""
    if not effort:
        return ""
    return f',\n          "options": {{ "reasoning_effort": "{effort}" }}'


def status_text(creds, team):
    """Compact plain-text scoreboard for the cycle prompt."""
    tid = creds[team.upper() + "_ID"]
    r = qget(creds, team, f"/tmp/jar.{team}", f"/api/services/{tid}")
    lines = []
    try:
        services = json.loads(r.stdout)
        if not isinstance(services, list):
            raise ValueError(f"unexpected payload: {str(services)[:80]}")
        for s in services:
            rounds = s.get("Last10Rounds") or []
            checks = (rounds[0] if rounds else {}).get("Checks") or []
            ok = bool(checks) and all(c.get("Result") for c in checks)
            err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
            lines.append(f"{'UP  ' if ok else 'DOWN'} {s['ServiceName']:16s} {err[:60]}")
    except Exception as e:
        # Self-diagnosing failure line: the monitor.log entry then says WHY
        lines.append(f"scoreboard unreachable ({type(e).__name__}: {e}; body={(r.stdout or '')[:80]!r})")
    return "\n".join(lines)


def blue_cycle_prompt(n, creds, args, elapsed, remain):
    tid = creds[f"TEAM{n}_ID"]
    return f"""You are Blue Team {n} defending the Meridian Health ward network (practice competition).
T+{elapsed}min of {args.duration_min} ({remain}min left). Work in THIS directory; everything you need is here.

LIVE SCOREBOARD (your 8 scored services — availability is points every minute):
{status_text(creds, f'team{n}')}

REACH YOUR BOXES (subnet 192.168.{tid}.0/24):
  Linux ({creds['BOX_USER']}, password in scrim.env, sudo: echo $BOX_PW | sudo -S <cmd>):  ./mybox web01 "<cmd>"   (also app01, db01)
  Windows (Administrator, same password):                                    ./mybox dc01 "<cmd>"    (also win01)
  ./mybox runs one command through the gateway and prints output — prefer it over hand-building ssh.

SCOREBOARD + INJECTS — Quotient allows ONE session per account, so NEVER log in
directly (that kills the shared jar's session). Use the jar; if a call answers
{{"error":"Forbidden"}}, run ./qlogin once and retry:
  source ./scrim.env
  curl -s -b "$JAR" http://$ENGINE_IP/api/services/$MY_TID | python3 -m json.tool
  curl -s -b "$JAR" http://$ENGINE_IP/api/injects | python3 -c "import json,sys;[print(i['ID'],i['Title'],'due',i['DueTime'][11:16],'subs',len(i.get('Submissions') or [])) for i in json.load(sys.stdin)]"
  echo "## deliverable" > sub.md && ./submit-inject <injectId> sub.md     # submit BEFORE close time

CYCLE TASK (~5 minutes then STOP):
1. If any service above is DOWN: restore it NOW (./mybox web01 "echo $BOX_PW | sudo -S systemctl unmask nginx; echo $BOX_PW | sudo -S systemctl start nginx" for example). Availability beats everything.
2. Else: the most urgent OPEN inject (due soonest; schedule in packet.md) — investigate on the boxes, write the deliverable, ./submit-inject.
3. Else: continue the misconfig hunt noted in LOG.md (planted rogue admins, backdoor services/tasks, bad firewall rules, exposed creds, SUID/sudo issues). Fix what is safe; never take a scored service down.
4. Append one timestamped line to LOG.md. ROE: never attack the engine ($ENGINE_IP), never change the scoring-check accounts (triage/svc-imaging/wardops). Keep replies terse."""


def _opencode_run(args, prompt, wd, env):
    """One blue cycle. On failure the captured output.log holds opencode's real
    error — the 17c run's 40/40 instant 'Unexpected server error' deaths never
    reproduced (2026-09-19, same version/endpoint/context), so retry-and-record
    is the strategy, not workarounds for an unconfirmed cause."""
    return subprocess.run(["opencode", "run", "-m",
                           f"{provider_key(args.blue_base_url)}/{args.blue_model}", "--auto",
                           prompt],
                          cwd=wd, env=env, capture_output=True, text=True, timeout=CYCLE_TIMEOUT)


def blue_feed_loop(n, args, creds, t0, stop, llm_lock, first_delay=0.0):
    if first_delay:
        stop.wait(first_delay)
        if stop.is_set():
            return
    while not stop.is_set() and (time.time() - t0) < (args.duration_min - 2) * 60:
        time.sleep(min(600, max(30, (args.duration_min * 60 - (time.time() - t0)) / 6)))
        if time.time() - t0 >= (args.duration_min - 2) * 60:
            break
        elapsed = int((time.time() - t0) / 60)
        remain = args.duration_min - elapsed
        wd = Path(args.run_dir) / f"blue-team{n}"
        env = {**os.environ}
        try:
            env["OPENROUTER_API_KEY"] = api_key()
        except RuntimeError:
            pass  # local endpoints need no key
        try:
            prompt = blue_cycle_prompt(n, creds, args, elapsed, remain)
            cycles = wd / "cycles"
            cycles.mkdir(exist_ok=True)
            # Serialize the two blues: the shared local endpoint has few slots,
            # and even cloud runs shouldn't have opencode DBs racing.
            with llm_lock:
                r = _opencode_run(args, prompt, wd, env)
                if r.returncode != 0:
                    # one immediate retry — a transient boot/endpoint failure
                    # otherwise costs the whole cycle slot to the pacing sleep
                    log(f"blue-team{n} cycle T+{elapsed} rc={r.returncode} — retrying once")
                    r = _opencode_run(args, prompt, wd, env)
            out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
            # full transcript + exact prompt kept for the after-action report;
            # feed.log stays a short readable tail
            (cycles / f"cycle-T+{elapsed:03d}.prompt.txt").write_text(prompt)
            (cycles / f"cycle-T+{elapsed:03d}.output.log").write_text(out)
            (wd / "feed.log").open("a").write(f"\n===== cycle T+{elapsed} rc={r.returncode} =====\n{out[-2000:]}\n")
            log(f"blue-team{n} cycle T+{elapsed} rc={r.returncode}")
        except subprocess.TimeoutExpired:
            (wd / "feed.log").open("a").write(f"\n===== cycle T+{elapsed} TIMEOUT =====\n")
            log(f"blue-team{n} cycle T+{elapsed} TIMED OUT")
        except Exception as e:
            log(f"blue-team{n} feed error: {e}")


def monitor_loop(args, creds, t0, stop):
    while not stop.is_set() and (time.time() - t0) < args.duration_min * 60:
        time.sleep(900)
        if stop.is_set():
            break
        try:
            snap = {t: status_text(creds, t) for t in ("team1", "team2")}
        except Exception as e:
            log(f"monitor snapshot failed: {e}")
            continue
        (Path(args.run_dir) / "monitor.log").open("a").write(
            f"\n#### T+{int((time.time()-t0)/60)}min {time.strftime('%H:%M')}\n" +
            "\n".join(f"{t}:\n{s}" for t, s in snap.items()))
        log("monitor snapshot written")
        # In-run red evidence: a teardown-time scp flake must not be able to
        # lose events.jsonl a third time, so grab it every snapshot too.
        if pull_red_snapshot(args, f"T+{int((time.time()-t0)/60):03d}"):
            log("red events.jsonl snapshot pulled")
        else:
            log("WARNING: red events.jsonl snapshot unavailable")


def _red_ssh_ctx(args):
    """(target, common ssh/scp args, engine jump ProxyCommand or None) for red01."""
    red_ip = "10.0.0.198"
    try:
        red_ip = json.loads((BAD_AUTO / "config.yaml").read_text())["deploy"]["red_ip"]
    except Exception:
        pass
    key = str(REPO / "proxmox")
    user = os.environ.get("TF_VAR_vm_username", "sysadmin")
    engine = None
    try:
        cred_text = (REPO / "competitions" / args.competition / "credentials.txt").read_text()
        engine = re.search(r"http://([0-9.]+)", cred_text).group(1)
    except Exception:
        pass
    common = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
              "-o", "ConnectTimeout=15", "-i", key]
    jump = (f"ProxyCommand=ssh -i {key} -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -W %h:%p {user}@{engine}") if engine else None
    return f"{user}@{red_ip}", common, jump


def pull_red_snapshot(args, tag=None):
    """Best-effort in-run events.jsonl pull from red01. Never raises."""
    ev = Path(args.run_dir) / "evidence" / "red"
    ev.mkdir(parents=True, exist_ok=True)
    target, common, jump = _red_ssh_ctx(args)
    dest = ev / "events.jsonl"
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = subprocess.run(["scp"] + common + extra +
                               [f"{target}:/var/lib/bad-auto/events.jsonl", str(dest)],
                               capture_output=True, timeout=75)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            if tag:
                shutil_copy(dest, ev / f"events-{tag}.jsonl")
            return True
        dest.unlink(missing_ok=True)
    return False


def stage_red(args, comp, creds, run_dir):
    llm = {"base_url": args.llm_base_url, "model": args.red_model,
           "max_tokens": 4096, "timeout": 240}
    if args.reasoning_effort:
        llm["reasoning_effort"] = args.reasoning_effort
    cfg = {
        "llm": llm,
        "intel": "nakon",
        "competition_dir": str(comp.resolve()),
        "event": {"duration_min": args.duration_min},
        "pacing": {"profile": "deadline", "decision_window_min": 3, "window_jitter_min": 1,
                   "active_burst_min": 8, "burst_jitter_min": 2, "quiet_min": 2, "quiet_jitter_min": 1,
                   "focus_rotation_min": max(10, args.duration_min // 8),
                   "max_concurrent_down_start": 1, "max_concurrent_down_end": 3,
                   "max_concurrent_down_endgame": 4, "access_deadline_remaining_min": args.duration_min // 4,
                   "endgame_start_remaining_min": args.duration_min // 8,
                   "endgame_decision_window_sec": 60, "endgame_force_active": True,
                   "min_standing_services": 2, "credlist_gate_min": args.duration_min // 4,
                   "credlist_max_per_team": 1, "lockout_gate_pct": 0.75},
        "limits": {"nmap_timing": "T3", "nmap_top_ports": 200,
                   "max_retries_per_service": 4, "spray_attempts_per_target": 24,
                   "action_timeout": 120, "scan_timeout": 900},
        "deploy": {"red_ip": "10.0.0.198", "red_gw": "10.0.0.1", "red_storage": "hdd"},
    }
    (BAD_AUTO / "config.yaml").write_text(json.dumps(cfg, indent=2))
    # Operator-side bad-auto runs (validate/dry-run/deploy bookkeeping) need a writable
    # state dir — the VM's own config hardcodes /var/lib/bad-auto inside red01, so this
    # override only affects the host side.
    env = {**os.environ, "BAuto_LLM_API_KEY": api_key(),
           "BAuto_STATE_DIR": str(Path(run_dir) / "bad-auto-state")}
    run(["python3", "-m", "badauto", "validate-llm"], cwd=BAD_AUTO, env=env, timeout=300, tail=3)
    run(["python3", "-m", "badauto", "run", "--once", "--dry-run",
         "--competition", str(comp.resolve())], cwd=BAD_AUTO, env=env, timeout=600, tail=6)
    log("deploying red01 (cloud LLM goes direct from red01 — no tunnel needed)")
    run(["python3", "-m", "badauto", "deploy", "--competition", str(comp.resolve()), "--start"],
        cwd=BAD_AUTO, env=env, timeout=1800)


def stage_run(args, creds, t0):
    stop = threading.Event()
    blue_lock = threading.Lock()
    # Blues staggered + LLM-call serialized (llm_lock): the shared local
    # endpoint has few slots and rejects oversized prompts, so the two blues
    # must never call it at the same time.
    threads = [threading.Thread(target=blue_feed_loop, args=(1, args, creds, t0, stop, blue_lock, 0.0)),
               threading.Thread(target=blue_feed_loop, args=(2, args, creds, t0, stop, blue_lock, 300.0))]
    threads.append(threading.Thread(target=monitor_loop, args=(args, creds, t0, stop)))
    for t in threads:
        t.start()
    try:
        while (time.time() - t0) < args.duration_min * 60:
            time.sleep(60)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)


def stage_capture(args, creds):
    log("capturing final evidence")
    ev = Path(args.run_dir) / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    # Capture runs after the event window closes, when team sessions have been
    # churned all event — qget re-logins and retries on {"error":"Forbidden"};
    # fall back to the team account if admin itself is locked out.
    for team in ("team1", "team2"):
        path = f"/api/services/{creds[team.upper() + '_ID']}"
        r = qget(creds, "admin", str(ev / ".jar-admin"), path)
        if '"error"' in (r.stdout or ""):
            r = qget(creds, team, str(ev / f".jar-{team}"), path)
        (ev / f"final-services-{team}.json").write_text(r.stdout or "")
        if '"error"' in (r.stdout or ""):
            log(f"WARNING: {team} services capture failed: {(r.stdout or '')[:120]}")
    # pause scoring (best-effort) to freeze the final state
    jar = str(ev / ".jar-admin")

    def _pause():
        return subprocess.run(["curl", "-s", "--max-time", "15", "-b", jar, "-X", "POST",
                               f"http://{creds['ENGINE_IP']}/api/engine/pause",
                               "-H", "Content-Type: application/json", "-d", '{"pause": true}'],
                              capture_output=True, text=True)

    _qlogin(creds, "admin", jar)
    r = _pause()
    if '"error"' in (r.stdout or ""):
        _qlogin(creds, "admin", jar)
        _pause()
    log("engine paused (best-effort); services JSON captured")
    # Blue-side deliverables into evidence/: logs, transcripts, submissions
    for n in (1, 2):
        src = Path(args.run_dir) / f"blue-team{n}"
        dst = ev / f"blue-team{n}"
        if not src.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("LOG.md", "feed.log"):
            if (src / name).exists():
                shutil_copy(src / name, dst / name)
        for pattern in ("sub-*.md", "sub-*.txt"):
            for f in src.glob(pattern):
                shutil_copy(f, dst / f.name)
        for sub in ("submissions", "cycles"):
            if (src / sub).is_dir():
                subprocess.run(["cp", "-r", str(src / sub), str(dst / sub)], check=False)
    log("blue evidence collected")


def pull_red_evidence(args):
    """Fetch the red agent's on-VM state before badauto destroy erases it.

    red01's events.jsonl is the only complete record of what red did. The
    direct operator->red01 path flaked in the 09-17b run (scp hung past 60s),
    so fall back to the scoring engine as a jump host — the engine and red01
    share a subnet by construction.
    """
    ev = Path(args.run_dir) / "evidence" / "red"
    ev.mkdir(parents=True, exist_ok=True)
    target, common, jump = _red_ssh_ctx(args)
    log(f"pulling red evidence from {target} before destroy (direct, then via engine jump host)")

    for remote, local in (("/var/lib/bad-auto/events.jsonl", "events.jsonl"),
                          ("/var/lib/bad-auto/world.json", "world.json")):
        attempts = [[], (["-o", jump] if jump else [])]
        ok = False
        for extra in attempts:
            r = subprocess.run(["scp"] + common + extra + [f"{target}:{remote}", str(ev / local)],
                               capture_output=True, timeout=120)
            if r.returncode == 0 and (ev / local).exists() and (ev / local).stat().st_size > 0:
                ok = True
                break
            (ev / local).unlink(missing_ok=True)
        if not ok:
            log(f"WARNING: could not pull {remote}")

    journal_cmds = [["sudo -n journalctl -u bad-auto --no-pager 2>/dev/null || true", []]]
    if jump:
        journal_cmds.append(["sudo -n journalctl -u bad-auto --no-pager 2>/dev/null || true",
                             ["-o", jump]])
    journal = ""
    for cmd, extra in journal_cmds:
        r = subprocess.run(["ssh"] + common + extra + [target, cmd],
                           capture_output=True, text=True, timeout=90)
        if (r.stdout or "").strip():
            journal = r.stdout
            break
    if journal:
        (ev / "bad-auto-journal.log").write_text(journal)
    got = sorted(p.name for p in ev.iterdir() if p.stat().st_size > 0)
    log(f"red evidence captured: {got}")
    return ev


def stage_teardown(args, creds=None):
    if creds:
        try:
            pull_red_evidence(args)
        except Exception as e:
            log(f"WARNING: red evidence pull failed: {e}")
    log("teardown: red01 + NAT")
    env = {**os.environ, "BAuto_LLM_API_KEY": api_key()}
    run(["python3", "-m", "badauto", "destroy"], cwd=BAD_AUTO, env=env, timeout=900, check=False)
    if args.keep_range:
        log("--keep-range: leaving the competition range up")
        return
    log("teardown: competition range")
    run(["python3", "destroy-competition.py", "--competition", args.competition, "--yes"],
        cwd=REPO, timeout=3600)


# -- main -----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--competition", required=True, help="competition dir under competitions/")
    p.add_argument("--new", help="author competitions/<NAME> from --from-template first")
    p.add_argument("--from-template", default=None)
    p.add_argument("--teams", type=int, default=2)
    p.add_argument("--duration-min", type=int, default=90)
    p.add_argument("--blue-model", default="openai/gpt-5.6-luna", help="cheap cloud model for blue agents")
    p.add_argument("--red-model", default="openai/gpt-5.6-luna", help="cheap cloud model for bad-auto")
    p.add_argument("--reasoning-effort", default="minimal",
                   help="reasoning effort sent to the LLM (GPT-5.x/o-series); '' disables")
    p.add_argument("--llm-base-url", default="https://openrouter.ai/api/v1")
    p.add_argument("--blue-base-url", default=None,
                   help="LLM base URL for blues only (defaults to --llm-base-url); "
                        "e.g. the local qwen endpoint http://100.64.0.9:8080/v1")
    p.add_argument("--skip-deploy", action="store_true", help="competition already at phase 7")
    p.add_argument("--from-phase", dest="resume", type=int, default=None,
                   help="resume create-competition at this phase")
    p.add_argument("--keep-range", action="store_true", help="skip destroy-competition at teardown")
    p.add_argument("--run-dir", default=None)
    args = p.parse_args()
    args.blue_base_url = args.blue_base_url or args.llm_base_url

    comp = REPO / "competitions" / args.competition
    if args.new:
        args.competition = args.new
        comp = REPO / "competitions" / args.new
        stage_author(args)
    if not (comp / "Compfile").exists():
        sys.exit(f"no such competition: {comp}")

    run_dir = Path(args.run_dir or f"/home/hna/dev/dawgsec/scrim-runs/{args.competition}")
    run_dir.mkdir(parents=True, exist_ok=True)
    args.run_dir = str(run_dir)
    log(f"run dir: {run_dir}")

    if not args.skip_deploy:
        stage_deploy(args, comp)
    creds = creds_from_files(comp)
    log(f"engine {creds['ENGINE_IP']}, teams {[(k, v['identifier']) for k, v in json.loads((comp / 'teams.json').read_text()).items()]}")

    if not (comp / "packet.md").exists():
        log("packet.md missing — generating")
        run(["python3", "generate-packet.py", str(comp)], cwd=REPO, timeout=120)

    stage_verify(args, comp, creds)
    stage_blues(args, comp, run_dir, creds, time.time())

    log("T0 — starting red, then feeding blues")
    t0 = time.time()
    stage_red(args, comp, creds, run_dir)
    stage_run(args, creds, t0)

    stage_capture(args, creds)
    stage_teardown(args, creds)
    log("DONE — reports: run-agent-scrim output above + run_dir evidence; write FINDINGS from blue logs and bad-auto events")


if __name__ == "__main__":
    main()
