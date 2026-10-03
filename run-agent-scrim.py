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
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import artifacts_ops
from config_ops import write_state, write_text_atomic
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
# scrim.env always exports JAR; the fallback mirrors the driver's run-dir jar location
# (.jars/<account>.jar) instead of the old predictable /tmp/jar.<team> path.
jar = os.environ.get("JAR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".jars", f"{team}.jar")
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
# Real JSON parse, not a substring probe: an inject whose body legitimately contains the
# text "error" used to trigger a pointless re-login (the driver has the same fix).
is_error() {
  python3 -c 'import json,sys
try: d = json.loads(sys.stdin.read())
except Exception: sys.exit(1)
sys.exit(0 if isinstance(d, dict) and "error" in d else 1)'
}
out=$(submit "$1" "$2")
if printf '%s' "$out" | is_error; then ./qlogin; out=$(submit "$1" "$2"); fi
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
# Grace between the SIGINT that lets terraform release its state lock and the SIGKILL
# that sweeps whatever ignored it (utils.run_terraform uses the same two-phase stop).
TREE_STOP_GRACE = 30
# Cookie jars live per-run, not in the shared /tmp (see jar_path).
JAR_DIRNAME = ".jars"
_JAR_LOCKS = {}
# Consecutive from-red01 LLM probe failures before the harness tries to rescue red.
# MONITOR_INTERVAL (300s) per tick: one blip must not trigger a restart that drops a
# tunnel red is mid-decision on, but silence this long is already the dress-rehearsal
# failure mode (an event ran red-LLM-less because a dead `ssh -R` went unnoticed).
RED_LLM_FAIL_THRESHOLD = 3
ALERTS_FILENAME = "alerts.jsonl"
# endpoint -> {"since": first_failure_ts|None, "failures": int, "restarted": bool}
_llm_watch = {}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class ScrimTimeout(subprocess.TimeoutExpired):
    """A supervised command blew its wall-clock budget and its process TREE was killed.

    Subclasses TimeoutExpired so the handlers that already exist around the blue cycles
    and the watchdog keep catching it. The message names the recovery path because a
    timed-out deploy/verify is resumed, not restarted: `--skip-deploy`/`--from-phase N`
    for the pipeline, `--resume-event` for an event that already reached T0.
    """

    def __init__(self, cmd, timeout):
        super().__init__(cmd, timeout)
        self.cmd, self.timeout = cmd, timeout

    def __str__(self):
        return (f"command timed out after {self.timeout}s (its whole process group was "
                f"signalled SIGINT then SIGKILL, so no orphaned grandchild still holds a "
                f"lock or keeps mutating infrastructure): "
                f"{' '.join(str(c) for c in self.cmd[:6])}"
                + (" ..." if len(self.cmd) > 6 else "")
                + " — resume the run with --skip-deploy/--from-phase (or --resume-event "
                  "if T0 was already recorded)")


def _feed_stdin(proc, text):
    """Write stdin from a thread so a timeout can still kill and drain the process.

    communicate(input=...) cannot be re-entered after TimeoutExpired, which is exactly
    what the kill-and-drain path needs, so the pipe is fed out of band and closed
    immediately (an open stdin pipe keeps a straggler grandchild alive).
    """
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        pass


def _kill_tree(proc, grace=TREE_STOP_GRACE):
    """SIGINT the process group, wait, then SIGKILL it and everything left inside it.

    SIGINT first because it is the graceful stop terraform uses to release its state
    lock (utils.run_terraform does the same); SIGKILL because the grandchildren this
    exists to reap may ignore SIGINT. killpg on an already-empty group is a no-op.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGINT)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def run_tree(cmd, cwd=None, env=None, timeout=None, check=True, tail=None,
             stdin_text=None, grace=TREE_STOP_GRACE):
    """Run cmd in its own session so a timeout kills the whole tree, not just the child.

    subprocess.run(timeout=) sends SIGKILL to the DIRECT child only. Every long-running
    command here (create-competition.py, badauto, their terraform/nakon/ssh grandchildren)
    then survives with the deploy lock still held and keeps mutating infrastructure — the
    failure class the file already fixed once for _opencode_run, where one hung cycle held
    the shared lock ~80 min because a grandchild outlived the kill. Modelled on
    utils.run_terraform: SIGINT to the group, escalate to SIGKILL after `grace`.

    Returns CompletedProcess; on timeout raises ScrimTimeout (a TimeoutExpired) whose
    message names the resume path. `run` below is the thin house wrapper for it.
    """
    cmd = [str(c) for c in cmd]
    log("$ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    if stdin_text is not None:
        threading.Thread(target=_feed_stdin, args=(proc, stdin_text), daemon=True).start()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, grace)
        proc.communicate()
        raise ScrimTimeout(cmd, timeout) from None
    except BaseException:
        # Ctrl-C / any other driver-side abort must not orphan the tree either.
        _kill_tree(proc, grace)
        proc.communicate()
        raise
    r = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if tail and r.stdout:
        print("\n".join(r.stdout.splitlines()[-tail:]))
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed rc={r.returncode}: {(r.stderr or '')[-800:]}")
    return r


def run(cmd, cwd=None, env=None, timeout=None, check=True, tail=None):
    """Supervised `subprocess.run`: every long-running call goes through the process tree."""
    return run_tree(cmd, cwd=cwd, env=env, timeout=timeout, check=check, tail=tail)


def box_sudo_stdin(base, host, box_pw, script):
    """(ssh argv, stdin text) for a root command on a team box.

    The password goes on stdin — the process argv of a running command is world-readable
    via ps, and interpolating a generated password into the remote shell string is one
    shell metacharacter away from a syntax error aborting the fire test. Same pin every
    other box path uses (beacon_ops._ssh_box). The script follows the password on the
    same stdin stream, which is what `sudo -S -p '' bash -s` expects.
    """
    return list(base) + [host, "sudo -S -p '' bash -s"], box_pw + "\n" + script


def write_evidence(path, text):
    """Write an evidence file atomically, 0600.

    Evidence files were written with a plain write_text at the process umask: the final
    scoreboard and the per-team service dumps name every scored service and every error
    string on the range, yet a report reader on a shared host could read them — and a torn
    write during teardown was possible (audit find D5).
    """
    return write_text_atomic(path, text, mode=0o600)


def secure_evidence(path):
    """chmod an evidence file that some other tool copied in (scp/cp/shutil) to 0600."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path



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
        # deploy.py writes `.postclone-swept`; this listed the long-dead `.phase6-swept`
        # name, so the real marker was copied into every competition authored from a
        # swept template and the new range skipped its post-clone sweep (audit find 12).
        if item.name == ".postclone-swept":
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
    # The pipeline's final phase number is the source of truth — hardcoded 7s went
    # stale when firewalls became phase 5 and seed became phase 8 (live-found
    # 2026-10-03: a completed deploy was rejected as "did not reach phase 7").
    from deploy_phases import PHASES
    final_phase = len(PHASES)
    if f"last_phase: {final_phase}" not in (r.stdout or ""):
        state = json.loads((comp / ".deploy_state.json").read_text())
        if state.get("last_phase") != final_phase:
            raise RuntimeError(f"deploy did not reach phase {final_phase}")


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


def scorch_script(comp):
    """Per-comp scored-check oracle: runs each pinned check the way the engine does,
    from the engine's vantage (HTTP GET / credlist password login / TCP connect), so
    blue can reconcile 'locally healthy but scorer DOWN' — run 1 showed both teams
    flying blind on exactly that after sshd hardening broke the scored ssh check."""
    boxes = {b["name"]: b for b in json.loads((comp / "boxes.json").read_text())}
    pins = json.loads((comp / "box_services.json").read_text())
    http_specs, port_specs, ssh_specs = [], [], []
    for box, services in pins.items():
        b = boxes.get(box)
        if not b:
            continue
        ip = f"192.168.$MY_TID.{b['last_octet']}"
        for pin in services:
            if isinstance(pin, str):
                pin = {"name": pin}
            port = pin.get("port")
            if not port:
                continue  # plant_only software with no listener isn't externally checkable
            if pin.get("score_only") or pin.get("plant_only"):
                kind = "code" if pin.get("display") in ("http", "https") else "port"
            else:
                kind = "code" if pin.get("display") in ("http", "https") else (
                    "ssh" if pin.get("display") == "ssh" else "port")
            if kind == "code":
                http_specs.append(f"{box}|{ip}|{port}")
            elif kind == "ssh":
                ssh_specs.append(f"{box}|{ip}|{port}")
            else:
                port_specs.append(f"{box}|{ip}|{port}")
    lines = ["#!/usr/bin/env bash", "set -uo pipefail", 'cd "$(dirname "$0")"',
             "source ./scrim.env",
             'ENGINE_RUN="ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no '
             '-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 "$VM_USER@$ENGINE_IP""',
             'echo "== scored-check oracle (engine vantage) — team $MY_TEAM =="']
    if http_specs:
        lines.append("for spec in " + " ".join(f'"{x}"' for x in http_specs) + "; do")
        lines.append('  IFS="|" read -r box ip port <<<"$spec"')
        lines.append('  r=$($ENGINE_RUN "curl -sm 5 -o /dev/null -w \'%{http_code}\' http://$ip:$port/" 2>/dev/null)')
        lines.append('  if [ "$r" = 200 ]; then echo "UP   $box http:$port (HTTP $r)"; '
                     'else echo "DOWN $box http:$port (HTTP ${r:-no answer})"; fi; done')
    if port_specs:
        lines.append("for spec in " + " ".join(f'"{x}"' for x in port_specs) + "; do")
        lines.append('  IFS="|" read -r box ip port <<<"$spec"')
        lines.append('  if $ENGINE_RUN "nc -w 5 $ip $port </dev/null >/dev/null 2>&1"; '
                     'then echo "UP   $box tcp:$port open"; else echo "DOWN $box tcp:$port closed"; fi; done')
    if ssh_specs:
        lines.append("for spec in " + " ".join(f'"{x}"' for x in ssh_specs) + "; do")
        lines.append('  IFS="|" read -r box ip port <<<"$spec"')
        lines.append('  ok=""')
        lines.append('  for pair in "${CREDLIST[@]}"; do')
        lines.append('    user=${pair%%:*}; pw=${pair#*:}')
        lines.append('    if sshpass -p "$pw" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR '
                     '-o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 '
                     '-o "ProxyCommand=ssh -i $KEY_PATH -o StrictHostKeyChecking=no '
                     '-o UserKnownHostsFile=/dev/null -W %h:%p $VM_USER@$ENGINE_IP" '
                     '"$user@$ip" true 2>/dev/null; then ok=$user; break; fi; done')
        lines.append('  if [ -n "$ok" ]; then echo "UP   $box ssh:$port (credlist login: $ok)"; '
                     'else echo "DOWN $box ssh:$port — port may be open but the scored credlist '
                     'LOGIN FAILS. If you hardened sshd: allow password auth for the credlist '
                     'account from the gateway only (Match User <acct> Address 192.168.$MY_TID.1 '
                     '/ PasswordAuthentication yes), sshd -t, reload, re-run ./scorch."; fi; done')
    lines.append('echo "(checks replicate the engine vantage; a pin DOWN here means the scorer sees it DOWN)"')
    return "\n".join(lines) + "\n"


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

    def ssh_web01(cmd, timeout=60):
        return run_tree(base + [f"{creds['BOX_USER']}@{web_ip}", cmd],
                        timeout=timeout, check=False)

    def sudo_web01(script, timeout=120):
        """Run `script` as root on web01 with the box password on STDIN.

        The old call piped the box password through an `echo` into `sudo -S` inside a
        remote shell string built with %-interpolation: the password landed in the local
        process argv (readable from ps on the operator host) and a single quote in a
        generated password would have been a shell syntax error that aborted the fire
        test. beacon_ops._ssh_box already established the house pattern — `sudo -S` reads
        the password from stdin and the script follows it on the same stream — so reuse it.
        """
        argv, stdin_text = box_sudo_stdin(base, f"{creds['BOX_USER']}@{web_ip}",
                                          creds["BOX_PW"], script)
        return run_tree(argv, timeout=timeout, check=False, stdin_text=stdin_text)

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
        r = run_tree(["ssh", "-i", creds["KEY_PATH"], "-o", "StrictHostKeyChecking=no",
                      "-o", "UserKnownHostsFile=/dev/null",
                      f"{creds['VM_USER']}@{creds['ENGINE_IP']}",
                      f"curl -sm 10 -o /dev/null -w '%{{http_code}}' "
                      f"http://192.168.{creds['TEAM1_ID']}.4:{port}/"],
                     timeout=45, check=False)
        return (r.stdout or "").strip()

    def scoreboard_down():
        """(down, err): down=True/False; err set when the scoreboard itself is unreadable.
        Watches only the fired service's row: other scored services may legitimately
        start down (planted broken services blue must restore), which would make the
        any-service gate structurally unpassable on lineups with one (17b's
        app01-dns/db01-sql sit down at T0 on the current template bases)."""
        try:
            rows = parsed_status(creds, "team1")
            fired = next((s for s in rows if s["service"].startswith("web01")), None)
            if fired is None:
                fired = rows[0]
            return not fired["up"], None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    sudo_web01(f"systemctl stop {shlex.quote(unit)}")
    time.sleep(150)
    down, down_err = scoreboard_down()
    http_down = web01_http()
    log(f"fire test: after stop — scoreboard down={down}"
        + (f" ({down_err})" if down_err else "")
        + f", web01 http={http_down or 'no answer'}")

    restored = healed = False
    for attempt in (1, 2, 3):
        # unmask first: run-12 left the unit unstartable and a plain start was a no-op
        sudo_web01(f"systemctl unmask {shlex.quote(unit)}; "
                   f"systemctl start {shlex.quote(unit)}")
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


def jar_dir(run_dir):
    """The run's private cookie-jar directory (0700, never shared /tmp)."""
    d = Path(run_dir) / JAR_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def jar_path(run_dir, account):
    """Deterministic per-account cookie jar inside the run dir, created 0600.

    `/tmp/jar.<team>` was a predictable name in the shared /tmp, created by `curl -c` at
    whatever umask the driver had: any local account on the operator host could read a
    live Quotient session cookie, and a second scrim on the same host clobbered the first
    one's jar. The name must stay deterministic (the generated qlogin/score.py helpers
    and this driver have to agree on one file), so it keeps a fixed basename — but inside
    a 0700 run-dir subdirectory, and seeded via mkstemp+rename so the mode is 0600 by
    construction instead of by luck with the umask.
    """
    path = jar_dir(run_dir) / f"{account}.jar"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(prefix=f".{account}-", dir=str(path.parent))
        os.close(fd)
        os.replace(tmp, path)
    return str(path)


def jar_lock(account):
    """One re-entrant lock per Quotient account.

    monitor_loop, inject_brief and the watchdog refresh the per-team jars concurrently,
    and Quotient allows ONE session per account: two refreshers invalidate each other's
    cookie in a loop — the documented cause of the "three-round scoreboard mystery".
    Re-entrant so a helper that refreshes while already holding the account lock (qget ->
    _qlogin) cannot deadlock against itself.
    """
    return _JAR_LOCKS.setdefault(account, threading.RLock())


def json_error(body):
    """True when a Quotient response is a JSON object carrying an `error` key.

    `'"error"' in body` flipped the verdict on any payload whose DATA contained that text
    (a service or inject legitimately named "error…") and missed an error object written
    with different spacing. A non-JSON body is NOT an error here: callers json.loads() it
    and fail loudly on their own.
    """
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and "error" in data


def _qlogin(creds, user, jar):
    """Refresh a Quotient cookie jar for one account (shared jar; newest login wins).

    curl writes a private temp file which is renamed over the shared jar: the jar is
    never observed half-written (`curl -c` truncates in place, so a concurrent reader
    could see a partial file), the 0600 mode survives, and a FAILED refresh leaves the
    previous jar intact instead of destroying a working session. Callers in the loops
    hold jar_lock(account); the lock here covers the one-off callers too.
    """
    with jar_lock(user):
        fd, tmp = tempfile.mkstemp(prefix=f".{Path(jar).name}.", dir=str(Path(jar).parent))
        os.close(fd)
        try:
            subprocess.run(["curl", "-s", "--max-time", "15", "-c", tmp, "-X", "POST",
                            f"http://{creds['ENGINE_IP']}/api/login",
                            "-H", "Content-Type: application/json",
                            "-d", json.dumps({"username": user,
                                              "password": creds[user.upper() + "_PW"]})],
                           capture_output=True)
            if os.path.getsize(tmp) > 0:
                os.chmod(tmp, 0o600)
                os.replace(tmp, jar)
                return True
        except OSError:
            pass
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False


def qget(creds, user, jar, path):
    """GET a Quotient path with the account's shared jar; re-login and retry once on rejection."""
    def _get():
        return subprocess.run(["curl", "-s", "--max-time", "15", "-b", jar,
                               f"http://{creds['ENGINE_IP']}{path}"],
                              capture_output=True, text=True)
    with jar_lock(user):
        r = _get()
        if not json_error(r.stdout):
            return r
        _qlogin(creds, user, jar)
        return _get()


def _team_tid(creds, user, jar, team):
    """Map a team name to Quotient's internal team ID via /api/teams.

    Raises ValueError on a payload that is not a team list. Without the isinstance guard
    an error body made `next(...)` raise StopIteration/TypeError from inside the monitor
    thread, which used to die silently (audit find D3).
    """
    r = qget(creds, user, jar, "/api/teams")
    try:
        teams = json.loads(r.stdout)
    except ValueError:
        raise ValueError(f"/api/teams is not JSON: {(r.stdout or '')[:80]!r}")
    if not isinstance(teams, list):
        raise ValueError(f"/api/teams returned {str(teams)[:80]}")
    tid = next((t.get("ID") for t in teams if isinstance(t, dict) and t.get("Name") == team), None)
    if tid is None:
        raise ValueError(f"team {team!r} is not registered in /api/teams")
    return str(tid)


def team_down(creds, team):
    try:
        return any(not s["up"] for s in parsed_status(creds, team))
    except Exception:
        return False


def services_to_rows(services):
    """Engine /api/services payload -> [{service, up, error}] (parsed_status + final capture).
    A team with no registered/scored services is a legitimate `null` body.

    `Last10Rounds` is ordered newest-first, and at >=8 teams a poll can land mid-round:
    rounds[0] is then the round in flight, with an empty `Checks` array that is not a
    verdict at all. Reading it as "no check passed" reports a phantom DOWN — which the
    soak's monitors, fire-tests and scoreboard snapshots would all have believed. So take
    the newest round that actually HAS checks; only when no round carries any is the
    service genuinely unmeasured, and unmeasured stays down (fail closed) rather than
    being quietly promoted to up."""
    rows = []
    for s in services or []:
        rounds = s.get("Last10Rounds") or []
        checks = next((r.get("Checks") for r in rounds if r.get("Checks")), None) or []
        up = bool(checks) and all(c.get("Result") for c in checks)
        err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
        rows.append({"service": s["ServiceName"], "up": bool(up), "error": err[:120]})
    return rows


def parsed_status(creds, team):
    """Parsed scoreboard for one team: [{service, up, error}]; raises on failure."""
    jar = jar_path(creds["RUN_DIR"], team)
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
        state = json.loads((comp / ".deploy_state.json").read_text())
        credlist = ";".join(f"{u}:{pw}" for u, pw in (state.get("box_creds") or {}).items())
        (wd / "scrim.env").write_text(
            f"ENGINE_IP={creds['ENGINE_IP']}\nMY_TEAM=team{n}\nMY_PW={creds[f'TEAM{n}_PW']}\n"
            f"MY_TID={tid}\nBOX_PW={creds['BOX_PW']}\nINJECT_PW={creds['INJECT_PW']}\n"
            f"KEY_PATH={creds['KEY_PATH']}\nVM_USER={creds['VM_USER']}\nBOX_USER={creds['BOX_USER']}\n"
            f"JAR={jar_path(run_dir, f'team{n}')}\n"
            f"CREDLIST=({credlist})\n")
        os.chmod(wd / "scrim.env", 0o600)
        for helper, body in (("mybox", mybox_script(comp)), ("scorch", scorch_script(comp)),
                             ("qlogin", QLOGIN), ("score.py", SCORE_PY), ("myscore", MYSCORE),
                             ("submit-inject", SUBMIT_INJECT)):
            p = wd / helper
            p.write_text(body)
            p.chmod(0o755)
        shutil_copy(comp / "packet.md", wd / "packet.md")
        (wd / "LOG.md").write_text(
            f"# Blue team {n} — defense log\n"
            "# Your after-action report goes in REPORT.md (an operator reads it afterwards)\n")
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
    # Blue's identity and where its deliverables live, recorded while the workdirs are being
    # made: teardown must not have to guess workdir names to collect them (INV5).
    record_blue_agents(args, run_dir)


def shutil_copy(src, dst):
    """Copy bytes and return the destination (so callers can chmod what they just wrote)."""
    dst.write_bytes(src.read_bytes())
    return dst


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
    scoring = ["HOW THE SCORER SEES YOUR BOXES — check with ./scorch after ANY change to sshd, a firewall, or a scored service:",
               "  ./scorch runs every scored check from the engine's vantage. The ssh pins log in as a credlist",
               f"  account (airship/blueteam) WITH A PASSWORD from the gateway 192.168.{tid}.1 — if you set",
               "  PasswordAuthentication no, the scored check itself fails and the pin shows DOWN forever even",
               "  though sshd is healthy locally. Safe hardening is a gateway-only exception:",
               f"    Match User airship Address 192.168.{tid}.1",
               "        PasswordAuthentication yes",
               "  (global PasswordAuthentication no), then sshd -t, reload ssh, and ./scorch must show the ssh pin UP."]
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

{chr(10).join(scoring)}

SCOREBOARD + INJECTS — Quotient allows ONE session per account, so NEVER log in
directly (that kills the shared jar's session). Use the jar; if a call answers
{{"error":"Forbidden"}}, run ./qlogin once and retry:
  source ./scrim.env
  curl -s -b "$JAR" http://$ENGINE_IP/api/services/$MY_TID | python3 -m json.tool
  curl -s -b "$JAR" http://$ENGINE_IP/api/injects | python3 -c "import json,sys;[print(i['ID'],i['Title'],'due',i['DueTime'][11:16],'subs',len(i.get('Submissions') or [])) for i in json.load(sys.stdin)]"
  mkdir -p submissions && echo "## deliverable" > submissions/sub-<injectId>.md \
    && ./submit-inject <injectId> submissions/sub-<injectId>.md   # submit BEFORE close time

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
AFTER-ACTION REPORT: when you conclude — at the end of your last cycle, or as soon as you
   know you cannot continue — write REPORT.md in your workdir. An operator reads it after
   the event to grade the defence, so make it self-contained: what you found, what you
   restored or fixed and how long each took, which injects you submitted, what you could
   not do, and what you would change about this competition.
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
        if json_error(out):
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
    jar = jar_path(creds["RUN_DIR"], team)
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
            r = run_cycle_with_retry(args, prompt, wd, env, llm_lock, stop, team=n)
            out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
            if r.returncode != 0:
                out += _opencode_log_tail(wd)
            (cycles / f"cycle-T+{elapsed:03d}.prompt.txt").write_text(prompt)
            (cycles / f"cycle-T+{elapsed:03d}.output.log").write_text(out)
            with (wd / "feed.log").open("a") as f:
                f.write(f"\n===== cycle T+{elapsed} rc={r.returncode} =====\n{out[-2000:]}\n")
            log(f"blue-team{n} cycle T+{elapsed} rc={r.returncode}")
        except subprocess.TimeoutExpired:
            write_cycle_timeout(wd, elapsed)
            log(f"blue-team{n} cycle T+{elapsed} TIMED OUT")
        except Exception as e:
            log(f"blue-team{n} feed error: {e}")
        took = time.time() - started
        stop.wait(min(600, max(30, CYCLE_TARGET_PERIOD - took)))


def write_cycle_timeout(wd, elapsed):
    """Record the exact header scrim-report.py counts as a timeout.

    Always written from the timeout path — including when the RETRY is the attempt that
    timed out. The report's `timeouts == 0` rehearsal gate finds this string and nothing
    else, so a timeout that skips it passes the gate silently.
    """
    with (Path(wd) / "feed.log").open("a") as f:
        f.write(f"\n===== cycle T+{elapsed} TIMEOUT =====\n")


def run_cycle_with_retry(args, prompt, wd, env, llm_lock, stop, team=None):
    """One blue cycle, at most two attempts, ONE LOCK ACQUISITION PER ATTEMPT.

    The lock exists to cap concurrency on a shared LLM endpoint. It used to wrap the
    attempt AND its retry, so two hung attempts held it for up to 2 x CYCLE_TIMEOUT
    (1800s each) and starved every other team on the same endpoint of its whole cycle.
    Releasing between attempts lets a waiter run while this team is between attempts;
    the cap on concurrent calls is unchanged. The retry is skipped once the stop event
    is set, so teardown is never held up by a doomed second attempt.
    """
    who = team if team is not None else "?"
    with llm_lock:
        r = _opencode_run(args, prompt, wd, env)
    if r.returncode != 0 and not stop.is_set():
        log(f"blue-team{who} cycle rc={r.returncode} — retrying once (lock released)")
        with llm_lock:
            r = _opencode_run(args, prompt, wd, env)
    return r


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
                    r = run_tree(base + [host, "bash -s"], timeout=60, check=False,
                                 stdin_text=watchdog_script(box_services[b["name"]],
                                                           creds["BOX_PW"]))
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
        # Context manager: the log append used to be a bare open(...).write(...) with no
        # close, leaking one file descriptor per MONITOR_INTERVAL for the whole event
        # (audit find D5).
        with (Path(args.run_dir) / "monitor.log").open("a") as f:
            f.write(f"\n#### T+{t_plus // 60}min {time.strftime('%H:%M')}\n" +
                    "\n".join(f"{t}:\n{s}" for t, s in texts.items()))
        log("monitor snapshot written")
        if pull_red_snapshot(args, f"T+{t_plus // 60:03d}"):
            log("red events.jsonl snapshot pulled")
        else:
            log("WARNING: red events.jsonl snapshot unavailable")
        red_llm_watch(args, t_plus)
        stop.wait(MONITOR_INTERVAL)


def _red_ssh_spec(args):
    """`(ssh spec, common scp/ssh args)` for red01 — the one place its address is resolved.

    Two callers, one construction: `_red_ssh_ctx` (the harness's own scp/ssh) and
    `record_red_agent` (the `ssh` record in test.json that the teardown-time collector
    dials). They must not be able to disagree — a collector pointing at a different address
    fails silently as `unreachable`, and the manifest is the only per-run source of truth
    because bad-auto's config.yaml is a rewritten singleton."""
    red_ip = getattr(args, "red_ip", None) or "10.0.0.198"
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
    return {"user": user, "host": red_ip, "key": key, "jump": jump}, common


def _red_ssh_ctx(args):
    """(target, common ssh/scp args, engine jump ProxyCommand or None) for red01."""
    spec, common = _red_ssh_spec(args)
    return f"{spec['user']}@{spec['host']}", common, spec["jump"]


def pull_red_snapshot(args, tag=None):
    """Best-effort in-run events.jsonl pull from red01. Never raises."""
    ev = Path(args.run_dir) / "evidence" / "red"
    ev.mkdir(parents=True, exist_ok=True)
    target, common, jump = _red_ssh_ctx(args)
    dest = ev / "events.jsonl"
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            r = run_tree(["scp"] + common + extra +
                         [f"{target}:/var/lib/bad-auto/events.jsonl", str(dest)],
                         timeout=75, check=False)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            secure_evidence(dest)
            if tag:
                secure_evidence(shutil_copy(dest, ev / f"events-{tag}.jsonl"))
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

    def restart(self):
        """Tear down and respawn the tunnel — the one-shot rescue for a WEDGED process.

        _supervise only notices a proc that has exited; an ssh that is still running but
        no longer forwarding traffic (the dress-rehearsal failure: "a plain ssh -R died
        and nobody noticed") needs an explicit kill+respawn. SIGTERM the session, then
        SIGKILL after a short grace so a hung ssh cannot hold the forwarded port and
        make the respawn fail with ExitOnForwardFailure.
        """
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=10)
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
            r = run_tree(["ssh"] + common + extra + [target, probe],
                         timeout=45, check=False)
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


def alerts_path(run_dir):
    """Run-dir alert journal (jsonl) that scrim-report.py surfaces."""
    return Path(run_dir) / "evidence" / ALERTS_FILENAME


def write_alert(run_dir, kind, detail, **fields):
    """Append one durable, 0600 alert line; returns the record.

    A lone log() line was the entire record that red spent an event LLM-less (the
    dress rehearsal), because stdout scrolls away and the post-hoc report never reads
    it. Alerts land in the run dir, where the report can count and print them.
    """
    path = alerts_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, "detail": detail,
           **fields}
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    secure_evidence(path)
    return rec


