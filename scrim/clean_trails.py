"""Wipe the deploy's own traces from every team box, before blue ever looks.

The deploy, the nakon plant and the day-0 seed all reach each box over ssh as
root, and they leave the whole operation in the box's own logs: the sessions in
`auth.log`, the package installs in the apt/dpkg history, the unit and registry
writes in the journal and the Windows event log (SCM 7045 "service installed"),
the commands themselves in the shell histories. A blue agent that greps any of
those gets the plant handed to it — every service name, every path, every
callback — which is precisely what the randomized naming exists to prevent.

So this runs as the LAST tezcatlipoca step before the clock starts:

  * deploy phase 7, AFTER the plant/seed and BEFORE the `tz-ready` snapshot, so
    the restore point every box carries is clean too (a wipe after the snapshot
    would leave the traces restorable);
  * the agent harness, AFTER the day-0 seed and before red's director starts.

The wipe is scoped by red01's world.json first: every planted artifact path goes
on an explicit never-touch skip-list, because a decoy IS payload the operator
planted on purpose — the 2026-10-11 run lost one in this window (cause unproven;
the staging rm globs and the journal vacuum are the prime suspects). After the
wipe the artifact checks are re-run and anything that was alive before and is
dead after is named loudly. Best-effort by design: a box that cannot be reached
is logged and skipped. A wipe is not allowed to fail a deploy — but it is also
not allowed to be silent.
"""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from estate import state as estate_state
from scrim import core, procs
from scrim.core import log

# Windows wipe budget: 180 s ScrimTimeout-ed on BOTH Windows boxes live
# (2026-10-11) — Remove-Item -Recurse over Temp/LogFiles is simply slow.
WIN_WIPE_TIMEOUT = 600

# What a deploy/plant/seed leaves behind on a Linux box. Truncation (not
# deletion) keeps the files' ownership/mode intact, so a blue reader sees an
# empty log rather than a missing one — a deleted file is its own tell.
# $SKIP_PATHS/in_skip come from the caller-built skip block and guard every
# destructive line: a planted path must never match a wipe glob.
LINUX_SCRIPT = r"""
set +e
for f in /var/log/auth.log /var/log/secure /var/log/syslog /var/log/messages \
         /var/log/apt/history.log /var/log/apt/term.log /var/log/dpkg.log \
         /var/log/yum.log /var/log/cloud-init.log /var/log/cloud-init-output.log; do
  if [ -f "$f" ] && ! in_skip "$f"; then : > "$f" 2>/dev/null; fi
done
if ! in_skip "/var/log/apt"; then
  rm -f /var/log/apt/history.log.* /var/log/dpkg.log.* /var/log/apt/*.gz 2>/dev/null
  rm -f /var/log/cloud-init.log.* /var/log/cloud-init-output.log.* 2>/dev/null
fi
# session/last-login records
for f in /var/log/wtmp /var/log/btmp /var/log/lastlog /var/log/faillog; do
  if [ -f "$f" ] && ! in_skip "$f"; then : > "$f" 2>/dev/null; fi
done
# shell histories (deploy commands are run as root and as the box user)
for h in /root/.bash_history /home/*/.bash_history /root/.python_history \
         /root/.mysql_history /root/.psql_history; do
  if [ -f "$h" ] && ! in_skip "$h"; then : > "$h" 2>/dev/null; fi
done
# staging leftovers from the deploy/seed — expanded and filtered, never a blind
# rm: a decoy planted under /tmp must survive its own wipe
for g in /tmp/.ba-* /tmp/ba /tmp/bad-auto* /tmp/nakon* /tmp/.imix* /tmp/.badauto* \
         /tmp/tezcatlipoca* /tmp/*.tar.gz; do
  for p in $g; do
    if [ -e "$p" ] && ! in_skip "$p"; then rm -rf "$p" 2>/dev/null; fi
  done
done
# the journal records the unit writes and the service starts
journalctl --rotate >/dev/null 2>&1
journalctl --vacuum-time=1s >/dev/null 2>&1
echo TRAILS_LINUX_OK
"""

# Windows: the SCM event log (7045) records every service install, the
# PowerShell console history records the exact install scripts, and Panther/Setup
# keep the provisioning scripts. Clearing Security/System/Application wholesale
# is itself loud, so the payload-driven traces go and the event logs are
# truncated only when the operator asks (SCRIM_CLEAR_WIN_EVENTLOGS=1).
# $skip/Test-SkipPath come from the caller-built header.
WINDOWS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$wipeRoots = @("$env:SystemRoot\Temp", "$env:SystemRoot\Panther",
               "$env:SystemRoot\Setup\Scripts", "$env:SystemRoot\System32\LogFiles",
               $env:TEMP)
