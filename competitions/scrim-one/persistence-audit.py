#!/usr/bin/env python3
"""Artifact-level audit of the seeded persistence — is each mechanism ACTUALLY live?

Reads the seed's world.json (the plant record) and checks every artifact on the box
itself: unit enabled+active, binary present/executable, file exists, key installed,
suid bit set, user exists, immutable flag set, mtime stomped, decoy present.
The agent's own report is not trusted for any of this.

Usage: persistence-audit.py <comp_dir> <world.json> [--boxes 192.168.120.4,...]
"""
import json
import re
import subprocess
import sys
from pathlib import Path

KEY = str(Path.home() / "dev/dawgsec/tezcatlipoca/proxmox")
ENGINE = "10.0.0.252"
PROXY = (f"ssh -i {KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
         f"-W %h:%p sysadmin@{ENGINE}")


def checks_for(artifact):
    """[shell test lines] for one artifact — 'label<TAB>shell' pairs."""
    kind = artifact.get("kind") or ""
    det = str(artifact.get("detail") or "")
    out = []
    if kind == "realm-imix":
        m = re.search(r"imix\[([a-z0-9_]+)\]\s+(\S+)\s+->\s+(\S+)\s+\(([^)]+)\)", det)
        if m:
            t, path, dsn, unit = m.groups()
            out.append(f"imix-{t}-binary\ttest -x {path} && echo ok || echo MISSING")
            out.append(f"imix-{t}-unit-state\tsystemctl is-enabled {unit} 2>/dev/null; systemctl is-active {unit} 2>/dev/null")
    elif kind == "systemd":
        unit = det.split()[0]
        out.append(f"systemd-{unit}\tsystemctl is-enabled {unit} 2>/dev/null; systemctl is-active {unit} 2>/dev/null")
    elif kind in ("profile", "cron", "sshd-dropin"):
        path = det.split()[0]
        out.append(f"{kind}-file\ttest -e {path} && echo ok || echo MISSING")
    elif kind == "authorized-key":
        out.append("authorized-key\tcat /root/.ssh/authorized_keys 2>&1 | grep -c bad-auto || echo 'unreadable-as-nonroot'")
    elif kind == "suid":
        path = det.split(" at ")[-1].split()[0]
        out.append(f"suid-bits\tstat -c '%a %U' {path} 2>/dev/null || echo MISSING")
    elif kind.startswith("rogue-user"):
        out.append(f"rogue-user\tid -u {det.strip()} 2>/dev/null || echo MISSING")
    elif kind.startswith("evasion:immutable"):
        paths = det.split(" on: ")[-1].split()
        for p in paths:
            out.append(f"immutable-{Path(p).name}\tlsattr {p} 2>/dev/null | cut -c1-20")
    elif kind.startswith("evasion:timestomp"):
        paths = det.split("matched to ")[-1].split(": ")[-1].split()
        for p in paths:
            out.append(f"timestomp-{Path(p).name}\tstat -c %y {p} 2>/dev/null | cut -c1-19; stat -c %y /etc/hostname | cut -c1-19")
    elif kind.startswith("decoy"):
        # detail is "<decoy_path> + <unit> (obvious bait implant)"
        path = det.split(" + ")[0].strip()
        unit = det.split(" + ")[1].split(" ")[0] if " + " in det else ""
        out.append(f"decoy\ttest -e {path} && echo file-ok; "
                   f"systemctl is-active {unit} 2>/dev/null")
    elif kind.startswith("evasion:logs"):
        cron = det.split(" — ")[0].strip()
        out.append(f"logwatcher\ttest -e {cron} && echo ok || echo MISSING")
    elif kind.startswith("rawsock-beacon"):
        path = det.split()[0]
        out.append(f"rawsock-binary\ttest -x {path} && echo ok || echo MISSING")
        out.append("rawsock-unit\tls /etc/systemd/system/ 2>/dev/null | grep -ci statpoll")
    return out


def run_box(ip, script, password, timeout=90):
    """Run the audit as the login user (no sudo): the range's planted
    world-writable-sudoers misconfig makes sudo refuse outright, and root login
    is blocked by sshd — so root-only facts (key contents under /root) are
    reported as unverifiable rather than silently missing."""
    import base64
    b64 = base64.b64encode(script.encode()).decode()
    remote = ("echo %s | base64 -d > /tmp/.audit.sh; "
              "bash /tmp/.audit.sh 2>&1; rm -f /tmp/.audit.sh")
    cmd = ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null", "-o", f"ProxyCommand={PROXY}",
           "-o", "ConnectTimeout=10", f"medic@{ip}", remote % b64]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def main():
    comp = Path(sys.argv[1])
    world = json.loads(Path(sys.argv[2]).read_text())
    only = None
    if "--boxes" in sys.argv:
        only = set(sys.argv[sys.argv.index("--boxes") + 1].split(","))
    state = json.loads((comp / ".deploy_state.json").read_text())
    pw = state["box_password"]

    by_box = {}
    for a in world.get("artifacts") or []:
        by_box.setdefault(a.get("ip"), []).append(a)

    for ip in sorted(by_box, key=lambda x: int(x.split(".")[-1])):
        if only and ip not in only:
            continue
        arts = by_box[ip]
        lines, labels = [], []
        for a in arts:
            for pair in checks_for(a):
                label, sh = pair.split("\t", 1)
                labels.append(label)
                lines.append(f"echo \"=== {label}\"; {sh}")
        script = "\n".join(lines)
        print(f"\n########## {ip}  ({len(arts)} artifacts) ##########")
        out = run_box(ip, script, pw)
        cur = None
        buf = {}
        for ln in out.splitlines():
            if ln.startswith("=== "):
                if cur:
                    print(f"  {cur:28s} {' | '.join(x.strip() for x in buf[cur] if x.strip())}")
                cur = ln[4:].strip()
                buf[cur] = []
            elif cur:
                buf[cur].append(ln)
        if cur:
            print(f"  {cur:28s} {' | '.join(x.strip() for x in buf[cur] if x.strip())}")


if __name__ == "__main__":
    main()