def restart_red_llm_tunnel(args):
    """Make ONE rescue attempt on the red-side LLM path; returns a detail string.

    Only a run with a managed reverse tunnel can be rescued from here: with a direct
    (openrouter) endpoint the path is red01's own egress and there is nothing local to
    respawn — say so instead of pretending to have acted.
    """
    tunnel = getattr(args, "red_tunnel", None)
    if not hasattr(tunnel, "restart"):
        return ("no managed reverse tunnel for this endpoint (red egresses directly) — "
                "red stays blind until the endpoint itself recovers")
    try:
        tunnel.restart()
    except Exception as e:
        return f"tunnel respawn FAILED: {type(e).__name__}: {e}"
    return f"reverse tunnel respawned (red01 localhost:{tunnel.remote_port})"


def red_llm_watch(args, t_plus):
    """In-event from-red01 LLM probe: per-endpoint hysteresis plus one rescue per episode.

    The old watcher tracked a single module-global timestamp with no endpoint key and no
    failure count, so it printed one line and never acted — red continued blind for the
    rest of the event (the exact failure the RedTunnel docstring records as having cost a
    dress rehearsal). Here each endpoint carries (first_failure, consecutive_failures,
    restarted): a single blip only logs, RED_LLM_FAIL_THRESHOLD consecutive failures
    trigger ONE tunnel restart and a durable alert, and recovery clears the episode so a
    later outage can be rescued again.
    """
    url = red_llm_url(args)
    st = _llm_watch.setdefault(url, {"since": None, "failures": 0, "restarted": False})
    if check_red_llm(args, url):
        if st["failures"]:
            mins = (time.time() - (st["since"] or time.time())) / 60
            log(f"red LLM reachable again after {mins:.0f} min "
                f"({st['failures']} failed probe(s))")
            write_alert(args.run_dir, "red_llm_recovered",
                        f"red01 can reach {url} again after {mins:.0f} min",
                        endpoint=url, failed_probes=st["failures"])
        st.update({"since": None, "failures": 0, "restarted": False})
        return
    st["failures"] += 1
    if st["since"] is None:
        st["since"] = time.time()
    if st["failures"] == 1:
        tunnel = getattr(args, "red_tunnel", None)
        dead = hasattr(tunnel, "proc") and tunnel.proc is not None and tunnel.proc.poll() is not None
        log(f"WARNING: T+{t_plus // 60} — red01 cannot reach the LLM endpoint ({url})"
            + (" (tunnel process dead; supervisor will respawn it)" if dead else "")
            + " — red is making decisions blind until this recovers")
        return
    if st["failures"] < RED_LLM_FAIL_THRESHOLD or st["restarted"]:
        return
    st["restarted"] = True
    detail = restart_red_llm_tunnel(args)
    log(f"WARNING: T+{t_plus // 60} — red01 missed {st['failures']} consecutive LLM probes; "
        f"{detail}")
    write_alert(args.run_dir, "red_llm_restart",
                f"red01 missed {st['failures']} consecutive LLM probes to {url}; {detail}",
                endpoint=url, failed_probes=st["failures"],
                down_since=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st["since"])))