foreach ($rootPath in $wipeRoots) {
  Get-ChildItem -Force -Path $rootPath -ErrorAction SilentlyContinue |
    Where-Object { -not (Test-SkipPath $_.FullName) } |
    Remove-Item -Force -Recurse -ErrorAction SilentlyContinue
}
$psHistory = "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
if (-not (Test-SkipPath $psHistory)) {
  Remove-Item -Force -Path $psHistory -ErrorAction SilentlyContinue
}
if ($env:SCRIM_CLEAR_WIN_EVENTLOGS -eq '1') {
  wevtutil cl System; wevtutil cl Application; wevtutil cl Security
}
"TRAILS_WIN_OK"
"""

# Absolute-path tokens inside an artifact's detail (realm beacons record the
# binary path in `imix[transport] <bin> -> <dsn>`, not under `paths`).
_ABS_PATH_RE = re.compile(r"(?<![\w.-])(/[A-Za-z0-9_@%+=:.~^-][A-Za-z0-9_@%+=:.~^/-]*)")
_CHECK_CMD_MAX = 300          # shell length we are willing to relay to a box
RED01_WORLD = "/var/lib/bad-auto/world.json"


def creds_for_deploy(ctx):
    return {"KEY_PATH": ctx.ssh_key_abs, "VM_USER": core.vm_username(),
            "ENGINE_IP": ctx.engine_mgmt_ip, "BOX_USER": ctx.box_username,
            "BOX_PW": ctx.box_password}


def _windows_boxes(comp):
    try:
        boxes = core.read_comp_json(comp, "boxes.json")
    except Exception:                                        # noqa: BLE001 - best effort
        return set()
    return {b["name"] for b in boxes if core.is_windows_box(b)}


# fw01 skipped: the in-path firewall is an appliance with no shell to clean — skipping
# it is not an omission
def box_targets(comp, creds):
    data = json.loads((Path(comp) / "targets.json").read_text())
    win = _windows_boxes(comp)
    out = []
    for t in data.get("targets", {}).values():
        name = t.get("box_name") or ""
        if name == "fw01":
            continue
        out.append({"ip": t["ip"], "name": name, "windows": name in win})
    return out


# world.json is what the plant/seed recorded: its artifact paths are the never-touch
# set, and its per-artifact check_cmd is the integrity probe the post-wipe re-check
# re-runs. Best-effort: any failure logs and returns empty.
def red01_world_artifacts(comp, creds):
    state = estate_state.load_flat(comp)
    red_ip = (state.get("assume_breach") or {}).get("red_ip")
    key = creds.get("KEY_PATH")
    if not red_ip or not key:
        log("trail wipe: no red01 record (deploy state / key missing) — wipe runs "
            "WITHOUT an artifact skip-list")
        return [], None
    argv = ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
            f"sysadmin@{red_ip}", f"sudo -n cat {RED01_WORLD}"]
    try:
        r = procs.run_tree(argv, timeout=60, check=False)
    except Exception as e:                                   # noqa: BLE001 - best effort
        log(f"trail wipe: red01 world.json read failed ({type(e).__name__}) — "
            f"wipe runs WITHOUT an artifact skip-list")
        return [], None
    try:
        world = json.loads(r.stdout or "")
    except ValueError:
        log("trail wipe: red01 world.json did not parse — wipe runs WITHOUT an "
            "artifact skip-list")
        return [], None
    return world.get("artifacts") or [], red_ip


# newline-free (the skip blocks are line-oriented); anything else is dropped rather
# than mangled
def extract_artifact_paths(artifacts):
    out, seen = [], set()
    for a in artifacts or []:
        candidates = [p for p in (a.get("paths") or []) if isinstance(p, str)]
        candidates += (m.group(1) for m in _ABS_PATH_RE.finditer(a.get("detail") or ""))
        for p in candidates:
            p = p.strip()
            if len(p) < 4 or "\n" in p or p in seen:
                continue
            seen.add(p)
            out.append(p)
    return out


def _linux_skip_block(paths):
    safe = [p for p in paths if "\n" not in p]
    body = "\n".join(safe)
    return ("SKIP_PATHS=\"$(cat <<'TZ_SKIP'\n"
            f"{body}\n"
            "TZ_SKIP\n)\"\n"
            "in_skip() { printf '%s\\n' \"$SKIP_PATHS\" | grep -qxF -- \"$1\"; }\n")


def _windows_skip_header(paths):
    safe = [p.replace("'", "''") for p in paths if "\n" not in p]
    # commas BETWEEN entries only: PowerShell 5.1 rejects a trailing comma
    # before the closing paren ("Missing expression after ','", live 2026-10-12)
    entries = ",\n".join(f"'{p}'" for p in safe)
    return (f"$skip = @(\n{entries}\n)\n"
            "function Test-SkipPath($p) { return ($skip -contains $p) }\n")


def _run_windows_script(creds, ip, script, timeout=WIN_WIPE_TIMEOUT):
    # -EncodedCommand dies above the Windows shell's command-line limit (the 103-path
    # skip header pushed the UTF-16LE base64 past cmd.exe's 8191 chars: "The command
    # line is too long.", live 2026-10-12), and `-Command -` parses stdin LINE BY LINE
    # so any multi-line construct breaks ("Missing expression after ','"). A staged
    # .ps1 + -File has neither cliff. The script must stay ASCII (the skip-list
    # extraction already filters to a safe charset).
    base = core.box_ssh_base(creds, connect_timeout=15)
    env = {**os.environ, "SSHPASS": creds.get("BOX_PW") or ""}
    remote_path = "C:/Windows/Temp/.tz-wipe.ps1"
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                     encoding="ascii", errors="strict") as fh:
        fh.write(script)
        local = fh.name
    try:
        # base is an SSH argv (starts with "ssh"); scp takes the same options
        # with its own program name.
        scp_argv = ["sshpass", "-e", "scp"] + base[1:] + [
            local, f"Administrator@{ip}:{remote_path}"]
        scp = procs.run_tree(scp_argv, timeout=90, check=False, env=env)
        if scp.returncode != 0:
            return subprocess.CompletedProcess(
                ["scp"], scp.returncode, "", (scp.stderr or "scp failed")[-200:])
        # del first (`&` runs both, exit code comes from the LAST command) so the
        # returned rc is PowerShell's, not the cleanup's.
        return procs.run_tree(["sshpass", "-e"] + base + [
            f"Administrator@{ip}",
            f"cmd.exe /c del {remote_path} & "
            f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f"-File {remote_path}"],
            timeout=timeout, check=False, env=env)
    finally:
        os.unlink(local)


def _clean_linux(creds, ip, skip_paths=()):
    script = _linux_skip_block(list(skip_paths)) + LINUX_SCRIPT
    base = core.box_ssh_base(creds, connect_timeout=15)
    argv, stdin_text = procs.box_sudo_stdin(base, f"{creds['BOX_USER']}@{ip}",
                                            creds["BOX_PW"], script)
    r = procs.run_tree(argv, timeout=180, check=False, stdin_text=stdin_text)
    ok = r.returncode == 0 and "TRAILS_LINUX_OK" in (r.stdout or "")
    detail = "" if ok else (r.stderr or r.stdout or "").strip()[-200:]
    return ok, detail


def _clean_windows(creds, ip, skip_paths=()):
    script = _windows_skip_header(list(skip_paths)) + WINDOWS_PS
    r = _run_windows_script(creds, ip, script)
    ok = r.returncode == 0 and "TRAILS_WIN_OK" in (r.stdout or "")
    detail = "" if ok else (r.stderr or r.stdout or "").strip()[-200:]
    return ok, detail


# plant integrity

def _linux_integrity_script(arts):
    lines = []
    for i, a in enumerate(arts):
        cmd = (a.get("check_cmd") or "").strip().replace("\n", " && ")
        if cmd and len(cmd) <= _CHECK_CMD_MAX:
            lines.append(cmd)
        elif a.get("paths"):
            lines.append(f"test -e {shlex.quote(str(a['paths'][0]))}")
        else:
            lines.append("true")                             # uncheckable: rc 0, counted
        lines.append(f'echo "INTEG|{i}|$?"')
    return "\n".join(lines)


# path-existence probes only: PowerShell check_cmds are too shape-diverse to batch
# safely under one EncodedCommand; uncheckable artifacts report -1
def _windows_integrity_script(arts):
    lines = []
    for i, a in enumerate(arts):
        paths = [p for p in (a.get("paths") or []) if isinstance(p, str) and p]
        if paths:
            p = str(paths[0]).replace("'", "''")
            lines.append(f"if (Test-Path -LiteralPath '{p}') "
                         f"{{ Write-Output 'INTEG|{i}|0' }} "
                         f"else {{ Write-Output 'INTEG|{i}|1' }}")
        else:
            lines.append(f"Write-Output 'INTEG|{i}|-1'")
    return "\n".join(lines)


def plant_integrity(comp, creds, artifacts):
    win_names = _windows_boxes(comp)
    by_ip, tmap = {}, {t["ip"]: t for t in box_targets(comp, creds)}
    for idx, a in enumerate(artifacts or []):
        by_ip.setdefault(a.get("ip"), []).append((idx, a))
    results = {}
    for ip, rows in by_ip.items():
        t = tmap.get(ip)
        if not t:
            continue
        arts = [a for _i, a in rows]
        try:
            if t["windows"] or t["name"] in win_names:
                out = (_run_windows_script(creds, ip,
                                           _windows_integrity_script(arts),
                                           timeout=120).stdout or "")
            else:
                base = core.box_ssh_base(creds, connect_timeout=15)
                argv, stdin_text = procs.box_sudo_stdin(
                    base, f"{creds['BOX_USER']}@{ip}", creds["BOX_PW"],
                    _linux_integrity_script(arts))
                out = procs.run_tree(argv, timeout=120, check=False,
                                     stdin_text=stdin_text).stdout or ""
        except Exception as e:                               # noqa: BLE001 - best effort
            log(f"plant integrity: {t['name']} ({ip}) raised {type(e).__name__}: {e}")
            continue
        for line in out.splitlines():
            m = re.match(r"INTEG\|(\d+)\|(-?\d+)\s*$", line.strip())
            if m:
                idx, a = rows[int(m.group(1))]
                results.setdefault(ip, []).append(
                    (idx, a.get("kind"), int(m.group(2))))
    return results


def _integrity_report(pre, post, artifacts, phase):
    checked = sum(len(v) for v in post.values())
    alive = sum(1 for rows in post.values() for _i, _k, st in rows if st == 0)
    if phase != "post":
        return alive, checked, []
    alive_pre = {ip: {i for i, _k, st in rows if st == 0} for ip, rows in pre.items()}
    casualties = []
    for ip, rows in post.items():
        for idx, kind, st in rows:
            if st == 1 and idx in alive_pre.get(ip, set()):
                detail = (artifacts[idx].get("detail") or "")[:90] \
                    if idx < len(artifacts) else ""
                casualties.append((ip, kind, detail))
    return alive, checked, casualties


# planted paths go on the never-touch skip-list, and the plant is integrity-checked
# before AND after the wipe so a casualty is named in the log instead of surfacing as a
# mystery dead beacon at T+30; returns {ip: bool}, never raises
def clean_trails(comp, creds, targets=None, skip_paths=None, artifacts=None):
    targets = targets if targets is not None else box_targets(comp, creds)
    if artifacts is None and skip_paths is None:
        artifacts, _red_ip = red01_world_artifacts(comp, creds)
    if artifacts and skip_paths is None:
        skip_paths = extract_artifact_paths(artifacts)
    if skip_paths is None:
        skip_paths = []
    if skip_paths:
        log(f"trail wipe: {len(skip_paths)} planted path(s) on the never-touch "
            f"skip-list (red01 world.json)")
    pre = plant_integrity(comp, creds, artifacts) if artifacts else {}
    results = {}
    if not targets:
        log("trail wipe: no box targets — skipped")
        return results
    log(f"trail wipe: clearing deploy traces on {len(targets)} box(es) "
        f"(blue must not be able to read the plant out of the logs)")
    for t in targets:
        try:
            if t["windows"]:
                ok, detail = _clean_windows(creds, t["ip"], skip_paths)
            else:
                ok, detail = _clean_linux(creds, t["ip"], skip_paths)
        except Exception as e:                               # noqa: BLE001 - best effort
            ok, detail = False, f"{type(e).__name__}: {e}"
            log(f"trail wipe: {t['name']} ({t['ip']}) raised {type(e).__name__}: {e}")
        results[t["ip"]] = ok
        suffix = f" — {detail}" if (not ok and detail) else ""
        log(f"trail wipe: {t['name']} ({t['ip']}) {'cleaned' if ok else 'NOT cleaned'}"
            f"{suffix}")
    if artifacts:
        alive, checked, casualties = _integrity_report(
            pre, plant_integrity(comp, creds, artifacts), artifacts, "post")
        log(f"POST-WIPE plant integrity: {alive}/{checked} planted artifact "
            f"check(s) still alive")
        for ip, kind, detail in casualties:
            log(f"POST-WIPE CASUALTY: {ip} {kind} was alive before the wipe and is "
                f"dead after — the wipe killed it: {detail}")
        if checked == 0:
            log("POST-WIPE plant integrity: nothing checkable (artifacts carry "
                "neither check_cmd nor paths, or no box answered)")
    return results
