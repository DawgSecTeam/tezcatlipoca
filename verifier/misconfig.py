"""Misconfig gates: one planted misconfig confirmed (SSH, guest-agent fallback) and survival across team clones."""

import json
import os
import subprocess
from pathlib import Path

from infra.guest_exec import guest_agent_exec_root
from targets import vm_id_for
from utils import MAX_CONCURRENCY, run_concurrent

from verifier import context
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


MISCONFIG_CHECKS = {
    "suid-find": (
        "ls -l $(which find)",
        lambda out: "rws" in out.split("\n")[0],
    ),
    "www-data-shell": (
        "grep '^www-data:' /etc/passwd",
        lambda out: out.strip().endswith("/bin/bash"),
    ),
    "bad-perms-userConfig": (
        "stat -c %a /etc/shadow",
        lambda out: out.strip() == "666",
    ),
    "writable-sudoers": (
        "stat -c %a /etc/sudoers.d",
        lambda out: out.strip() == "777",
    ),
}


def _config_name(entry):
# Normalize config entry (string or {"name": ...}) to name for dict lookup.
    return entry if isinstance(entry, str) else entry.get("name")


def _target_vmid(comp_dir, box):
# VMID for a nakon machine entry: frozen targets.json, falling back to boxes.json order.
    try:
        identifier = box["ip"].split(".")[2]
        base_name = box["name"].rsplit("-team", 1)[0]
        targets_path = Path(comp_dir) / "targets.json"
        if targets_path.exists():
            for t in json.loads(targets_path.read_text()).get("targets", {}).values():
                if str(t["identifier"]) == identifier and t["box_name"] == base_name:
                    return t["vmid"]
        boxes_json = json.loads((Path(comp_dir) / "boxes.json").read_text())
        idx = next(i for i, b in enumerate(boxes_json) if b.get("name") == base_name)
        return vm_id_for(identifier, idx)
    except Exception:
        return None


def misconfig_via_guest_agent(comp_dir, box, verifiable):
# SSH-dead fallback: run the same checks via the QEMU guest agent.
#
#     scrim-extreme-cyberfield-2026-09-22 lost its only verifier FAIL to dead
#     Linux SSH while the planted artifacts were independently confirmed fine —
#     the agent (virtio-serial, no network) reaches the box anyway.
    vmid = _target_vmid(comp_dir, box)
    if vmid is None:
        print("  (guest-agent fallback unavailable: could not resolve the box's vmid)")
        return False
    node = os.environ.get("TF_VAR_proxmox_node")
    if not node:
        print("  (guest-agent fallback unavailable: TF_VAR_proxmox_node not set)")
        return False
    print(f"  SSH unavailable — falling back to guest-agent exec on vmid {vmid}...")
    for config in verifiable:
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            rc, out, err = guest_agent_exec_root(node, vmid, cmd)
        except Exception as e:
            print(f"  WARN  {config}: guest-agent exec failed ({e})")
            continue
        if rc != 0:
            print(f"  ....  '{config}': guest rc={rc}: {(err or '').strip()[:80]}")
            continue
        if predicate(out):
            print(f"  PASS  confirmed '{config}' via guest agent — `{cmd}` -> {out.strip()[:80]}")
            return True
        print(f"  ....  '{config}' not present: {out.strip()[:80]}")
    return False


def comp_is_clean(comp_dir, boxes):
# True for a deliberately-clean comp: box_vulns.json plants nothing AND no nakon-config
#     machine carries a verifiable misconfig. A comp that pins vulns but whose machines lost them
#     is drift, not clean — that stays a FAIL in check_misconfig.
    try:
        pinned = json.loads((Path(comp_dir) / "box_vulns.json").read_text())
    except (OSError, ValueError):
        pinned = None
    if pinned is None or any(pinned.values()):
        return False
    return not any(c in MISCONFIG_CHECKS
                   for b in boxes for c in map(_config_name, b.get("configurations", [])))


def misconfigs_unverifiable(comp_dir, boxes):
# True when box_vulns.json pins misconfigs, every pin reached a machine's configuration list,
#     and none of them has a probe here (Windows-only pins, catalog rows without a check). The
#     spot-check cannot confirm what it has no probe for; plant_coverage is the gate that proves
#     those pins were planted, so this is a non-gating SKIP, not a FAIL.
    try:
        pinned = json.loads((Path(comp_dir) / "box_vulns.json").read_text())
    except (OSError, ValueError):
        return False
    names = {_config_name(e) for v in pinned.values() for e in v}
    if not names:
        return False
    planted = {c for b in boxes for c in map(_config_name, b.get("configurations", []))}
    return names <= planted and not (names & set(MISCONFIG_CHECKS))