def verify_red_reaches_teams(args, comp, creds):
    """Pre-T0 gate: red01 must be able to dial a box on EVERY team.

    scale8-soak-2026-10-02: routed red01 could not reach any satellite team box — all
    15 cred_sprays and both db_attacks failed "unreachable over SSH" — and the run went
    40 minutes before anyone noticed, because nothing checked red's path before T0 and
    `verify --red-identity` only proved red reached one box on its own segment. Red
    spent the event scouting a network half of which it could not touch.

    Raises (stopping the run before T0) rather than warning: a range where red cannot
    reach half the teams is not an event worth starting. Masq-mode red shares the team
    gateways and has no path of its own to prove, so it is skipped."""
    if (args.red_mode or "routed") == "masq":
        log("--red-mode masq: red shares the team gateways, skipping the reachability gate")
        return
    log("pre-T0: red01 -> every team reachability gate")
    run(["python3", "verify-competition.py", str(comp.relative_to(REPO)),
         "--engine-ip", creds["ENGINE_IP"], "--admin-password", creds["ADMIN_PW"],
         "--red-ip", args.red_ip,
         "--red-identity", "--red-teams", "all",
         *_verify_flags(comp)],
        cwd=REPO, timeout=900, check=True, tail=25)


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
    # Atomic: a torn config.yaml would break every subsequent badauto call, and the
    # file may name an internal endpoint.
    write_text_atomic(BAD_AUTO / "config.yaml", json.dumps(cfg, indent=2), mode=0o600)
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
        # Atomic, same reason as above.
        write_text_atomic(BAD_AUTO / "config.yaml", json.dumps(cfg, indent=2), mode=0o600)
        args.red_tunnel = tunnel
    red_mode = args.red_mode or "routed (bad-auto default)"
    log(f"deploying red01 at {args.red_ip} (storage {args.red_storage}, mode {red_mode})")
    run(["python3", "-m", "badauto", "deploy", "--competition", str(comp.resolve()), "--start"],
        cwd=BAD_AUTO, env=env, timeout=1800)
    # Record red01's identity now, while it is known: bad-auto's config.yaml is a rewritten
    # singleton, so a later reader of it can name ANOTHER run's red01 — the manifest is the
    # only per-run source of truth the collector may dial (INV5). Recorded before the LLM
    # gate below so a run that dies there still lets teardown collect from red01.
    record_red_agent(args)

    red_base = cfg["llm"]["base_url"]
    if not check_red_llm(args, red_base):
        raise RuntimeError(
            f"red01 cannot reach the LLM endpoint ({red_base}) — refusing to start the event "
            f"red-LLM-less. Run a socat relay on this host and/or the reverse tunnel "
            f"(--red-tunnel), then re-run. See docs/scrim-harness.md, 'stage_red (LLM gate)'.")
    log(f"red01 reached the LLM at {red_base} — clear to start")
    # Second pre-T0 gate, same reasoning as the LLM one above: red that cannot dial the
    # teams cannot attack them, and the soak burned 40 minutes finding that out.
    verify_red_reaches_teams(args, comp, creds)


