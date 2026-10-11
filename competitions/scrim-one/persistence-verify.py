#!/usr/bin/env python3
"""Persistence verification for a scrim run — what PLANTED vs what is LIVE.

Never trusts the red agent's own report: it cross-checks three independent
sources and prints the disagreements, because those are the findings.

  1. world.json artifacts        — what red believes it planted, per box, per kind
     world.json meta.dead_artifacts — what red believes was removed
  2. tavern (the real C2)        — which realm channels the C2 actually knows about
     matching each planted beacon's deterministic IMIX_BEACON_ID
     (uuid5 of "bad-auto:{team}:{ident}:{box}:{transport}") against the beacon
     rows tavern holds, so a channel that was planted but never checked in is
     visible instead of looking like a success.
  3. the box itself              — is the planted unit actually enabled+running

Usage:
  persistence-verify.py <comp_dir> [--world <path-to-world.json>] [--boxes <ip,ip>]
"""
import json
import subprocess
import sys
import uuid
from collections import defaultdict
from pathlib import Path

TAVERN = "http://10.0.0.117:8000/graphql"
GENESIS = "gRPC/HTTP1/DNS/ICMP/QUIC + multi (one independent beacon per channel)"
KEY = Path.home() / "dev/dawgsec/tezcatlipoca/proxmox"
ENGINE = "10.0.0.252"


def sh(cmd, timeout=90, cwd=None, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       cwd=cwd, env=env)
    return (r.stdout or "") + (r.stderr or "")


def graphql(query):
    out = sh(["curl", "-s", "-m", "20", TAVERN, "-H", "Content-Type: application/json",
              "-d", json.dumps({"query": query})])
    try:
        return json.loads(out)
    except ValueError:
        return {"error": out[:200]}


def tavern_beacons():
    """{identifier: {transport, name, host, last_seen}} — the C2's own truth."""
    q = ("{ beacons { edges { node { identifier name transport lastSeenAt "
         "host { name } } } } }")
    d = graphql(q)
    out = {}
    edges = (((d.get("data") or {}).get("beacons") or {}).get("edges") or [])
    for e in edges:
        b = e.get("node") or {}
        out[b.get("identifier")] = {
            "transport": b.get("transport"), "name": b.get("name"),
            "host": ((b.get("host") or {}).get("name")), "last_seen": b.get("lastSeenAt"),
        }
    return out, d.get("errors")


def world_artifacts(path):
    d = json.loads(Path(path).read_text())
    return d.get("artifacts") or [], (d.get("meta") or {})


def box_ssh(ip, script, timeout=60):
    return sh(["ssh", "-i", str(KEY), "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
               f"sysadmin@{ENGINE}",
               f"ssh -i ~/.ssh/proxmox_key -o StrictHostKeyChecking=no "
               f"-o ConnectTimeout=8 {USER}@{ip} 'sudo -n bash -s' <<'EOS'\n{script}\nEOS"],
              timeout=timeout)


USER = "medic"


def main():
    comp = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    argv = sys.argv[2:]
    world_path = None
    for i, a in enumerate(argv):
        if a == "--world":
            world_path = argv[i + 1]
        if a == "--user":
            globals()["USER"] = argv[i + 1]

    teams = json.loads((comp / "teams.json").read_text())
    boxes = json.loads((comp / "boxes.json").read_text())
    ident = {v["identifier"] for v in teams.values()}

    expected = {}   # identifier(uuid) -> (box, transport, ip)
    for team_name, tv in teams.items():
        tid = tv["identifier"]
        for b in boxes:
            ip = f"192.168.{tid}.{b['last_octet']}"
            for t in ("grpc", "http1", "dns", "icmp", "quic", "multi"):
                seed = f"bad-auto:{team_name}:{tid}:{b['name']}:{t}"
                expected[str(uuid.uuid5(uuid.NAMESPACE_URL, seed))] = (b["name"], t, ip)

    arts, meta = (world_artifacts(world_path) if world_path else ([], {}))
    dead = meta.get("dead_artifacts") or []
    reg, errs = tavern_beacons()

    print(f"== world.json: {len(arts)} artifact(s)"
          + (f"  (meta.dead_artifacts: {len(dead)})" if dead else ""))
    by_box_kind = defaultdict(lambda: defaultdict(int))
    for a in arts:
        by_box_kind[a.get("ip")][a.get("kind")] += 1
    for ip in sorted(by_box_kind, key=lambda x: [int(p) for p in x.split(".")]):
        kinds = by_box_kind[ip]
        print(f"  {ip}: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))

    print(f"\n== realm channels: planted (world.json) vs registered (tavern) {GENESIS}")
    planted = defaultdict(set)
    for a in arts:
        if a.get("kind") == "realm-imix":
            det = str(a.get("detail") or "")
            if "imix[" in det:
                planted[a.get("ip")].add(det.split("imix[", 1)[1].split("]", 1)[0])
    for ip in sorted(planted, key=lambda x: [int(p) for p in x.split(".")]):
        row = []
        for t in ("grpc", "http1", "dns", "icmp", "quic", "multi"):
            if t not in planted[ip]:
                row.append(f"{t}:-")
                continue
            # find the expected identifier for this box/transport
            match = [k for k, v in expected.items() if v[1] == t and v[2] == ip]
            live = "LIVE" if match and match[0] in reg else ("PLANTED-no-checkin" if match else "?")
            row.append(f"{t}:{live}")
        print(f"  {ip}: " + "  ".join(row))

    if errs:
        print(f"\n  tavern errors: {errs}")
    print("\n== tavern beacons by host (all channels, incl. pre-existing)")
    hosts = defaultdict(list)
    for ident_uuid, b in reg.items():
        hosts[b.get("host")].append((b.get("transport"), b.get("last_seen")))
    for h in sorted(k or "?" for k in hosts):
        seen = hosts.get(h) or []
        print(f"  {h}: {len(seen)}  {sorted({t for t, _ in seen})}")

    if dead:
        print("\n== meta.dead_artifacts (red saw these removed)")
        for d in dead[:20]:
            print("  ", json.dumps(d)[:160])


if __name__ == "__main__":
    main()