def check_misconfig(ctx, boxes, comp_dir):
# One gate for the planted misconfigs, one probe per (box, verifiable config).
#
#     Every team's copy of a box must carry the configs its clones share (clone-race
#     guard), and at least one planted misconfig must be confirmed somewhere — over SSH,
#     or via the guest agent for a box whose SSH is dead. With one team there is no second
#     copy to compare, so the confirmation alone decides. Unprovable everywhere is SKIP,
#     never a pass.
    print("\n[4/5] MISCONFIG SPOT-CHECK")
    candidates = []
    for box in boxes:
        verifiable = [c for c in map(_config_name, box.get("configurations", []))
                      if c in MISCONFIG_CHECKS]
        if box.get("ip") and verifiable:
            candidates.append((box, verifiable))
    if not candidates:
        print("  FAIL  — no box in nakon-config.json carries a verifiable misconfig.")
        return gate_fail("misconfig", "no box carries a verifiable misconfig")

    units = [(box, config) for box, verifiable in candidates for config in verifiable]

    def _probe(unit):
        box, config = unit
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            proc = context.ssh_via_gateway(ctx, box["ip"], cmd)
        except (subprocess.TimeoutExpired, CheckError):
            return "unreachable"
        if proc.returncode != 0:
            return "unknown"
        return "present" if predicate(proc.stdout) else "absent"

    outcome = {}
    for (box, config), r in zip(units, run_concurrent(units, _probe, max_workers=MAX_CONCURRENCY)):
        # Only the two SSH failures are classified above; anything else escapes here.
        if isinstance(r, Exception):
            raise r
        outcome[(box["ip"], config)] = r
        name = box.get("name", box["ip"])
        if r == "present":
            print(f"  PASS  confirmed '{config}' on {name}")
        elif r == "absent":
            print(f"  ....  '{config}' not present on {name}")
        elif r == "unreachable":
            print(f"  WARN  {name}: '{config}' SSH unreachable")
        else:
            print(f"  WARN  {name}: '{config}' command failed")

    confirmed = any(r == "present" for r in outcome.values())
    for box, verifiable in candidates:
        if all(outcome[(box["ip"], c)] == "unreachable" for c in verifiable):
            if misconfig_via_guest_agent(comp_dir, box, verifiable):
                confirmed = True

    groups = {}
    for box, _ in candidates:
        configs = tuple(_config_name(c) for c in box.get("configurations", []))
        groups.setdefault(configs, []).append(box)

    all_ok = True
    any_unverified = False
    for configs, machines in groups.items():
        if len(machines) < 2:
            continue
        for config in [c for c in configs if c in MISCONFIG_CHECKS]:
            present, absent, unknown = [], [], []
            for m in machines:
                name = m.get("name", m["ip"])
                r = outcome[(m["ip"], config)]
                if r == "present":
                    present.append(name)
                elif r == "absent":
                    absent.append(name)
                else:
                    unknown.append(name)
            if present and absent:
                all_ok = False
                print(f"  FAIL  '{config}' present on {present} but MISSING on {absent} — "
                      f"didn't survive cloning")
            elif absent and not present:
                all_ok = False
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  FAIL  '{config}' absent on every team ({absent}){note} — the "
                      f"config didn't plant on any clone")
            elif present:
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  PASS  '{config}' present on all {len(present)} team(s): {present}{note}")
            else:
                any_unverified = True
                print(f"  SKIP  '{config}' — could not verify on any team ({unknown})")
    if not all_ok:
        return gate_fail("misconfig", "misconfig did not survive cloning everywhere")
    if not confirmed:
        if any(r == "absent" for r in outcome.values()):
            print("  FAIL  — no planted misconfig could be confirmed on any box.")
            return gate_fail("misconfig", "no planted misconfig could be confirmed")
        print("  SKIP  — no misconfig could be probed (SSH and guest agent unavailable).")
        return gate_skip("misconfig", "could not verify any misconfig (SSH unavailable)")
    if any_unverified:
        return gate_skip("misconfig", "some configs unprovable (SSH dead)")
    return gate_pass("misconfig", "verifiable configs present on every team")