class WorkerDiedError(RuntimeError):
    """A monitor/feed/watchdog worker thread stopped on its own during the event.

    The event itself keeps running (the surviving teams still need their feeds and the
    watchdog is still keeping services up), but the harness must not report a clean run:
    a dead feed thread means that team's agent was unattended, and a dead monitor thread
    means the evidence the post-hoc report depends on simply is not there (audit find D3).
    """


# A worker may be parked inside a full opencode cycle when the window closes; 10s used to
# abandon it, and stage_capture/stage_teardown then wrote into (and destroyed infra under)
# a live cycle. One CYCLE_TIMEOUT plus slack is the honest bound (the retry is skipped
# once stop is set, so a single attempt is the worst case).
WORKER_JOIN_BUDGET = CYCLE_TIMEOUT + 120


def dead_workers(threads, reported):
    """Threads that stopped without being asked to, excluding already-reported ones.

    Liveness is polled rather than assumed: nothing observed these threads before, so a
    worker killed by a StopIteration on an error body (or any other exception escaping
    its own handler) left the harness running to completion with no indication that its
    monitoring had stopped.
    """
    return [t for t in threads if not t.is_alive() and t not in reported]


def join_workers(threads, budget=None):
    """Join every worker against ONE shared deadline; return the still-alive ones.

    Budget is resolved at call time so tests (and an operator override) can shorten it.
    """
    budget = WORKER_JOIN_BUDGET if budget is None else budget
    deadline = time.time() + budget
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.time()))
    return [t for t in threads if t.is_alive()]


