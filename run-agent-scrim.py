#!/usr/bin/env python3
"""Deploy, verify, and run an agent-manned red-vs-blue scrim end to end."""

import argparse
import calendar
import contextlib
import json
import os
import re
import socket
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from utils import load_users_config, valid_comp_name

REPO = Path(__file__).resolve().parent
BAD_AUTO = REPO.parent / "bad-auto"
DEFAULT_TEMPLATE = REPO / "competitions" / "agent-scrim-2026-09-17b"
RUNTIME_FILES = {
    "teams.json", ".deploy_state.json", "credentials.txt", "nakon-config.json",
    "cloned_vms.json", "packet.md", "event.conf",
}

SSH_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"


def mybox_script(comp):
    """Per-comp mybox helper: one case branch per box from boxes.json.

    The helper used to hardcode the 5-box dc01/win01/web01/app01/db01 lineup with
    fixed octets — on any other comp (cde-2026: ad01/ftp01/web01/db01, db01 at .5)
    it silently targeted the wrong hosts or refused known boxes. Windows = boxes
    whose template name contains "windows" (password auth as Administrator);
    everything else = key auth as the provisioning user."""
    boxes = json.loads((comp / "boxes.json").read_text())
    proxy = f'"ProxyCommand=ssh -i $KEY_PATH {SSH_OPTS} -W %h:%p $VM_USER@$ENGINE_IP"'
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", 'cd "$(dirname "$0")"',
             f'BOX=${{1:?box: {"|".join(b["name"] for b in boxes)}}}; shift',
             "source ./scrim.env", 'case "$BOX" in']
    for b in boxes:
        target = f"192.168.$MY_TID.{b['last_octet']}"
        if "windows" in str(b.get("template") or ""):
            lines.append(
                f'  {b["name"]}) exec sshpass -p "$BOX_PW" ssh {SSH_OPTS} -o {proxy} '
                f'"Administrator@{target}" "$@" ;;')
        else:
            lines.append(
                f'  {b["name"]}) exec ssh -i "$KEY_PATH" {SSH_OPTS} -o {proxy} '
                f'"$BOX_USER@{target}" "$@" ;;')
    lines.append('  *) echo "unknown box $BOX" >&2; exit 2 ;;')
    lines.append("esac")
    return "\n".join(lines) + "\n"

QLOGIN = """#!/usr/bin/env bash
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
    try:
        for line in reversed(open(jar).read().splitlines()):
            # HttpOnly cookies land in the jar as "#HttpOnly_..." — that's not a
            # comment; only "# "-prefixed lines are. Current Quotient sets HttpOnly.
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 7:
                return f"{parts[-2]}={parts[-1]}"
    except OSError:
        pass
    return None


def load_teams(cookie):
    data = json.loads(call("/api/teams", cookie).read())
    if not isinstance(data, list):
        raise ValueError(f"unexpected payload: {str(data)[:80]}")
    return data


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
cd "$(dirname "$0")"
set -a; source ./scrim.env; set +a
exec python3 "$(dirname "$0")/score.py"
"""

