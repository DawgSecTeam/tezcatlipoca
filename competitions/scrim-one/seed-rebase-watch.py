#!/usr/bin/env python3
"""Rebase the red director onto the seeded world, right after the day-0 seed.

Why: run-agent-scrim starts bad-auto.service (`badauto deploy --start`) BEFORE it runs
the seed, and the seed is a separate process. The director loads an EMPTY world.json at
startup; when the seed later writes its artifact record, the director's next save
overwrites it (lost update) — red then believes it planted nothing, so `beacon_score`
reads only its own plants and the per-channel re-plant logic is blind to the seeded set.
Restarting the service once the seed has landed makes the director load the seeded world,
and lines red's event clock up with T0.

Usage: seed-rebase-watch.py <harness-log> [--red-ip IP] [--timeout-min N]
"""
import os
import subprocess
import sys
import time

MARKER = "assume-breach presence seeded"
KEY = os.path.expanduser("~/dev/dawgsec/tezcatlipoca/proxmox")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    logpath = sys.argv[1]
    red_ip = "10.0.0.198"
    if "--red-ip" in sys.argv:
        red_ip = sys.argv[sys.argv.index("--red-ip") + 1]
    limit = 60 * int(sys.argv[sys.argv.index("--timeout-min") + 1]) if "--timeout-min" in sys.argv else 3600
    deadline = time.time() + limit
    log(f"waiting for '{MARKER}' in {logpath} (limit {limit // 60} min)")
    while time.time() < deadline:
        try:
            with open(logpath, encoding="utf-8", errors="replace") as f:
                if MARKER in f.read():
                    break
        except FileNotFoundError:
            pass
        time.sleep(10)
    else:
        log("seed marker never appeared — giving up, no restart performed")
        sys.exit(1)

    # let the seed's own process exit and the harness capture T0
    time.sleep(20)
    log(f"seed landed — restarting bad-auto.service on {red_ip} so the director loads it")
    r = subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
         f"sysadmin@{red_ip}",
         "sudo -n systemctl restart bad-auto.service; sleep 3; systemctl is-active bad-auto.service; "
         "sudo -n python3 -c \"import json;d=json.load(open('/var/lib/bad-auto/world.json'));"
         "print('artifacts',len(d.get('artifacts') or []),'seed_done',(d.get('meta') or {}).get('seed_done'))\""],
        capture_output=True, text=True, timeout=120)
    log("restart result: " + ((r.stdout or "") + (r.stderr or "")).strip().replace("\n", " | "))


if __name__ == "__main__":
    main()