def supervise_workers(threads, deadline, stop, poll=60, t0=None):
    """Poll worker liveness until `deadline`, then stop and join them all.

    Returns the workers that died on their own or refused to stop within the join budget.
    An empty list means every worker ran to the end of the window and shut down cleanly.
    """
    reported = []
    try:
        while time.time() < deadline:
            time.sleep(poll)
            for t in dead_workers(threads, reported):
                when = f" at T+{int((time.time() - t0) // 60)}min" if t0 else ""
                reported.append(t)
                log(f"ERROR: worker thread {t.name!r} DIED{when} — whatever it was "
                    f"supervising is now unattended")
    finally:
        stop.set()
        for t in join_workers(threads):
            log(f"ERROR: worker thread {t.name!r} did not stop within "
                f"{WORKER_JOIN_BUDGET}s of the event window closing — capture/teardown "
                f"would have raced it")
            reported.append(t)
    return reported


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
        threads.append(threading.Thread(target=blue_feed_loop, name=f"blue-feed-{n}",
                                        args=(n, args, creds, t0, stop,
                                              endpoint_locks[base_url], 300.0 * (n - 1))))
    threads.append(threading.Thread(target=monitor_loop, name="monitor",
                                    args=(args, creds, t0, stop)))
    if getattr(args, "blue_watchdog", False):
        threads.append(threading.Thread(target=blue_watchdog_loop, name="blue-watchdog",
                                        args=(args, creds, t0, stop)))
    for t in threads:
        t.start()
    reported = supervise_workers(threads, t0 + args.duration_min * 60, stop, t0=t0)
    if reported:
        raise WorkerDiedError(
            "worker thread(s) stopped or hung during the event: "
            + ", ".join(t.name for t in reported)
            + " — that team's feed/monitoring was not running; treat the evidence as partial")


def run_event_and_finish(args, creds, t0):
    """stage_run -> capture -> teardown -> finalize, capturing even when a worker died.

    Teardown MUST still run (the range is expensive and a live cycle can be destroyed
    under it otherwise), but the harness must not exit clean afterwards: the failure is
    returned so main() can exit non-zero after the evidence is safely on disk.
    """
    failure = None
    try:
        stage_run(args, creds, t0)
    except WorkerDiedError as e:
        failure = str(e)
    stage_capture(args, creds)
    stage_teardown(args, creds)
    # Last, and after teardown, so it also runs when --keep-range skipped the range
    # destroy: stubs + REPORT.md + index.json make the folder a standard test artifact
    # either way. Wrapped, because reporting must never turn a finished run into a crash
    # (the destroy has already happened by now).
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        log("WARNING: no test folder on this run — test artifacts not finalized")
    else:
        try:
            result = artifacts_ops.finalize(test_dir)
            for warning in (result or {}).get("warnings") or []:
                log(f"WARNING: {warning}")
            log(f"test artifacts finalized: {test_dir}")
        except Exception as e:
            log(f"WARNING: could not finalize test artifacts in {test_dir}: {e}")
    return failure