SUBMIT_INJECT = """#!/usr/bin/env bash
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


CYCLE_TIMEOUT = 1800
CYCLE_TARGET_PERIOD = 600
MONITOR_INTERVAL = 300

_llm_down_since = None


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
        if item.name == ".phase6-swept":
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


def _web01_units(comp):
    """(candidate systemd units, scored port) for the fire test's stop/restore on web01.

    Returns every unit the web01 pins map to — which one actually exists is resolved
    on the box (apache2 vs httpd across distros) by the caller. The old code
    hardcoded nginx, which doesn't exist on comps whose web box runs apache
    (cde-2026: Fedora httpd) — the fire test would stop a non-existent unit, see no
    scoreboard change, and abort."""
    pins = json.loads((comp / "box_services.json").read_text()).get("web01", [])
    candidates = []
    for pin in pins:
        if isinstance(pin, str):
            pin = {"name": pin}
        mapped = _WATCHDOG_UNITS.get(pin.get("name", ""), [])
        if not mapped:
            continue
        httpish = pin.get("port") == 80 or pin.get("display") in ("http", "https")
        candidates.append((httpish, pin.get("port") or 80, mapped))
    if not candidates:
        raise SystemExit("  ERROR: no serviceable web01 unit for the fire test — "
                         "web01 pins in box_services.json map to nothing in _WATCHDOG_UNITS")
    candidates.sort(key=lambda c: (not c[0], c[1]))
    return candidates[0][2], candidates[0][1]


def comp_world(comp):
    """Box/scenario facts for the blue cycle prompt, from the comp's own files.

    The prompt used to hardcode the Meridian 5-box world (box names, '8 scored
    services', nginx restore example, wardops ROE) and lied on every other lineup."""
    boxes = json.loads((comp / "boxes.json").read_text())
    windows = [b["name"] for b in boxes if "windows" in str(b.get("template") or "")]
    linux = [b["name"] for b in boxes if b["name"] not in windows]
    meta = {}
    for line in (comp / "Compfile").read_text().splitlines():
        key, _, value = line.partition(" ")
        if key in ("name", "scenario"):
            meta[key] = value.strip()
    try:
        units, _ = _web01_units(comp)
    except SystemExit:
        units = None
    return {"name": meta.get("name", ""), "scenario": meta.get("scenario", ""),
            "linux": linux, "windows": windows,
            "web_unit": units[0] if units else None}


def _verify_flags(comp):
    """Packet/no-vuln flags for verify-competition, derived from the comp's files.

    A packet-sourced comp gets the packet fidelity gates; an empty box_vulns.json
    means the misconfig spot-check has nothing to confirm and must be skipped
    (cde-2026 failed verify as 'FAIL' with every summary line PASS — the packet
    AD-misconfig model plants no box configurations)."""
    flags = []
    try:
        src = next((l.split(None, 1)[1].strip() for l in (comp / "Compfile").read_text().splitlines()
                    if l.startswith("packet_source")), None)
    except OSError:
        src = None
    if src:
        flags += ["--packet", src]
    try:
        vulns = json.loads((comp / "box_vulns.json").read_text())
        if not any(vulns.values()):
            flags.append("--expect-no-vulns")
    except (OSError, ValueError):
        pass
    return flags


def stage_verify(args, comp, creds):
    log("verify-competition + fire test")
    r = run(["python3", "verify-competition.py", str(comp.relative_to(REPO)),
             "--engine-ip", creds["ENGINE_IP"], "--admin-password", creds["ADMIN_PW"],
             *_verify_flags(comp)],
            cwd=REPO, timeout=1800, check=False)
    print("\n".join((r.stdout or "").splitlines()[-25:]))
    if r.returncode != 0:
        log("WARNING: verify reported failures (continuing — investigate before event)")
    proxy = (f"ssh -i {creds['KEY_PATH']} -o StrictHostKeyChecking=no "
             f"-o UserKnownHostsFile=/dev/null -W %h:%p {creds['VM_USER']}@{creds['ENGINE_IP']}")
    base = ["ssh", "-i", creds["KEY_PATH"], "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", f"ProxyCommand={proxy}"]
    web_ip = (json.loads((comp / "targets.json").read_text()).get("targets", {})
              .get("team1-web01", {}).get("ip"))
    web_ip = web_ip or f"192.168.{creds['TEAM1_ID']}.4"
    units, svc_port = _web01_units(comp)

    def ssh_web01(cmd):
        return subprocess.run(base + [f"{creds['BOX_USER']}@{web_ip}", cmd],
                              capture_output=True, text=True, timeout=60)
    # pick the unit that actually exists on this box (apache2 vs httpd across distros)
    probe = ('u=""; for c in %s; do systemctl list-unit-files "$c.service" --no-legend '
             '2>/dev/null | grep -q . && u=$c && break; done; echo "$u"' % " ".join(units))
    r = ssh_web01(probe)
    unit = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else units[0]
    args.web_unit = unit
    log(f"fire test unit: {unit} on {web_ip} (port {svc_port})")
    port = os.environ.get("SCRIM_WEB01_PORT", str(svc_port))

    def web01_http():
        """HTTP code for web01 over the engine's network path ('' / 000 = no answer)."""
        r = subprocess.run(["ssh", "-i", creds["KEY_PATH"], "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            f"{creds['VM_USER']}@{creds['ENGINE_IP']}",
                            f"curl -sm 10 -o /dev/null -w '%{{http_code}}' "
                            f"http://192.168.{creds['TEAM1_ID']}.4:{port}/"],
                           capture_output=True, text=True, timeout=45)
        return (r.stdout or "").strip()

    def scoreboard_down():
        """(down, err): down=True/False; err set when the scoreboard itself is unreadable."""
        try:
            return any(not s["up"] for s in parsed_status(creds, "team1")), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    ssh_web01("echo %s | sudo -S systemctl stop %s" % (creds["BOX_PW"], unit))
    time.sleep(150)
    down, down_err = scoreboard_down()
    http_down = web01_http()
    log(f"fire test: after stop — scoreboard down={down}"
        + (f" ({down_err})" if down_err else "")
        + f", web01 http={http_down or 'no answer'}")

    restored = healed = False
    for attempt in (1, 2, 3):
        # unmask first: run-12 left the unit unstartable and a plain start was a no-op
        ssh_web01("echo %s | sudo -S systemctl unmask %s" % (creds["BOX_PW"], unit))
        ssh_web01("echo %s | sudo -S systemctl start %s" % (creds["BOX_PW"], unit))
        time.sleep(150 if attempt == 1 else 60)
        up, up_err = scoreboard_down()
        http_up = web01_http()
        restored = up is False
        healed = restored and http_up not in ("", "000")
        log(f"fire test: restore attempt {attempt} — scoreboard "
            f"{'up' if up is False else ('unreadable' if up is None else 'still down')}"
            f"{f' ({up_err})' if up_err else ''}, web01 http={http_up or 'no answer'}")
        if healed:
            break
    log(f"fire test: down_detected={down} restored={restored} healed={healed}")
    if not (down and restored and healed):
        raise RuntimeError(
            "fire test failed — team1 web01-http was not verifiably down and restored, so the "
            "scoring path is unproven. Manual fix: ./mybox web01 'echo $BOX_PW | sudo -S "
            f"systemctl unmask {unit} && sudo systemctl start {unit}', confirm "
            f"http://192.168.{creds['TEAM1_ID']}.4/ answers from the engine, then re-run "
            "(the fire test re-validates before T0).")


def _qlogin(creds, user, jar):
    """Refresh a Quotient cookie jar for one account (shared jar; newest login wins)."""
    subprocess.run(["curl", "-s", "--max-time", "15", "-c", jar, "-X", "POST",
                    f"http://{creds['ENGINE_IP']}/api/login", "-H", "Content-Type: application/json",
                    "-d", json.dumps({"username": user, "password": creds[user.upper() + "_PW"]})],
                   capture_output=True)


def qget(creds, user, jar, path):
    """GET a Quotient path with the account's shared jar; re-login and retry once on rejection."""
    def _get():
        return subprocess.run(["curl", "-s", "--max-time", "15", "-b", jar,
                               f"http://{creds['ENGINE_IP']}{path}"],
                              capture_output=True, text=True)
    r = _get()
    if '"error"' not in (r.stdout or ""):
        return r
    _qlogin(creds, user, jar)
    return _get()


def _team_tid(creds, user, jar, team):
    """Map a team name to Quotient's internal team ID via /api/teams."""
    r = qget(creds, user, jar, "/api/teams")
    teams = json.loads(r.stdout)
    return str(next(t["ID"] for t in teams if t["Name"] == team))


def team_down(creds, team):
    try:
        return any(not s["up"] for s in parsed_status(creds, team))
    except Exception:
        return False


def services_to_rows(services):
    """Engine /api/services payload -> [{service, up, error}] (parsed_status + final capture).
    A team with no registered/scored services is a legitimate `null` body."""
    rows = []
    for s in services or []:
        rounds = s.get("Last10Rounds") or []
        checks = (rounds[0] if rounds else {}).get("Checks") or []
        up = bool(checks) and all(c.get("Result") for c in checks)
        err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
        rows.append({"service": s["ServiceName"], "up": bool(up), "error": err[:120]})
    return rows


def parsed_status(creds, team):
    """Parsed scoreboard for one team: [{service, up, error}]; raises on failure."""
    jar = f"/tmp/jar.{team}"
    r = qget(creds, team, jar, f"/api/services/{_team_tid(creds, team, jar, team)}")
    try:
        services = json.loads(r.stdout)
    except Exception:
        raise ValueError(f"non-JSON body: {(r.stdout or '')[:80]!r}")
    if not isinstance(services, list):
        raise ValueError(f"unexpected payload: {str(services)[:80]}")
    return services_to_rows(services)


def render_status(rows):
    return "\n".join(f"{'UP  ' if s['up'] else 'DOWN'} {s['service']:16s} {s['error'][:60]}"
                     for s in rows)


def status_text(creds, team):
    """Compact plain-text scoreboard for the cycle prompt."""
    try:
        return render_status(parsed_status(creds, team))
    except Exception as e:
        return f"scoreboard unreachable ({type(e).__name__}: {e})"


def blue_ep(args, n):
    """Per-team blue endpoint (--blue{N}-base-url/--blue{N}-model); shared endpoint fallback."""
    base = getattr(args, f"blue{n}_base_url", None)
    if base:
        return base, (getattr(args, f"blue{n}_model", None) or args.blue_model)
    return args.blue_base_url, args.blue_model


def stage_blues(args, comp, run_dir, creds, t0):
    api_key(local="openrouter" not in args.blue_base_url)
    for n in range(1, args.teams + 1):
        base_url, blue_model = blue_ep(args, n)
        local_blue = "openrouter" not in base_url
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
        for helper, body in (("mybox", mybox_script(comp)), ("qlogin", QLOGIN),
                             ("score.py", SCORE_PY), ("myscore", MYSCORE),
                             ("submit-inject", SUBMIT_INJECT)):
            p = wd / helper
            p.write_text(body)
            p.chmod(0o755)
        shutil_copy(comp / "packet.md", wd / "packet.md")
        (wd / "LOG.md").write_text(f"# Blue team {n} — defense log\n")
        if not (wd / "NOTEBOOK.md").exists():
            (wd / "NOTEBOOK.md").write_text(NOTEBOOK_TEMPLATE.format(n=n))
        (wd / "opencode.jsonc").write_text(
            OPENCODE_PROJECT_CFG
            .replace("{PROVIDER_KEY}", provider_key(base_url))
            .replace("{BASE_URL}", base_url)
            .replace("{API_KEY_FIELD}", "{env:OPENROUTER_API_KEY}" if not local_blue else "local")
            .replace("{MODEL_ID}", blue_model)
            .replace("{CTX}", "60000" if local_blue else "120000")
            .replace("{OUT}", "4000" if local_blue else "16000")
            .replace("{EFFORT}", "" if local_blue else effort_json(args.reasoning_effort)))
        log(f"blue workdir team{n} ready ({blue_model} @ {base_url})")


def shutil_copy(src, dst):
    dst.write_bytes(src.read_bytes())


def api_key(local=False):
    for var in ("OPENROUTER_API_KEY",):
        if os.environ.get(var):
            return os.environ[var]
    env_file = BAD_AUTO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("BAuto_LLM_API_KEY="):
                return line.split("=", 1)[1].strip()
    if local:
        return "local"
    raise RuntimeError("no API key: set OPENROUTER_API_KEY or BAuto_LLM_API_KEY in bad-auto/.env")


def provider_key(base_url):
    return "openrouter" if "openrouter" in base_url else "scrim-llm"


def effort_json(effort):
    """opencode.jsonc fragment requesting a reasoning effort for the model."""
    if not effort:
        return ""
    return f',\n          "options": {{ "reasoning_effort": "{effort}" }}'


def blue_cycle_prompt(n, creds, args, elapsed, remain, delta_text, inject_text, notebook_text, log_tail):
    tid = creds[f"TEAM{n}_ID"]
    world = comp_world(REPO / "competitions" / args.competition)
    lin, win = world["linux"], world["windows"]
    unit = getattr(args, "web_unit", None) or world["web_unit"]
    title = world["name"] or "the company network"
    header = f"You are Blue Team {n} defending {title} (practice competition)."
    if world["scenario"]:
        header += f"\nScenario: {world['scenario']}"
    reach = [f"REACH YOUR BOXES (subnet 192.168.{tid}.0/24):"]
    if lin:
        rest = ", ".join(lin[1:]) or "none"
        reach.append(f"  Linux ({creds['BOX_USER']}, password in scrim.env, sudo: echo $BOX_PW | sudo -S <cmd>):"
                     f"  ./mybox {lin[0]} \"<cmd>\"   (also: {rest})")
    if win:
        rest = ", ".join(win[1:]) or "none"
        reach.append(f"  Windows (Administrator, same password):"
                     f"  ./mybox {win[0]} \"<cmd>\"   (also: {rest})")
    reach.append("  ./mybox runs one command through the gateway and prints output — prefer it over hand-building ssh.")
    if unit and lin:
        restore_example = (f'(./mybox {lin[0]} "echo $BOX_PW | sudo -S systemctl unmask {unit}; '
                           f'echo $BOX_PW | sudo -S systemctl start {unit}" for example)')
    else:
        restore_example = "(restart the failed unit over ./mybox for example)"
    return f"""{header}
T+{elapsed}min of {args.duration_min} ({remain}min left). Work in THIS directory; everything you need is here.

CHANGES SINCE LAST CYCLE (orchestrator scoreboard diff — act on these first):
{delta_text}

LIVE SCOREBOARD (your scored services — availability is points every minute):
{status_text(creds, f'team{n}')}

INJECTS:
{inject_text}

TEAM NOTEBOOK (NOTEBOOK.md — your working memory; current content):
{notebook_text}

Last LOG.md lines:
{log_tail}

{chr(10).join(reach)}

SCOREBOARD + INJECTS — Quotient allows ONE session per account, so NEVER log in
directly (that kills the shared jar's session). Use the jar; if a call answers
{{"error":"Forbidden"}}, run ./qlogin once and retry:
  source ./scrim.env
  curl -s -b "$JAR" http://$ENGINE_IP/api/services/$MY_TID | python3 -m json.tool
  curl -s -b "$JAR" http://$ENGINE_IP/api/injects | python3 -c "import json,sys;[print(i['ID'],i['Title'],'due',i['DueTime'][11:16],'subs',len(i.get('Submissions') or [])) for i in json.load(sys.stdin)]"
  echo "## deliverable" > sub.md && ./submit-inject <injectId> sub.md     # submit BEFORE close time

CYCLE TASK — you have ~25 wall-clock minutes for this whole cycle; pace for it,
update the notebook BEFORE acting (so an interrupted cycle still hands over context),
then AT MOST TWO change/fix actions, then STOP. Read-only investigation (status queries,
hunts) is fine inside your budget — two changes is the cap, not two commands:
0. FIRST: update NOTEBOOK.md (SNAPSHOT line: current state + next action; move finished
   items to DONE; add new incidents/findings) and append one timestamped line to LOG.md.
1. If any service above is DOWN (or CHANGES shows a new DOWN): restore it NOW {restore_example}. Availability beats everything.
   If the SAME service goes down again after you restored it, the attacker still has a way in —
   close the ENTRY VECTOR in the same cycle (e.g. a harvested password over SSH: set
   `PasswordAuthentication no` in sshd_config AND any sshd_config.d drop-in, reload ssh — your
   own access is key-based; rotate/lock the abused account). Restoring the symptom alone gets
   re-undone every minute.
2. Else if an inject is due within 30 minutes and unsubmitted: investigate on the boxes, write the deliverable, ./submit-inject.
3. Else: ONE hunt item from the notebook checklist (rogue UID-0 users, cron, systemd units, sudoers, firewall rules, listeners, Windows services/tasks/run-keys). Fix what is safe; never take a scored service down.
4. Wrap up by minute 20: finish the current step, re-check the scoreboard, leave the
   SNAPSHOT line + LOG.md current so the next cycle starts warm.
ROE: never attack the engine ($ENGINE_IP) or the scoring path; never lock out, rename, or
rotate {creds['BOX_USER']}/Administrator — those are the credentials your own helpers use.
Keep replies terse."""


def scoreboard_delta(run_dir, team):
    """CHANGES SINCE LAST CYCLE for one team, from scoreboard-state.jsonl."""
    path = Path(run_dir) / "scoreboard-state.jsonl"
    try:
        recs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return "(no scoreboard history yet)"

    def states(rec):
        return {s["service"]: s["up"] for s in ((rec or {}).get("teams", {}).get(team) or [])}

    cur = recs[-1]
    curm = states(cur)
    if len(recs) < 2:
        downs = sorted(s for s, up in curm.items() if not up)
        return f"baseline snapshot T+{cur['t_plus_sec'] // 60}; DOWN at baseline: {', '.join(downs) or 'none'}"
    prevm = states(recs[-2])
    lines = []
    for svc, up in curm.items():
        if svc not in prevm:
            continue
        if prevm[svc] and not up:
            lines.append(f"{svc} DOWN (new since last cycle) — RESTORE IT FIRST")
        elif not prevm[svc] and up:
            lines.append(f"{svc} back UP — expect red to re-attack it")
        elif not up:
            since = cur["t_plus_sec"]
            for r in recs:
                if states(r).get(svc, True):
                    continue
                since = r["t_plus_sec"]
                break
            lines.append(f"{svc} still DOWN (since T+{since // 60})")
    return "\n".join(lines) or "no changes since last cycle"


def _inject_reanchor_plan(comp_injects, remote_injects):
    """Match comp injects to engine injects by title; return (updates, missing_titles).

    Each update is (engine_id, form_fields, keep_files) with times already RFC3339.
    Matching mirrors create_injects' title dedup — title is the join key.
    """
    by_title = {}
    for r in remote_injects or []:
        title = r.get("Title")
        if title:
            by_title.setdefault(title, r)
    updates, missing = [], []
    for inj in comp_injects:
        r = by_title.get(inj["title"])
        if r is None:
            missing.append(inj["title"])
            continue
        updates.append((str(r["ID"]),
                        {"open-time": inj["open_time"],
                         "due-time": inj["due_time"],
                         "close-time": inj["close_time"]},
                        list(r.get("InjectFileNames") or [])))
    return updates, missing


def reanchor_injects(args, comp, creds):
    """Re-anchor the engine's inject clocks at T0 (idempotent; run right before stage_run).

    Inject offsets resolve to absolute times at phase-7 deploy time, so a --skip-deploy
    restart or a long staging gap reaches T0 with every inject already expired
    (shakedown-5x4: blue inject score was a guaranteed 0). UpdateInject deletes any
    attachment NOT listed under keep-files, so every existing InjectFileNames entry is
    re-listed on each POST.
    """
    from config_ops import load_injects, resolve_inject_times

    injects = load_injects(comp)
    if not injects:
        return
    for inj in injects:
        if not (inj["open_offset_min"] <= inj["due_offset_min"] <= inj["close_offset_min"]):
            raise SystemExit(
                f"  ERROR: inject {inj['title']!r} offsets are unordered — need "
                f"open {inj['open_offset_min']} <= due {inj['due_offset_min']} <= close "
                f"{inj['close_offset_min']}; fix competitions/{comp.name}/injects/.")
    resolve_inject_times(injects)
    jar = str(Path(args.run_dir) / ".jar-reanchor")
    _qlogin(creds, "admin", jar)
    r = qget(creds, "admin", jar, "/api/injects")
    try:
        remote = json.loads(r.stdout)
        if not isinstance(remote, list):
            raise ValueError(str(remote)[:80])
    except Exception:
        log(f"WARNING: inject re-anchor skipped — /api/injects unreadable: {(r.stdout or '')[:120]}")
        return
    updates, missing = _inject_reanchor_plan(injects, remote)
    for title in missing:
        log(f"WARNING: inject {title!r} has no engine counterpart — deploy phase 7 never created it?")
    n_ok = 0
    for inject_id, fields, keep_files in updates:
        cmd = ["curl", "-s", "--max-time", "15", "-b", jar, "-X", "POST",
               f"http://{creds['ENGINE_IP']}/api/injects/{inject_id}"]
        for key, value in fields.items():
            cmd += ["-F", f"{key}={value}"]
        for fn in keep_files:
            cmd += ["-F", f"keep-files={fn}"]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout or ""
        if '"error"' in out:
            log(f"WARNING: re-anchoring inject {inject_id} failed: {out[:120]}")
        else:
            n_ok += 1
    if updates:
        log(f"inject clocks re-anchored at T0 ({n_ok}/{len(updates)} updated, "
            f"first opens {updates[0][1]['open-time']})")
    else:
        log("no engine injects matched competitions/%s/injects/ — nothing re-anchored" % comp.name)


def inject_brief(creds, team):
    """One line per inject with submission state; flags due-within-30-min as task #1."""
    jar = f"/tmp/jar.{team}"
    try:
        r = qget(creds, team, jar, "/api/injects")
        injects = json.loads(r.stdout)
        if not isinstance(injects, list):
            raise ValueError(f"unexpected payload: {str(injects)[:80]}")
    except Exception as e:
        return f"(inject list unavailable: {type(e).__name__}: {e})"
    if not injects:
        return "(no injects published yet)"
    now = time.time()
    lines = []

    def due_in_min(due):
        try:
            naive = time.strptime(due[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
        for cand in (calendar.timegm(naive) - now, time.mktime(naive) - now):
            if -15 * 60 <= cand <= 12 * 3600:
                return cand / 60
        return None

    for i in sorted(injects, key=lambda x: x.get("DueTime") or ""):
        subs = len(i.get("Submissions") or [])
        due_in = due_in_min(i.get("DueTime") or "")
        when = f"due in {due_in:.0f}min" if due_in is not None else f"due {(i.get('DueTime') or '')[11:16]}"
        state = "submitted ✓" if subs else "OPEN"
        urgent = (not subs) and due_in is not None and 0 <= due_in <= 30
        lines.append(f"  #{i.get('ID')} {i.get('Title')} — {when} — {state}"
                     + ("  << TASK #1: submit before close" if urgent else ""))
    return "\n".join(lines)


NOTEBOOK_TEMPLATE = """# NOTEBOOK — team {n} working memory (update FIRST every cycle)

## SNAPSHOT (one line — current state + what you were about to do next)
(none yet)

## OPEN INCIDENTS
(none yet)

## HUNT CHECKLIST
- [ ] rogue users / UID-0 accounts (/etc/passwd, uid 0 duplicates, fresh /etc/shadow entries)
- [ ] unexpected cron (/etc/cron.d/*, crontab -l, user crontabs)
- [ ] rogue systemd units (/etc/systemd/system, list-unit-files, enabled-but-unfamiliar)
- [ ] sudoers changes (/etc/sudoers, /etc/sudoers.d/*)
- [ ] firewall tampering (iptables/nft rules dropping scored ports, ufw status)
- [ ] unexpected listeners (ss -tlnp vs your known service list)
- [ ] Windows: new local admins, odd services, scheduled tasks (schtasks), HKLM Run keys
- [ ] credential exposure (world-readable files, shell history, stray keys in /root/.ssh)

## DONE
(finished items move here)
"""


def _opencode_run(args, prompt, wd, env):
    """One blue cycle in a hardened context; the whole process group dies on timeout."""
    m = re.search(r"team(\d+)$", wd.name)
    n_team = int(m.group(1)) if m else 1
    base_url, blue_model = blue_ep(args, n_team)
    # subprocess cwd= does not update $PWD and opencode resolves its project
    # (and thus the per-workdir opencode.jsonc defining the scrim-llm provider)
    # from $PWD; it also needs a shell parent to survive — see docs/scrim-harness.md.
    child_env = {**env, "PWD": str(wd), "CYCLE_PROMPT": prompt}
    proc = subprocess.Popen(["bash", "-c",
                             "exec opencode run -m "
                             f"{provider_key(base_url)}/{blue_model} --auto \"$CYCLE_PROMPT\""],
                            cwd=wd, env=child_env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=CYCLE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # opencode leaves a server grandchild holding the pipes; killing only the
        # direct child made subprocess.run block for that grandchild's lifetime
        # (run-12: one hung cycle held the shared lock ~80 min). bash is the
        # session leader (start_new_session), so killpg takes the whole tree.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _opencode_log_tail(wd):
    """Tail of the newest opencode server log under the team's isolated state."""
    logdir = Path(wd) / ".opencode-home" / "data" / "opencode" / "log"
    try:
        logs = sorted(logdir.glob("*"), key=lambda p: p.stat().st_mtime)
        if logs:
            return ("\n[opencode server log tail]\n"
                    + "\n".join(logs[-1].read_text(errors="replace").splitlines()[-15:]))
    except OSError:
        pass
    return ""


def blue_feed_loop(n, args, creds, t0, stop, llm_lock, first_delay=0.0):
    if first_delay:
        stop.wait(first_delay)
        if stop.is_set():
            return
    while not stop.is_set() and (time.time() - t0) < (args.duration_min - 2) * 60:
        started = time.time()
        elapsed = int((time.time() - t0) / 60)
        remain = args.duration_min - elapsed
        wd = Path(args.run_dir) / f"blue-team{n}"
        home = wd / ".opencode-home"
        for sub in ("data", "config", "cache"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        svc_cfg = home / "config" / "opencode"
        svc_cfg.mkdir(parents=True, exist_ok=True)
        # Fresh OS-assigned port every cycle: the fixed 49370+n collides with any
        # orphaned opencode service left by an earlier run/team on this host
        # (cde-2026 T+0: a Sep-30 orphan held 49371 and burned team1's first cycle).
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            svc_port = sock.getsockname()[1]
        (svc_cfg / "service.json").write_text(json.dumps({"port": svc_port}))
        env = {**os.environ,
               "HOME": str(home), "XDG_DATA_HOME": str(home / "data"),
               "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache")}
        try:
            env["OPENROUTER_API_KEY"] = api_key()
        except RuntimeError:
            pass
        try:
            notebook = (wd / "NOTEBOOK.md").read_text()[:2500] if (wd / "NOTEBOOK.md").exists() \
                else "(no notebook yet)"
            try:
                log_tail = "\n".join((wd / "LOG.md").read_text().splitlines()[-5:]) or "(empty)"
            except OSError:
                log_tail = "(no LOG.md yet)"
            prompt = blue_cycle_prompt(n, creds, args, elapsed, remain,
                                       scoreboard_delta(args.run_dir, f"team{n}"),
                                       inject_brief(creds, f"team{n}"),
                                       notebook, log_tail)
            cycles = wd / "cycles"
            cycles.mkdir(exist_ok=True)
            with llm_lock:
                r = _opencode_run(args, prompt, wd, env)
                if r.returncode != 0:
                    log(f"blue-team{n} cycle T+{elapsed} rc={r.returncode} — retrying once")
                    r = _opencode_run(args, prompt, wd, env)
            out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
            if r.returncode != 0:
                out += _opencode_log_tail(wd)
            (cycles / f"cycle-T+{elapsed:03d}.prompt.txt").write_text(prompt)
            (cycles / f"cycle-T+{elapsed:03d}.output.log").write_text(out)
            (wd / "feed.log").open("a").write(f"\n===== cycle T+{elapsed} rc={r.returncode} =====\n{out[-2000:]}\n")
            log(f"blue-team{n} cycle T+{elapsed} rc={r.returncode}")
        except subprocess.TimeoutExpired:
            (wd / "feed.log").open("a").write(f"\n===== cycle T+{elapsed} TIMEOUT =====\n")
            log(f"blue-team{n} cycle T+{elapsed} TIMED OUT")
        except Exception as e:
            log(f"blue-team{n} feed error: {e}")
        took = time.time() - started
        stop.wait(min(600, max(30, CYCLE_TARGET_PERIOD - took)))


# Scored service name (box_services.json) -> candidate systemd units, first existing wins.
_WATCHDOG_UNITS = {
    "nginx": ["nginx"], "apache": ["apache2", "httpd"], "httpd": ["httpd", "apache2"],
    "bind": ["named", "bind9"], "named": ["named", "bind9"],
    "mysql": ["mysql", "mariadb"], "mariadb": ["mariadb", "mysql"], "mysqld": ["mysql", "mariadb"],
    "postfix": ["postfix"], "dovecot": ["dovecot"], "vsftpd": ["vsftpd"],
    "ssh": ["ssh", "sshd"], "openssh": ["ssh", "sshd"], "sshd": ["ssh", "sshd"],
    "splunk": ["Splunkd"], "exim4": ["exim4"], "exim": ["exim4"], "sendmail": ["sendmail"],
}
WATCHDOG_INTERVAL = 60


def watchdog_script(services, box_pw):
    """Idempotent: for each scored unit that exists and is not active, unmask + enable --now.
    Prints one 'RESTORED <unit>' line per unit it had to bring back."""
    units = []
    for svc in services:
        units.extend(_WATCHDOG_UNITS.get(svc, []))
    lines = [f"S() {{ echo {shlex.quote(box_pw)} | sudo -S -p '' \"$@\"; }}"]
    for u in dict.fromkeys(units):
        lines.append(
            f"if systemctl list-unit-files {u}.service --no-legend 2>/dev/null | grep -q . "
            f"&& ! systemctl is-active --quiet {u}; then "
            f"S systemctl unmask {u} >/dev/null 2>&1; S systemctl enable --now {u} >/dev/null 2>&1 "
            f"&& echo RESTORED {u}; fi")
    return "\n".join(lines)


def blue_watchdog_loop(args, creds, t0, stop):
    """Non-LLM dead-man's switch (winad-scrim2 rec 7): an account-wide rate limit killed BOTH
    blue agents at once and web01 sat down ~2h, unwatched. Every WATCHDOG_INTERVAL this
    restores any scored Linux unit that is stopped/masked, over the operator's key via the
    engine. It only keeps availability up — it does not hunt or close the entry vector."""
    comp = REPO / "competitions" / args.competition
    boxes = json.loads((comp / "boxes.json").read_text())
    box_services = json.loads((comp / "box_services.json").read_text())
    linux = [b for b in boxes if any(s in _WATCHDOG_UNITS for s in box_services.get(b["name"], []))]
    proxy = (f"ssh -i {creds['KEY_PATH']} -o StrictHostKeyChecking=no "
             f"-o UserKnownHostsFile=/dev/null -W %h:%p {creds['VM_USER']}@{creds['ENGINE_IP']}")
    base = ["ssh", "-i", creds["KEY_PATH"], "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=15",
            "-o", f"ProxyCommand={proxy}"]
    wlog = Path(args.run_dir) / "watchdog.log"
    team_ids = sorted(v for k, v in creds.items() if re.fullmatch(r"TEAM\d+_ID", k))
    log(f"blue watchdog on: {[b['name'] for b in linux]} x {len(team_ids)} team(s)")
    while not stop.is_set() and (time.time() - t0) < args.duration_min * 60:
        for tid in team_ids:
            for b in linux:
                host = f"{creds['BOX_USER']}@192.168.{tid}.{b['last_octet']}"
                try:
                    r = subprocess.run(base + [host, "bash -s"],
                                       input=watchdog_script(box_services[b["name"]], creds["BOX_PW"]),
                                       capture_output=True, text=True, timeout=60)
                    out = [l for l in (r.stdout or "").splitlines() if l.startswith("RESTORED")]
                    msg = "; ".join(out) if out else ("" if r.returncode == 0 else
                                                      f"ssh rc={r.returncode} {(r.stderr or '').strip()[-120:]}")
                except subprocess.TimeoutExpired:
                    msg = "timeout"
                if msg:
                    line = f"T+{int(time.time() - t0) // 60}min 192.168.{tid}.{b['last_octet']} {b['name']}: {msg}"
                    with wlog.open("a") as f:
                        f.write(line + "\n")
                    log(f"watchdog {line}")
        stop.wait(WATCHDOG_INTERVAL)


def _cred_team_names(args, creds):
    """Team names this run has creds for, bounded by --teams."""
    return [f"team{i}" for i in range(1, args.teams + 1) if f"TEAM{i}_PW" in creds]


def monitor_loop(args, creds, t0, stop):
    """Snapshot scoreboard + evidence every MONITOR_INTERVAL starting at T+0."""
    sb_path = Path(args.run_dir) / "scoreboard-state.jsonl"
    teams = _cred_team_names(args, creds)
    while not stop.is_set() and (time.time() - t0) < args.duration_min * 60:
        parsed, texts = {}, {}
        for t in teams:
            try:
                parsed[t] = parsed_status(creds, t)
                texts[t] = render_status(parsed[t])
            except Exception as e:
                texts[t] = f"scoreboard unreachable ({type(e).__name__}: {e})"
        t_plus = int(time.time() - t0)
        if parsed:
            rec = {"t_plus_sec": t_plus, "wallclock": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "teams": parsed}
            with sb_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        (Path(args.run_dir) / "monitor.log").open("a").write(
            f"\n#### T+{t_plus // 60}min {time.strftime('%H:%M')}\n" +
            "\n".join(f"{t}:\n{s}" for t, s in texts.items()))
        log("monitor snapshot written")
        if pull_red_snapshot(args, f"T+{t_plus // 60:03d}"):
            log("red events.jsonl snapshot pulled")
        else:
            log("WARNING: red events.jsonl snapshot unavailable")
        red_llm_watch(args, t_plus)
        stop.wait(MONITOR_INTERVAL)


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


class RedTunnel:
    """Reverse SSH tunnel so red01 can reach an operator-side LLM endpoint.

    red01 sits on the node LAN with no tailscale, so a local endpoint is
    unreachable from it: this ssh -N -R (run on the operator, binding on
    red01) forwards red01's localhost:<remote_port> through the connection to
    the endpoint. The dress rehearsal ran an event red-LLM-less because a
    plain `ssh -R` died and nobody noticed — this one keepalives and has a
    supervisor that restarts it until teardown."""

    def __init__(self, llm_base_url, red_ip, key, user):
        url = urlparse(llm_base_url if "//" in llm_base_url else f"http://{llm_base_url}")
        host = url.hostname or "127.0.0.1"
        port = url.port or (443 if url.scheme == "https" else 80)
        if host in ("localhost", "127.0.0.1", "::1"):
            # Base URL already names an operator-side relay — mirror that port.
            self.remote_port, self.target = port, f"127.0.0.1:{port}"
        else:
            # Endpoint reachable only from the operator: bind 8180 on red01 and
            # forward straight at it (no socat relay needed).
            self.remote_port, self.target = 8180, f"{host}:{port}"
        self.red_ip, self.key, self.user = red_ip, key, user
        self.proc = None
        self.stop = threading.Event()

    def red_base_url(self):
        return f"http://localhost:{self.remote_port}/v1"

    def _spawn(self):
        self.proc = subprocess.Popen(
            ["ssh", "-N", "-T",
             "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=15",
             "-o", "ServerAliveCountMax=3",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=15",
             "-i", self.key,
             "-R", f"{self.remote_port}:{self.target}",
             f"{self.user}@{self.red_ip}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)

    def start(self):
        self._spawn()
        threading.Thread(target=self._supervise, daemon=True).start()

    def _supervise(self):
        while not self.stop.wait(15):
            if self.proc is None or self.proc.poll() is not None:
                log("red LLM tunnel died — restarting")
                self._spawn()

    def shutdown(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def maybe_start_red_tunnel(args):
    """auto: tunnel local (non-openrouter) endpoints; openrouter needs none."""
    mode = getattr(args, "red_tunnel", "auto") or "auto"
    local = "openrouter" not in args.llm_base_url
    if mode == "off" or (mode == "auto" and not local):
        return None
    if mode == "on" and not local:
        log("--red-tunnel ignored for an openrouter endpoint (red01 reaches it directly)")
        return None
    tunnel = RedTunnel(args.llm_base_url, args.red_ip, str(REPO / "proxmox"),
                       os.environ.get("TF_VAR_vm_username", "sysadmin"))
    tunnel.start()
    log(f"red LLM tunnel up: red01 localhost:{tunnel.remote_port} -> {tunnel.target}")
    return tunnel


def check_red_llm(args, base_url):
    """From-red01 LLM reachability gate.

    validate-llm runs operator-side and proves nothing about what red01 can
    reach — the dress run went red-LLM-less the whole event on exactly that
    gap. Probes <base_url>/models from red01 itself (direct, then through the
    engine jump host)."""
    target, common, jump = _red_ssh_ctx(args)
    probe = f"curl -sm 10 -o /dev/null -w '%{{http_code}}' {base_url.rstrip('/')}/models"
    for path, extra in (("direct", []), ("engine jump", ["-o", jump] if jump else None)):
        if extra is None:
            continue
        try:
            r = subprocess.run(["ssh"] + common + extra + [target, probe],
                               capture_output=True, text=True, timeout=45)
            code = (r.stdout or "").strip()
            if code == "200":
                return True
            log(f"red01 LLM probe ({path}) -> {code or (r.stderr or '').strip()[:80] or 'no answer'}")
        except subprocess.TimeoutExpired:
            log(f"red01 LLM probe ({path}) timed out")
    return False


def red_llm_url(args):
    """The LLM base URL as red01 dials it (tunnel-local when a tunnel is up).

    args.red_tunnel holds the CLI string until stage_red overwrites it with the
    live RedTunnel object (local endpoints only) — a truthy string must not be
    mistaken for a tunnel (openrouter run crashed at the first monitor tick)."""
    tunnel = getattr(args, "red_tunnel", None)
    return tunnel.red_base_url() if hasattr(tunnel, "red_base_url") else args.llm_base_url


def red_llm_watch(args, t_plus):
    """In-event from-red01 LLM probe; alerts once per failure episode."""
    global _llm_down_since
    if check_red_llm(args, red_llm_url(args)):
        if _llm_down_since is not None:
            log(f"red LLM reachable again after {(time.time() - _llm_down_since) / 60:.0f} min")
            _llm_down_since = None
        return
    if _llm_down_since is None:
        _llm_down_since = time.time()
        tunnel = getattr(args, "red_tunnel", None)
        dead = hasattr(tunnel, "proc") and tunnel.proc is not None and tunnel.proc.poll() is not None
        log(f"WARNING: T+{t_plus // 60} — red01 cannot reach the LLM endpoint"
            + (" (tunnel process dead; supervisor will respawn it)" if dead else "")
            + " — red is making decisions blind until this recovers")


def stage_red(args, comp, creds, run_dir):
    # Local endpoints (llama.cpp/qwen) are slow: tighter call timeout and no
    # JSON-retry double-call, or one decision can eat 8-16 min of a 90-min event.
    local_llm = "openrouter" not in args.llm_base_url
    llm = {"base_url": args.llm_base_url, "model": args.red_model,
           "max_tokens": 4096, "timeout": 120 if local_llm else 240,
           "json_retries": 0 if local_llm else 1}
    if args.reasoning_effort:
        llm["reasoning_effort"] = args.reasoning_effort
    cfg = {
        "llm": llm,
        "intel": "nakon",
        "competition_dir": str(comp.resolve()),
        "event": {"duration_min": args.duration_min},
        "pacing": {"decision_window_min": 2, "window_jitter_min": 1,
                   "active_burst_min": 15, "burst_jitter_min": 2, "quiet_min": 2, "quiet_jitter_min": 1,
                   "focus_rotation_min": max(10, args.duration_min // 8),
                   "max_concurrent_down_start": 2, "max_concurrent_down_end": 4,
                   "max_concurrent_down_endgame": 6, "access_deadline_remaining_min": args.duration_min // 4,
                   "endgame_start_remaining_min": 15,
                   "endgame_decision_window_sec": 60, "endgame_force_active": True,
                   "min_standing_services": 2, "credlist_gate_min": args.duration_min // 4,
                   "credlist_max_per_team": 1, "lockout_gate_pct": 0.75},
        "limits": {"nmap_timing": "T3", "nmap_top_ports": 200,
                   "max_retries_per_service": 4, "spray_attempts_per_target": 24,
                   "action_timeout": 120, "scan_timeout": 900},
        "deploy": {"red_ip": args.red_ip, "red_gw": args.red_gw, "red_storage": args.red_storage,
                   **({"red_vmid": args.red_vmid} if args.red_vmid else {}),
                   **({"template": args.red_template} if args.red_template else {}),
                   **({"red_mode": args.red_mode} if args.red_mode else {}),
                   **({"red_subnet": args.red_subnet} if args.red_subnet else {}),
                   **({"red_seg_ip": args.red_seg_ip} if args.red_seg_ip else {})},
    }
    (BAD_AUTO / "config.yaml").write_text(json.dumps(cfg, indent=2))
    env = {**os.environ, "BAuto_LLM_API_KEY": api_key(local="openrouter" not in args.llm_base_url),
           "BAuto_STATE_DIR": str(Path(run_dir) / "bad-auto-state")}
    run(["python3", "-m", "badauto", "validate-llm"], cwd=BAD_AUTO, env=env, timeout=300, tail=3)
    run(["python3", "-m", "badauto", "run", "--once", "--dry-run",
         "--competition", str(comp.resolve())], cwd=BAD_AUTO, env=env, timeout=600, tail=6)

    tunnel = maybe_start_red_tunnel(args)
    if tunnel:
        # The operator-side validate/dry-run above used the real URL on purpose;
        # red01 itself can only dial the endpoint through the tunnel.
        cfg["llm"]["base_url"] = tunnel.red_base_url()
        (BAD_AUTO / "config.yaml").write_text(json.dumps(cfg, indent=2))
        args.red_tunnel = tunnel
    red_mode = args.red_mode or "routed (bad-auto default)"
    log(f"deploying red01 at {args.red_ip} (storage {args.red_storage}, mode {red_mode})")
    run(["python3", "-m", "badauto", "deploy", "--competition", str(comp.resolve()), "--start"],
        cwd=BAD_AUTO, env=env, timeout=1800)

    red_base = cfg["llm"]["base_url"]
    if not check_red_llm(args, red_base):
        raise RuntimeError(
            f"red01 cannot reach the LLM endpoint ({red_base}) — refusing to start the event "
            f"red-LLM-less. Run a socat relay on this host and/or the reverse tunnel "
            f"(--red-tunnel), then re-run. See docs/scrim-harness.md, 'stage_red (LLM gate)'.")
    log(f"red01 reached the LLM at {red_base} — clear to start")


def stage_run(args, creds, t0):
    stop = threading.Event()
    # One lock per distinct LLM endpoint — teams sharing an endpoint share its
    # concurrency cap; teams 3/4 used to silently inherit team1's endpoint + lock.
    endpoint_locks = {}
    for n in range(1, args.teams + 1):
        base_url, _ = blue_ep(args, n)
        endpoint_locks.setdefault(base_url, threading.Lock())
    threads = []
    for n in range(1, args.teams + 1):
        base_url, _ = blue_ep(args, n)
        threads.append(threading.Thread(target=blue_feed_loop,
                                        args=(n, args, creds, t0, stop,
                                              endpoint_locks[base_url], 300.0 * (n - 1))))
    threads.append(threading.Thread(target=monitor_loop, args=(args, creds, t0, stop)))
    if getattr(args, "blue_watchdog", False):
        threads.append(threading.Thread(target=blue_watchdog_loop, args=(args, creds, t0, stop)))
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
    admin_jar = str(ev / ".jar-admin")
    teams = _cred_team_names(args, creds)
    # Final scoreboard dump goes to evidence BEFORE anything can tear the engine
    # down — teardown destroys the scoring DB, and the report reads this file.
    final = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "teams": [], "injects": [], "services": {}}
    for path, key in (("/api/teams", "teams"), ("/api/injects", "injects")):
        r = qget(creds, "admin", admin_jar, path)
        try:
            final[key] = json.loads(r.stdout)
        except Exception:
            log(f"WARNING: final {key} capture failed: {(r.stdout or '')[:120]}")
    for team in teams:
        path = f"/api/services/{_team_tid(creds, 'admin', admin_jar, team)}"
        r = qget(creds, "admin", admin_jar, path)
        if '"error"' in (r.stdout or ""):
            r = qget(creds, team, str(ev / f".jar-{team}"), path)
        (ev / f"final-services-{team}.json").write_text(r.stdout or "")
        try:
            final["services"][team] = services_to_rows(json.loads(r.stdout))
        except Exception:
            final["services"][team] = None
            log(f"WARNING: {team} services capture failed: {(r.stdout or '')[:120]}")
    (ev / "final-scoreboard.json").write_text(json.dumps(final, indent=1))
    log("final scoreboard dumped to evidence")
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
    sb = Path(args.run_dir) / "scoreboard-state.jsonl"
    if sb.exists():
        shutil_copy(sb, ev / "scoreboard-state.jsonl")
    for n in range(1, args.teams + 1):
        src = Path(args.run_dir) / f"blue-team{n}"
        dst = ev / f"blue-team{n}"
        if not src.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("LOG.md", "NOTEBOOK.md", "feed.log"):
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
    """Fetch the red agent's on-VM state (events.jsonl) before badauto destroy erases it."""
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
    tunnel = getattr(args, "red_tunnel", None)
    if hasattr(tunnel, "shutdown"):
        tunnel.shutdown()
        log("teardown: red LLM tunnel stopped")
    if creds:
        try:
            pull_red_evidence(args)
        except Exception as e:
            log(f"WARNING: red evidence pull failed: {e}")
    log("teardown: red01 + NAT")
    env = {**os.environ, "BAuto_LLM_API_KEY": api_key(local=True)}
    run(["python3", "-m", "badauto", "destroy", "--competition", args.competition, "--yes"],
        cwd=BAD_AUTO, env=env, timeout=900, check=False)
    if args.keep_range:
        log("--keep-range: leaving the competition range up")
        return
    log("teardown: competition range")
    run(["python3", "destroy-competition.py", "--competition", args.competition, "--yes"],
        cwd=REPO, timeout=3600)



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
    p.add_argument("--blue2-base-url", default=None,
                   help="separate LLM base URL for team2's blue (default: same as --blue-base-url)")
    p.add_argument("--blue2-model", default=None,
                   help="model for team2's blue when --blue2-base-url is set")
    for n in (3, 4):
        p.add_argument(f"--blue{n}-base-url", default=None,
                       help=f"separate LLM base URL for team{n}'s blue (default: --blue-base-url)")
        p.add_argument(f"--blue{n}-model", default=None,
                       help=f"model for team{n}'s blue when --blue{n}-base-url is set")
    p.add_argument("--red-ip", default="10.0.0.198", help="red01 IP on the cluster (realm default)")
    p.add_argument("--red-gw", default="10.0.0.1", help="red01 gateway")
    p.add_argument("--red-storage", default="hdd", help="storage pool red01 clones from")
    p.add_argument("--red-vmid", type=int, default=None,
                   help="red01 vmid when the cluster default collides (cyberfield used 999)")
    p.add_argument("--red-template", default=None,
                   help="red01 template name when it differs from the cluster default")
    p.add_argument("--red-mode", choices=["routed", "masq"], default=None,
                   help="red's network identity: routed (bad-auto default) gives red01 a "
                        "dedicated segment and keeps its source IP visible end-to-end, so "
                        "blue can hunt and firewall the attacker while scoring keeps "
                        "sourcing from the team gateway; masq = legacy gateway masquerade "
                        "(red unblockable-by-IP, indistinguishable from scoring)")
    p.add_argument("--red-subnet", default=None,
                   help="red segment CIDR in routed mode (bad-auto default 10.200.0.0/24); "
                        "must not overlap the team 192.168.0.0/16 or the mgmt LAN")
    p.add_argument("--red-seg-ip", default=None,
                   help="red01's address on the red segment (bad-auto default 10.200.0.10; "
                        "the engine takes the segment gateway x.x.x.1)")
    p.add_argument("--red-tunnel", choices=["auto", "on", "off"], default="auto",
                   help="reverse-SSH tunnel so red01 can reach a local LLM endpoint "
                        "(auto = on for non-openrouter endpoints)")
    p.add_argument("--skip-deploy", action="store_true", help="competition already at phase 7")
    p.add_argument("--from-phase", dest="resume", type=int, default=None,
                   help="resume create-competition at this phase")
    p.add_argument("--keep-range", action="store_true", help="skip destroy-competition at teardown")
    p.add_argument("--run-dir", default=None)
    p.add_argument("--blue-watchdog", action="store_true", dest="blue_watchdog",
                   help="run a non-LLM loop that re-unmasks/starts stopped scored Linux units every "
                        f"{WATCHDOG_INTERVAL}s — keeps availability up through a blue-agent API outage")
    args = p.parse_args()
    args.blue_base_url = args.blue_base_url or args.llm_base_url

    for nm in (args.competition, args.new):
        if nm and not valid_comp_name(nm):
            sys.exit(f"invalid competition name {nm!r} — use [a-z0-9._-], no path separators.")

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

    red_setup_started = time.time()
    stage_red(args, comp, creds, run_dir)
    t0 = time.time()
    log(f"T0 — event clock starts now (red setup took {(t0 - red_setup_started) / 60:.0f} min, "
        f"outside scored time)")
    reanchor_injects(args, comp, creds)
    stage_run(args, creds, t0)

    stage_capture(args, creds)
    stage_teardown(args, creds)
    log("DONE — reports: run-agent-scrim output above + run_dir evidence; write FINDINGS from blue logs and bad-auto events")


if __name__ == "__main__":
    main()
