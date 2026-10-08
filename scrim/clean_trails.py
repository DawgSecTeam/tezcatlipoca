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

Best-effort by design: a box that cannot be reached is logged and skipped. A
wipe is not allowed to fail a deploy — but it is also not allowed to be silent.
"""

import base64
import json
from pathlib import Path

from scrim import core, procs
from scrim.core import log

# What a deploy/plant/seed leaves behind on a Linux box. Truncation (not
# deletion) keeps the files' ownership/mode intact, so a blue reader sees an
# empty log rather than a missing one — a deleted file is its own tell.
LINUX_SCRIPT = r"""
set +e
for f in /var/log/auth.log /var/log/secure /var/log/syslog /var/log/messages \
         /var/log/apt/history.log /var/log/apt/term.log /var/log/dpkg.log \
         /var/log/yum.log /var/log/cloud-init.log /var/log/cloud-init-output.log; do
  [ -f "$f" ] && : > "$f" 2>/dev/null
done
rm -f /var/log/apt/history.log.* /var/log/dpkg.log.* /var/log/apt/*.gz 2>/dev/null
rm -f /var/log/cloud-init.log.* /var/log/cloud-init-output.log.* 2>/dev/null
# session/last-login records
for f in /var/log/wtmp /var/log/btmp /var/log/lastlog /var/log/faillog; do
  [ -f "$f" ] && : > "$f" 2>/dev/null
done
# shell histories (deploy commands are run as root and as the box user)
for h in /root/.bash_history /home/*/.bash_history /root/.python_history \
         /root/.mysql_history /root/.psql_history; do
  [ -f "$h" ] && : > "$h" 2>/dev/null
done
# staging leftovers from the deploy/seed
rm -rf /tmp/.ba-* /tmp/ba /tmp/bad-auto* /tmp/nakon* /tmp/.imix* /tmp/.badauto* \
       /tmp/tezcatlipoca* /tmp/*.tar.gz 2>/dev/null
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
WINDOWS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
Remove-Item -Force -Recurse -Path "$env:SystemRoot\Temp\*" | Out-Null
Remove-Item -Force -Recurse -Path "$env:SystemRoot\Panther\*" | Out-Null
Remove-Item -Force -Recurse -Path "$env:SystemRoot\Setup\Scripts\*" | Out-Null
Remove-Item -Force -Recurse -Path "$env:SystemRoot\System32\LogFiles\*" | Out-Null
Remove-Item -Force -Recurse -Path "$env:TEMP\*" | Out-Null
Remove-Item -Force -Path "$env:APPDATA\Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt" | Out-Null
if ($env:SCRIM_CLEAR_WIN_EVENTLOGS -eq '1') {
  wevtutil cl System; wevtutil cl Application; wevtutil cl Security
}
"TRAILS_WIN_OK"
"""


def creds_for_deploy(ctx):
    """The harness-shaped ssh creds dict, built from a deploy context."""
    return {"KEY_PATH": ctx.ssh_key_abs, "VM_USER": core.vm_username(),
            "ENGINE_IP": ctx.engine_mgmt_ip, "BOX_USER": ctx.box_username,
            "BOX_PW": ctx.box_password}


def _windows_boxes(comp):
    """Box names whose template is Windows (password auth as Administrator)."""
    try:
        boxes = core.read_comp_json(comp, "boxes.json")
    except Exception:                                        # noqa: BLE001 - best effort
        return set()
    return {b["name"] for b in boxes if core.is_windows_box(b)}


def box_targets(comp, creds):
    """[{ip, name, windows}] for the launchable team boxes (the in-path firewall
    is an appliance with no shell to clean; skipping it is not an omission)."""
    data = json.loads((Path(comp) / "targets.json").read_text())
    win = _windows_boxes(comp)
    out = []
    for t in data.get("targets", {}).values():
        name = t.get("box_name") or ""
        if name == "fw01":
            continue
        out.append({"ip": t["ip"], "name": name, "windows": name in win})
    return out


def _clean_linux(creds, ip):
    base = core.box_ssh_base(creds, connect_timeout=15)
    argv, stdin_text = procs.box_sudo_stdin(base, f"{creds['BOX_USER']}@{ip}",
                                            creds["BOX_PW"], LINUX_SCRIPT)
    r = procs.run_tree(argv, timeout=180, check=False, stdin_text=stdin_text)
    return r.returncode == 0 and "TRAILS_LINUX_OK" in (r.stdout or "")


def _clean_windows(creds, ip):
    b64 = base64.b64encode(WINDOWS_PS.encode("utf-16-le")).decode()
    remote = (f"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
              f"-EncodedCommand {b64}")
    base = core.box_ssh_base(creds, connect_timeout=15)
    argv = ["sshpass", "-e"] + base + [f"Administrator@{ip}", remote]
    # sshpass -e takes the password from the environment, never from argv
    # (a running command's argv is world-readable via ps).
    import os
    env = {**os.environ, "SSHPASS": creds.get("BOX_PW") or ""}
    r = procs.run_tree(argv, timeout=180, check=False, env=env)
    return r.returncode == 0 and "TRAILS_WIN_OK" in (r.stdout or "")


def clean_trails(comp, creds, targets=None):
    """Wipe deploy traces on every team box. Returns {ip: bool}; never raises."""
    targets = targets if targets is not None else box_targets(comp, creds)
    results = {}
    if not targets:
        log("trail wipe: no box targets — skipped")
        return results
    log(f"trail wipe: clearing deploy traces on {len(targets)} box(es) "
        f"(blue must not be able to read the plant out of the logs)")
    for t in targets:
        try:
            ok = _clean_windows(creds, t["ip"]) if t["windows"] else _clean_linux(creds, t["ip"])
        except Exception as e:                               # noqa: BLE001 - best effort
            ok, err = False, e
            log(f"trail wipe: {t['name']} ({t['ip']}) raised {type(e).__name__}: {err}")
        results[t["ip"]] = ok
        log(f"trail wipe: {t['name']} ({t['ip']}) {'cleaned' if ok else 'NOT cleaned'}")
    return results