def capture_engine_evidence(ev):
    """Copy the engine-side capture into `ev/engine/` (created here); return what was written.

    A copy, never a move: scrim-report.py reads `evidence/final-scoreboard.json` at that
    exact path (scrim-report.py:193-199), so relocating the engine dump would silently zero
    the report's scoreboard section."""
    ev = Path(ev)
    engine_dir = ev / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    srcs = [p for p in (ev / "final-scoreboard.json", ev / "scoreboard-state.jsonl")
            if p.is_file()]
    srcs += [p for p in sorted(ev.glob("final-services-*.json")) if p.is_file()]
    return [secure_evidence(shutil_copy(src, engine_dir / src.name)) for src in srcs]


def capture_blue_evidence(run_dir, ev, teams):
    """Seal each blue workdir's deliverables under `ev/blue-team<n>/`; return what was written.

    The submission globs are `sub*.md`/`sub*.txt`, NOT `sub-*.md`: the cycle prompt tells
    blue to write `sub.md`, so the hyphenated pattern matched nothing and observed runs
    counted 0 inject submissions with an empty `submissions/` (plan §3.2). `REPORT.md` is
    blue's after-action report and is copied for the same reason it is asked for."""
    run_dir, ev = Path(run_dir), Path(ev)
    written = []
    for n in range(1, teams + 1):
        src = run_dir / f"blue-team{n}"
        dst = ev / f"blue-team{n}"
        if not src.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("LOG.md", "NOTEBOOK.md", "feed.log", "REPORT.md"):
            if (src / name).exists():
                written.append(secure_evidence(shutil_copy(src / name, dst / name)))
        for pattern in ("sub*.md", "sub*.txt"):
            for f in src.glob(pattern):
                written.append(secure_evidence(shutil_copy(f, dst / f.name)))
        for sub in ("submissions", "cycles"):
            if (src / sub).is_dir():
                subprocess.run(["cp", "-r", str(src / sub), str(dst / sub)], check=False)
    log("blue evidence collected")
    return written


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
        if json_error(r.stdout):
            r = qget(creds, team, jar_path(args.run_dir, team), path)
        write_evidence(ev / f"final-services-{team}.json", r.stdout or "")
        try:
            final["services"][team] = services_to_rows(json.loads(r.stdout))
        except Exception:
            final["services"][team] = None
            log(f"WARNING: {team} services capture failed: {(r.stdout or '')[:120]}")
    write_evidence(ev / "final-scoreboard.json", json.dumps(final, indent=1))
    log("final scoreboard dumped to evidence")

    def _pause():
        """POST engine/pause. Returns (ok, detail) — the caller must not claim success.

        The old version ignored both the return code and the body and logged "engine
        paused (best-effort)" unconditionally: a 500 mid-round left scoring running while
        the operator read a line saying it had stopped (audit find D7).
        """
        r = run_tree(["curl", "-s", "--max-time", "15", "-b", admin_jar, "-X", "POST",
                      f"http://{creds['ENGINE_IP']}/api/engine/pause",
                      "-H", "Content-Type: application/json", "-d", '{"pause": true}'],
                     timeout=30, check=False)
        if r.returncode != 0:
            return False, f"curl rc={r.returncode}: {(r.stderr or '').strip()[-120:]}"
        if json_error(r.stdout):
            return False, (r.stdout or "")[:120]
        return True, ""

    # Refresh the admin session before pausing: the capture loop only re-logs-in after a
    # rejected call, so a jar that expired between the last capture and the pause would
    # otherwise silently spend the first attempt on an unauthenticated POST.
    _qlogin(creds, "admin", admin_jar)
    paused, why = _pause()
    if not paused:
        _qlogin(creds, "admin", admin_jar)
        paused, why = _pause()
    if paused:
        log("engine paused; services JSON captured")
    else:
        log(f"ERROR: engine pause FAILED ({why}) — scoring is still running; pause it by "
            f"hand before assuming the event is over")
    sb = Path(args.run_dir) / "scoreboard-state.jsonl"
    if sb.exists():
        secure_evidence(shutil_copy(sb, ev / "scoreboard-state.jsonl"))
    # The engine capture is also copied into its own subtree (INV8) — a copy, because
    # scrim-report.py reads evidence/final-scoreboard.json where the harness wrote it.
    log(f"engine capture copied to {ev / 'engine'} "
        f"({len(capture_engine_evidence(ev))} file(s))")
    capture_blue_evidence(args.run_dir, ev, args.teams)


def _config_red_vmid():
    """The red vmid bad-auto's destroy will actually act on: config.yaml's deploy.red_vmid.

    `badauto destroy` follows config.yaml by design (cmd_destroy refuses to be a silent
    target override), so this is the number to reconcile `--red-vmid` against."""
    try:
        cfg = json.loads((BAD_AUTO / "config.yaml").read_text())
        return (cfg.get("deploy") or {}).get("red_vmid")
    except (OSError, ValueError):
        return None


def _red_vm_still_exists(vmid):
    """True when `vmid` is still present in the cluster.

    None (could not ask) is treated as "not proven gone" by the caller — this is a
    teardown assertion, so an unverifiable check must not read as success."""
    if not vmid:
        return None
    try:
        from range_ops import live_vmids
        return int(vmid) in live_vmids()
    except Exception as e:                                  # noqa: BLE001 - reported
        log(f"WARNING: could not verify red01 vmid {vmid} is gone: {e}")
        return None


def teardown_red(args, env):
    """Destroy red01 + its NAT, then PROVE the VM is gone.

    scale8 soak 2026-10-02: red01 (998) survived `badauto destroy` and had to be
    destroyed by hand. The stage ran with check=False and never looked at the result, so
    a destroy that removed nothing was indistinguishable from one that worked — the
    driver printed DONE while a red box with a live LLM key and beacon tasking stayed
    up on the range. Both halves are needed: surface the exit code, and assert the
    specific vmid this run deployed."""
    log("teardown: red01 + NAT")
    configured = _config_red_vmid()
    if args.red_vmid and configured and int(configured) != int(args.red_vmid):
        # Not fatal — badauto's identity guards are the authority on what is safe to
        # delete — but it is exactly the stale-config shape that lost red01, so say it.
        log(f"WARNING: config.yaml's deploy.red_vmid is {configured} but this run deployed "
            f"{args.red_vmid}; badauto destroy follows config.yaml")
    proc = run(["python3", "-m", "badauto", "destroy", "--competition", args.competition,
                "--yes"],
               cwd=BAD_AUTO, env=env, timeout=900, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"badauto destroy failed (rc={proc.returncode}) — red01 and its engine NAT "
            f"rules may still be up. Do not treat this run as torn down; fix and re-run "
            f"`python3 -m badauto destroy --competition {args.competition} --yes` in "
            f"{BAD_AUTO}, or destroy vmid {args.red_vmid} by hand and re-run "
            f"destroy-competition.py.")

    vmid = args.red_vmid or configured
    still = _red_vm_still_exists(vmid)
    if still:
        raise RuntimeError(
            f"badauto destroy reported success but vmid {vmid} is STILL PRESENT — red01 "
            f"survived teardown. This is the scale8 soak's leak (red01 998 left running, "
            f"with the beacon controller and its LLM key, after the driver printed DONE). "
            f"Destroy vmid {vmid} before starting another run on this range.")
    if still is False:
        log(f"teardown: red01 vmid {vmid} confirmed gone")
    else:
        log(f"teardown: red01 vmid {vmid} could not be verified gone — check by hand "
            f"(badauto destroy exited 0)")


