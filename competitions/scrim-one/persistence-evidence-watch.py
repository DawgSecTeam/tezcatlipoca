#!/usr/bin/env python3
"""In-run persistence evidence sampler.

Every INTERVAL seconds, record what the range actually shows about red's
persistence, so the post-run verification report has time-series evidence and not
just the agent's own self-report:

  * tavern (C2) host/beacon registry  -> which channels actually registered
  * red01 world.json artifact census  -> what red believes it planted, per kind
  * red01 world.json dead_artifacts   -> what blue (or the box) removed
  * red01 rawsock controller journal  -> whether the raw-socket channel checked in
  * bad-auto.service state            -> whether the director was alive

Read-only: never mutates the range.
"""
import json
import os
import subprocess
import sys
import time
from collections import Counter

EVID = sys.argv[1]
DUR_MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 240
INTERVAL = 300
RED_IP = "10.0.0.198"
KEY = os.path.expanduser("~/dev/dawgsec/tezcatlipoca/proxmox")
BAD = os.path.expanduser("~/dev/dawgsec/bad-auto")
STOP_FILE = os.path.join(EVID, "STOP")

_WORLD_PY = (
    "import json,collections;"
    "d=json.load(open('/var/lib/bad-auto/world.json'));"
    "a=d.get('artifacts') or [];m=d.get('meta') or {};"
    "print(json.dumps({'n':len(a),"
    "'kinds':dict(collections.Counter(x.get('kind') for x in a)),"
    "'dead':len(m.get('dead_artifacts') or []),"
    "'names':len(m.get('realm_unit_names') or []),"
    "'seed_done':bool(m.get('seed_done'))}))"
)


def sh(cmd, timeout=120, cwd=None, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # a probe must never kill the sampler
        return f"ERR {type(e).__name__}: {e}"


def ssh_red(cmd, timeout=45):
    return sh(["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
               f"sysadmin@{RED_IP}", cmd], timeout=timeout)


def tavern_hosts():
    env = {**os.environ, "BAuto_LLM_API_KEY": "local"}
    out = sh(["/usr/bin/python3", "-m", "badauto", "realm", "list-hosts"],
             timeout=90, cwd=BAD, env=env)
    try:
        hosts = json.loads(out)
    except ValueError:
        return {"error": out.strip()[:200]}
    return {h.get("name"): {"ip": h.get("primaryIP"),
                            "platform": (h.get("platform") or "").replace("PLATFORM_", ""),
                            "beacons": [b.get("name") for b in (h.get("beacons") or [])]}
            for h in hosts} if isinstance(hosts, list) else {"error": "unexpected shape"}


def sample():
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "epoch": int(time.time())}
    rec["tavern"] = tavern_hosts()
    rec["red01_world"] = ssh_red(f"sudo -n python3 -c \"{_WORLD_PY}\"").strip()
    rec["red01_beaconctl_lines"] = ssh_red(
        "sudo -n journalctl -u bad-auto-beaconctl --no-pager 2>/dev/null | wc -l").strip()
    rec["red01_badauto_active"] = ssh_red(
        "systemctl is-active bad-auto.service").strip()
    with open(os.path.join(EVID, "samples.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    tb = rec["tavern"]
    n_hosts = len(tb) if isinstance(tb, dict) and "error" not in tb else f"ERR({str(tb)[:60]})"
    with open(os.path.join(EVID, "sample.log"), "a") as f:
        f.write(f"{rec['ts']} tavern_hosts={n_hosts} world={rec['red01_world'][:160]} "
                f"beaconctl_lines={rec['red01_beaconctl_lines']} "
                f"bad-auto={rec['red01_badauto_active']}\n")


def main():
    os.makedirs(EVID, exist_ok=True)
    deadline = time.time() + DUR_MIN * 60
    while time.time() < deadline and not os.path.exists(STOP_FILE):
        sample()
        for _ in range(INTERVAL):
            if os.path.exists(STOP_FILE):
                return
            time.sleep(1)
    sample()


if __name__ == "__main__":
    main()
