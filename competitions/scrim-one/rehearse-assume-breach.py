#!/usr/bin/env python3
"""Rehearsal of the HUMAN-run path: the deploy-time assume-breach seed.

An agent scrim seeds through the harness (`stage_red` -> `seed_assume_breach`).
The human flow is a different pipeline: `create-competition.py` deploy runs the
phase-7 hook `red_plant_ops.plant_assume_breach(ctx)` — gated by the Compfile
knob `assume_breach` — which deploys red01 and seeds the presence BEFORE the
`tz-ready` snapshot, so the restore point every box carries is already
compromised. That path had never been executed; its `_seed_red` was missing the
`cd /opt/bad-auto` (badauto is imported by cwd on red01, never pip-installed), so
it died on `No module named badauto`.

This calls the hook with the same context shape the deploy phase passes, on the
live range, and reports what it did. It is the rehearsal, not a stand-in: same
code, same bad-auto deploy, same seed.
"""

import json
import subprocess
import sys
from pathlib import Path

TEZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TEZ))

import red_plant_ops  # noqa: E402  (needs the repo root on sys.path)


class Ctx:
    """The fields plant_assume_breach touches."""

    def __init__(self, comp_dir, ssh_key):
        self.comp_dir = comp_dir
        self.ssh_key_abs = str(ssh_key)
        self.state = {}

    def save_state(self):
        pass


def world_census(key, red_ip):
    """red01's own world.json: total artifacts, kinds, and any Windows realm artifacts."""
    r = subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
         f"sysadmin@{red_ip}", "sudo -n cat /var/lib/bad-auto/world.json"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    try:
        w = json.loads(r.stdout)
    except ValueError:
        return None
    kinds = {}
    for a in w.get("artifacts") or []:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    wins = [a for a in (w.get("artifacts") or []) if a.get("kind") == "realm-imix-win"]
    return {"artifacts": len(w.get("artifacts") or []),
            "seed_done": (w.get("meta") or {}).get("seed_done"),
            "kinds": kinds, "realm_win": len(wins),
            "realm_win_details": [a["detail"][:110] for a in wins[:6]]}


def main():
    comp_dir = (TEZ / "competitions" / "scrim-one").resolve()
    compfile = comp_dir / "Compfile"
    text = compfile.read_text()

    if "assume_breach" not in text:
        print(f"NOTE: no `assume_breach` knob in {compfile} — the human path would")
        print("      deploy a range with NO red presence. Turning it on for the rehearsal;")
        print("      this is the setting a human run needs.")
        with open(compfile, "a") as fh:
            fh.write("assume_breach 1\nassume_breach_depth 3\n")
        print("      appended: assume_breach 1 / assume_breach_depth 3")

    ctx = Ctx(comp_dir, TEZ / "proxmox")
    print(f"plant_assume_breach(comp_dir={ctx.comp_dir}, key={ctx.ssh_key_abs})")
    summary = red_plant_ops.plant_assume_breach(ctx)
    print(f"\nsummary: {summary}")

    print("\n=== red01 world.json census (the organizer's view of the plant) ===")
    cen = world_census(str((TEZ / "proxmox").resolve()), "10.0.0.198")
    if cen is None:
        print("  could not read /var/lib/bad-auto/world.json on red01")
    else:
        print(f"  artifacts: {cen['artifacts']}   seed_done: {cen['seed_done']}")
        print(f"  realm-imix-win artifacts: {cen['realm_win']}")
        for d in cen["realm_win_details"]:
            print(f"     {d}")
        for k in sorted(cen["kinds"]):
            print(f"     {k}: {cen['kinds'][k]}")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