def stage_teardown(args, creds=None):
    tunnel = getattr(args, "red_tunnel", None)
    if hasattr(tunnel, "shutdown"):
        tunnel.shutdown()
        log("teardown: red LLM tunnel stopped")
    if creds:
        # The collector owns the red01 pull now — one implementation shared with
        # destroy-competition.py (artifacts_ops.py's docstring: two callers must agree) —
        # and it MUST run before `badauto destroy` below, which erases red01. It never
        # raises: an unreachable red01 is recorded as `unreachable` in collection.json.
        collect_run_artifacts(args)
        write_interaction_report(args)
    log("teardown: red01 + NAT")
    env = {**os.environ, "BAuto_LLM_API_KEY": api_key(local=True)}
    teardown_red(args, env)
    if args.keep_range:
        log("--keep-range: leaving the competition range up")
        return
    log("teardown: competition range")
    run(["python3", "destroy-competition.py", "--competition", args.competition, "--yes"],
        cwd=REPO, timeout=3600)


# ── the run's test folder (artifacts_ops.py owns the format and the vocabulary) ──────────

BOXES_JSON = "boxes.json"


def _box_names(comp):
    """Box names from the comp's boxes.json; [] when it is absent or unreadable.

    Recorded in test.json so a teardown-time reader knows what the run covered without
    re-reading a comp dir a later deploy may have rewritten."""
    try:
        boxes = json.loads((Path(comp) / BOXES_JSON).read_text())
    except (OSError, ValueError):
        return []
    return [b.get("name") for b in boxes if isinstance(b, dict) and b.get("name")]


def resolve_run_dir(comp, args):
    """Open this run's test folder and return `(run_dir, test_dir)`.

    The default run dir IS the test folder — `competitions/<comp>/.automated-tests/<key>`
    with `key = artifacts_ops.test_key(comp)` (the run id when the deploy has one) — so the
    harness writes run.json/T0.txt/blue-team*/evidence straight into the folder the
    collector and the reports live in, instead of a post-hoc move out of a comp-keyed
    `scrim-runs/<comp>` dir. `--run-dir <path>` remains the debugging escape hatch: the run
    writes there and the test folder records `paths.run_dir` so the collector still finds
    the evidence.

    Resolution order, which is also the resume rule: an explicit `--run-dir` wins; else a
    `paths.run_dir` already recorded in test.json is authoritative; else the test folder.
    Both main() branches call this, so a fresh run and its `--resume-event` land in one
    folder — a resume never mints a second key or forks the evidence.
    """
    test_path, manifest = artifacts_ops.ensure_test(
        comp, kind="scrim", script="run-agent-scrim.py",
        teams=getattr(args, "teams", None), boxes=_box_names(comp),
        node=os.environ.get("TF_VAR_proxmox_node"),
        endpoint=os.environ.get("TF_VAR_proxmox_endpoint"))
    args.test_dir = str(test_path)
    explicit = getattr(args, "run_dir", None)
    recorded = (manifest.get("paths") or {}).get("run_dir")
    run_dir = Path(explicit or recorded or test_path)
    artifacts_ops.record_paths(test_path, run_dir=str(run_dir))
    return run_dir, test_path


def record_agent(args, side, **fields):
    """Merge one agent side's identity into test.json's `agents` map; return that side.

    Merged, never replaced: red is recorded in stage_red and blue in stage_blues, and a
    resume re-recording one side must not erase what the other side, or an earlier call,
    already knew (INV9)."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return {}
    manifest = artifacts_ops.load_manifest(test_dir)
    agents = manifest.get("agents") or {}
    agents[side] = {**(agents.get(side) or {}), **fields}
    artifacts_ops.update_manifest(test_dir, agents=agents)
    return agents[side]


def record_red_agent(args):
    """Write red01's identity, address and ssh route into test.json (INV5).

    Recorded in stage_red, where the values are first known, because nothing later can
    recover them honestly: bad-auto's config.yaml is a rewritten singleton, so a
    teardown-time reader of it can point at ANOTHER run's red01. `ssh` comes from the same
    helper `_red_ssh_ctx` builds its scp/ssh arguments from, so the harness and the
    collector cannot dial different addresses."""
    spec, _common = _red_ssh_spec(args)
    node = os.environ.get("TF_VAR_proxmox_node")
    return record_agent(args, "red", present=True, ip=spec["host"],
                        vmid=getattr(args, "red_vmid", None) or None, ssh=spec,
                        **({"node": node} if node else {}))


def record_blue_agents(args, run_dir):
    """Blue's identity and the paths its deliverables live at (INV5).

    Blue never runs on a guest box — its workdirs are operator-side — so the collector
    needs the paths, not just "present": a teardown that has to guess workdir names
    collects nothing. `engine_evidence` is recorded here too; stage_capture creates it."""
    record_agent(args, "blue", present=True, teams=getattr(args, "teams", None))
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return
    artifacts_ops.record_paths(
        test_dir,
        blue_workdirs=[str(Path(run_dir) / f"blue-team{n}")
                       for n in range(1, (getattr(args, "teams", 0) or 0) + 1)],
        engine_evidence=str(Path(run_dir) / "evidence" / "engine"))


def collect_run_artifacts(args):
    """Pull red01 + seal every local artifact into the test folder; never raises.

    This is the harness's half of the two-caller contract: destroy-competition.py runs the
    same collector for a run whose harness died, so the pull logic exists once
    (artifacts_ops.collect). It must run before `badauto destroy`, which erases red01; an
    unreachable box is recorded as `unreachable`, not raised, so a dead guest can never
    hold the destroy hostage."""
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        log("WARNING: no test folder on this run — artifact collection skipped")
        return None
    comp = REPO / "competitions" / args.competition
    try:
        collection = artifacts_ops.collect(
            test_dir,
            artifacts_ops.plan_targets(artifacts_ops.load_manifest(test_dir), comp_dir=comp))
    except Exception as e:
        log(f"WARNING: artifact collection failed: {e}")
        return None
    summary = (collection or {}).get("summary") or {}
    log("artifacts collected: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    return collection


def write_interaction_report(args):
    """Best-effort `scrim-report.py <run_dir>` + fold its verdict into test.json.

    Generating INTERACTION.md used to be a manual operator step, which is why the machine
    verdict the artifact folder wants was usually missing. Both halves warn and continue:
    reporting must never block the destroy."""
    # main() sets run_dir; the test-folder fallback keeps the default case (run dir == test
    # folder) working for any caller that only knows args.test_dir.
    run_dir = Path(getattr(args, "run_dir", None) or getattr(args, "test_dir", None) or ".")
    try:
        r = run(["python3", "scrim-report.py", str(run_dir)], cwd=REPO, timeout=300,
                check=False)
        if r.returncode != 0:
            log(f"WARNING: scrim-report.py exited {r.returncode} — "
                f"{run_dir / 'INTERACTION.md'} may be stale")
    except Exception as e:
        log(f"WARNING: scrim-report.py failed: {e}")
    test_dir = getattr(args, "test_dir", None)
    if not test_dir:
        return {}
    # ingest_verdict reads the test folder's OWN copy (evidence/harness/INTERACTION.md),
    # which the collection above cannot have taken: it ran before the report existed. Seal
    # the report where the collector would have put it so the verdict is readable (a later
    # teardown-time collection records it in collection.json with its hash).
    interaction = run_dir / "INTERACTION.md"
    if interaction.exists():
        try:
            artifacts_ops.seal_local_file(
                interaction, Path(test_dir) / "evidence" / "harness" / "INTERACTION.md")
        except OSError as e:
            log(f"WARNING: could not file INTERACTION.md into the test folder: {e}")
    try:
        return artifacts_ops.ingest_verdict(test_dir)
    except Exception as e:
        log(f"WARNING: could not ingest the scrim verdict: {e}")
        return {}


RUN_MANIFEST = "run.json"
RESUME_MIN_CYCLES = 2


def save_manifest(run_dir, fields):
    """Persist the run's intent (duration, retention, watchdog, phase) atomically, 0600."""
    write_state(Path(run_dir) / RUN_MANIFEST, fields)


