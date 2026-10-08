#!/usr/bin/env python3
"""Instrumented post-run check for the run-6 validation scrim.

Answers the questions the last two runs left open, from red's OWN pulled state
(teardown now collects world.json before destroying red01) cross-checked against
the live tavern C2 — never from an agent's self-report:

  1. did the day-0 seed survive the director's start?   (meta.seed_done + count)
  2. did the Windows realm channels plant, all five?    (realm-imix-win artifacts)
  3. are those beacons actually LIVE at the C2?         (tavern lastSeenAt freshness)
  4. did the two families that never planted land?      (defrun-win, evasion:hide-win)
  5. are the artifact names randomised?                 (no legacy IOC literals)
  6. what are the drawn names per box?                  (blue's grep list)

Usage: run6-verify.py <run_dir> [--tavern http://10.0.0.117:8000/graphql]
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The literals blue used to be able to grep for; none of them may appear in an artifact.
LEGACY = ("tznet", "netupdate", "log-cleanup", ".sysupd", "winlogmaint",
          "svc-netupdate", "bad-auto-red")
# Transports red plants; a Windows box should carry one beacon per transport.
TRANSPORTS = ("grpc", "http1", "dns", "icmp", "quic")

QUERY = """query { beacons { edges { node {
  id transport lastSeenAt host { id name } } } } }"""


def find_world(run_dir):
    hits = sorted(Path(run_dir).rglob("world.json"))
    if not hits:
        sys.exit(f"no world.json under {run_dir} — teardown did not collect red's state")
    return hits[-1]


def tavern_beacons(url):
    body = json.dumps({"query": QUERY}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        payload = json.load(r)
    if "errors" in payload:
        return {}, payload["errors"]
    out = {}
    for e in payload["data"]["beacons"]["edges"]:
        n = e["node"]
        out[n["id"]] = n
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--tavern", default="http://10.0.0.117:8000/graphql")
    a = ap.parse_args()

    wpath = find_world(a.run_dir)
    w = json.loads(wpath.read_text())
    meta = w.get("meta") or {}
    arts = w.get("artifacts") or []

    print(f"world.json: {wpath}")
    print(f"  artifacts: {len(arts)}   seed_done: {meta.get('seed_done')}   "
          f"event_start: {meta.get('event_start')}")

    # 1. seed survived the director's start
    if meta.get("seed_done"):
        seeded = meta.get("seed_artifacts", "?")
        print(f"  [1] seed survived the post-seed director start: YES "
              f"(seed recorded {seeded} artifacts, world holds {len(arts)})")
    else:
        print("  [1] seed_done MISSING — the lost-update is back")

    # 2/3. realm channels per box, then liveness at the C2
    try:
        tb, err = tavern_beacons(a.tavern)
    except Exception as exc:            # noqa: BLE001 - report, never crash the audit
        tb, err = {}, str(exc)
    if err:
        print(f"  tavern query failed: {err}")
    fresh_cut = datetime.now(timezone.utc) - timedelta(minutes=20)

    realm = {}
    for art in arts:
        if art.get("kind", "").startswith("realm-imix"):
            realm.setdefault(art["ip"], []).append(art)

    def _transport(detail):
        m = re.search(r"imix\[(\w+)\]", detail or "")
        return m.group(1) if m else None

    print(f"\n  [2] realm beacons by box ({len(realm)} boxes):")
    for ip in sorted(realm):
        win = any("win" in (x.get("kind") or "") for x in realm[ip])
        got = sorted({t for t in (_transport(x.get("detail", "")) for x in realm[ip]) if t})
        missing = [t for t in TRANSPORTS if t not in got]
        live = 0
        for x in realm[ip]:
            m = re.search(r"beacon ([0-9a-f]{8})", x.get("detail", ""))
            if m:
                for bid, node in (tb or {}).items():
                    if bid.startswith(m.group(1)):
                        ts = node.get("lastSeenAt")
                        if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) > fresh_cut:
                            live += 1
        print(f"     {ip}{' (win)' if win else '     '}: {len(got)}/{len(TRANSPORTS)} transports "
              f"{got} missing={missing or '-'} | live<20min={live}")

    # 4. the two families that never planted
    kinds = {}
    for art in arts:
        kinds[art["kind"]] = kinds.get(art["kind"], 0) + 1
    print("\n  [4] artifact kinds:")
    for k in sorted(kinds):
        print(f"     {k}: {kinds[k]}")
    for want, label in (("defrun-win", "Windows defrun (HKU .DEFAULT Run)"),
                        ("evasion:hide-win", "Windows evasion hide")):
        print(f"     {label}: {'PRESENT' if kinds.get(want) else 'MISSING'}")

    # 5. randomised names
    blob = " ".join(json.dumps(x.get("detail", "")) for x in arts).lower()
    leaked = [i for i in LEGACY if i in blob]
    print(f"\n  [5] legacy IOC literals in artifact details: {leaked or 'none'}")

    # 6. drawn names
    names = sorted(set(meta.get("artifact_names") or []))
    print(f"\n  [6] drawn name stems this event ({len(names)}): {names}")


if __name__ == "__main__":
    main()