def load_manifest(run_dir):
    """The run manifest, or {} when absent/unreadable (older run dirs have none)."""
    try:
        data = json.loads((Path(run_dir) / RUN_MANIFEST).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_phase(run_dir, args, phase, t0=None):
    """Write/refresh the run manifest; doubles as the pre-T0 marker for --resume-event.

    Also mirrors the marker into this run's test folder (test.json) when it has one, so the
    artifact reader can see how far a run got without cross-referencing run.json. run.json
    stays the harness's own file (`save_manifest`/`load_manifest` above), written exactly as
    before; `getattr` because callers without a test folder (unit tests, legacy run dirs)
    must keep working.
    """
    save_manifest(run_dir, {
        "competition": getattr(args, "competition", None),
        "teams": getattr(args, "teams", None),
        "duration_min": getattr(args, "duration_min", None),
        "t0": t0,
        "keep_range": bool(getattr(args, "keep_range", False)),
        "blue_watchdog": bool(getattr(args, "blue_watchdog", False)),
        "phase": phase,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    test_dir = getattr(args, "test_dir", None)
    if test_dir:
        artifacts_ops.record_phase(test_dir, phase, t0=t0)


def resume_intent(args, manifest):
    """Fold the ORIGINAL run's retention/watchdog intent into the resume invocation.

    That intent lived only in the first CLI invocation, so a resume without --keep-range
    tore down a range the original run was keeping up. OR semantics: a resume cannot
    silently drop an intent the first run had, and an operator who passes the flag on the
    resume still gets it.
    """
    args.keep_range = bool(args.keep_range or manifest.get("keep_range"))
    args.blue_watchdog = bool(args.blue_watchdog or manifest.get("blue_watchdog"))
    return args


def resume_window_ok(remaining_min, min_cycles=RESUME_MIN_CYCLES):
    """True when at least `min_cycles` blue cycles still fit inside the window.

    The feed loop stops issuing cycles at duration_min - 2, so that margin is required
    on top of the cycles themselves.
    """
    return remaining_min >= min_cycles * (CYCLE_TARGET_PERIOD / 60.0) + 2


def resume_refusal(remaining_min, force=False):
    """None when resuming is worthwhile, otherwise the message that refuses it.

    A driver that died at T+85 of a 90-min event left remaining=5: every loop exits
    immediately, the log claims "RESUME", nothing runs — and the run then marched on to
    capture and TEAR DOWN a range the original run may have meant to keep.
    """
    if force or resume_window_ok(remaining_min):
        return None
    return (f"--resume-event refused: {remaining_min:.0f} min left of the event window "
            f"(T0 + duration), fewer than {RESUME_MIN_CYCLES} blue cycles of "
            f"{CYCLE_TARGET_PERIOD // 60} min can still run. A resume now would log "
            f"re-entry, run nothing, and still capture + tear down. Pass --force-resume "
            f"to capture/tear down anyway.")


def main():
    # Start-up umask: every evidence file, jar and log this run creates is private unless
    # a call site explicitly widens it. Several evidence writes used to rely on the
    # operator's umask (audit find D5).
    os.umask(0o077)
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
    p.add_argument("--resume-event", action="store_true", dest="resume_event",
                   help="the driver died mid-event: skip staging entirely and re-run the blue "
                        "feeds + monitor + capture + teardown from the T0 recorded in "
                        "run_dir/T0.txt (badauto red keeps running on red01 regardless)")
    p.add_argument("--from-phase", dest="resume", type=int, default=None,
                   help="resume create-competition at this phase")
    p.add_argument("--force-resume", action="store_true", dest="force_resume",
                   help="with --resume-event: proceed even when too little of the event "
                        "window is left for a single useful cycle (capture + teardown only)")
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
    if args.resume_event:
        if not comp.is_dir():
            # resolve_run_dir opens/creates the test folder under comp, so a typo must not
            # mint a comp dir for a run that cannot exist.
            sys.exit(f"no such competition: {comp}")
        # The same folder the first run used: the test folder is keyed on this comp's run
        # id, and a paths.run_dir already recorded in test.json is authoritative, so a
        # resume can never mint a second key or fork the evidence (INV1).
        run_dir, test_dir = resolve_run_dir(comp, args)
        log(f"test folder: {test_dir}")
        manifest = load_manifest(run_dir)
        resume_intent(args, manifest)
        t0 = None
        t0_file = run_dir / "T0.txt"
        if t0_file.exists():
            rec = json.loads(t0_file.read_text().strip())
            t0 = float(rec["t0"])
            args.duration_min = int(rec.get("duration_min") or args.duration_min)
        elif manifest.get("t0"):
            t0 = float(manifest["t0"])
            args.duration_min = int(manifest.get("duration_min") or args.duration_min)
        args.run_dir = str(run_dir)
        if t0 is None:
            # No T0.txt and no manifest t0: the driver died before the event clock
            # started (almost always inside stage_red, which deploys red01 for tens of
            # minutes). There is nothing to re-enter, and saying so beats a bare
            # "T0.txt missing" now that the pre-stage_red marker exists.
            where = f" (manifest phase={manifest.get('phase')!r})" if manifest else ""
            sys.exit(f"--resume-event: no event clock in {run_dir} (T0.txt and run.json "
                     f"t0 both missing){where} — the run never reached T0, so there is no "
                     f"event to resume. Re-stage with --skip-deploy instead.")
        remaining = args.duration_min - (time.time() - t0) / 60
        refusal = resume_refusal(remaining, force=args.force_resume)
        if refusal:
            sys.exit(refusal)
        log(f"RESUME: re-entering stage_run at T+{int((time.time() - t0) / 60)}min "
            f"({remaining:.0f}min left; keep_range={args.keep_range}, "
            f"watchdog={args.blue_watchdog} from the manifest)")
        creds = creds_from_files(comp)
        creds["RUN_DIR"] = args.run_dir
        record_phase(run_dir, args, "event-resumed", t0=t0)
        failure = run_event_and_finish(args, creds, t0)
        log("DONE — resumed event captured and torn down")
        if failure:
            sys.exit(f"FAILED: {failure}")
        return
    if args.new:
        args.competition = args.new
        comp = REPO / "competitions" / args.new
        stage_author(args)
    if not (comp / "Compfile").exists():
        sys.exit(f"no such competition: {comp}")

    run_dir, test_dir = resolve_run_dir(comp, args)
    run_dir.mkdir(parents=True, exist_ok=True)
    args.run_dir = str(run_dir)
    log(f"run dir: {run_dir}")
    log(f"test folder: {test_dir}")

    if not args.skip_deploy:
        stage_deploy(args, comp)
    creds = creds_from_files(comp)
    creds["RUN_DIR"] = args.run_dir
    log(f"engine {creds['ENGINE_IP']}, teams {[(k, v['identifier']) for k, v in json.loads((comp / 'teams.json').read_text()).items()]}")

    if not (comp / "packet.md").exists():
        log("packet.md missing — generating")
        run(["python3", "generate-packet.py", str(comp)], cwd=REPO, timeout=120)

    stage_verify(args, comp, creds)
    stage_blues(args, comp, run_dir, creds, time.time())

    red_setup_started = time.time()
    # Marker BEFORE stage_red: red01 deployment takes tens of minutes, and a driver that
    # dies in there used to leave nothing but a missing T0.txt for --resume-event (D3).
    record_phase(run_dir, args, "stage_red")
    stage_red(args, comp, creds, run_dir)
    t0 = time.time()
    write_state(run_dir / "T0.txt", {"t0": t0, "duration_min": args.duration_min})
    record_phase(run_dir, args, "event", t0=t0)
    log(f"T0 — event clock starts now (red setup took {(t0 - red_setup_started) / 60:.0f} min, "
        f"outside scored time)")
    reanchor_injects(args, comp, creds)

    failure = run_event_and_finish(args, creds, t0)
    test_dir = getattr(args, "test_dir", None) or run_dir
    log(f"DONE — reports + evidence in {test_dir}; REPORT.md there needs its judgement "
        f"sections filled, then: python3 test-artifacts.py verify {args.competition} "
        f"{Path(test_dir).name} --seal")
    if failure:
        sys.exit(f"FAILED: {failure}")


if __name__ == "__main__":
    main()
