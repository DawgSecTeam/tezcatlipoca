"""Misconfig gates: one planted misconfig confirmed (SSH, guest-agent fallback) and survival across team clones."""

import json
import os
import subprocess
from pathlib import Path

from range_ops import guest_agent_exec_root, vm_id_for
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
    """Normalize config entry (string or {"name": ...}) to name for dict lookup."""
    return entry if isinstance(entry, str) else entry.get("name")


def _target_vmid(comp_dir, box):
    """VMID for a nakon machine entry: frozen targets.json, falling back to boxes.json order."""
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
    """SSH-dead fallback: run the same checks via the QEMU guest agent.

    scrim-extreme-cyberfield-2026-09-22 lost its only verifier FAIL to dead
    Linux SSH while the planted artifacts were independently confirmed fine —
    the agent (virtio-serial, no network) reaches the box anyway."""
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
    """True for a deliberately-clean comp: box_vulns.json plants nothing AND no nakon-config
    machine carries a verifiable misconfig. A comp that pins vulns but whose machines lost them
    is drift, not clean — that stays a FAIL in check_misconfig."""
    try:
        pinned = json.loads((Path(comp_dir) / "box_vulns.json").read_text())
    except (OSError, ValueError):
        pinned = None
    if pinned is None or any(pinned.values()):
        return False
    return not any(c in MISCONFIG_CHECKS
                   for b in boxes for c in map(_config_name, b.get("configurations", [])))


def misconfigs_unverifiable(comp_dir, boxes):
    """True when box_vulns.json pins misconfigs, every pin reached a machine's configuration list,
    and none of them has a probe here (Windows-only pins, catalog rows without a check). The
    spot-check cannot confirm what it has no probe for; plant_coverage is the gate that proves
    those pins were planted, so this is a non-gating SKIP, not a FAIL."""
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
    """SSH via gateway to one box and confirm >=1 planted misconfig. Returns bool."""
    print("\n[4/5] MISCONFIG SPOT-CHECK")
    target = None
    for box in boxes:
        verifiable = [c for c in map(_config_name, box.get("configurations", []))
                      if c in MISCONFIG_CHECKS]
        if box.get("ip") and verifiable:
            target = (box, verifiable)
            break
    if target is None:
        print("  FAIL  — no box in nakon-config.json carries a verifiable misconfig.")
        return False
    box, verifiable = target
    ip = box["ip"]
    print(f"  target: {box.get('name', ip)} ({ip}) — candidates: {', '.join(verifiable)}")
    ssh_dead = False
    for config in verifiable:
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            proc = context.ssh_via_gateway(ctx, ip, cmd)
        except subprocess.TimeoutExpired:
            print(f"  WARN  {config}: SSH timed out.")
            ssh_dead = True
            continue
        except CheckError as e:
            print(f"  WARN  SSH to the box is unavailable ({e})")
            ssh_dead = True
            break
        if proc.returncode != 0:
            print(f"  WARN  {config}: command failed (rc={proc.returncode}): "
                  f"{(proc.stderr or '').strip()[:120]}")
            continue
        out = proc.stdout
        if predicate(out):
            print(f"  PASS  confirmed '{config}' — `{cmd}` -> {out.strip()[:80]}")
            return True
        print(f"  ....  '{config}' not present: {out.strip()[:80]}")
    if ssh_dead:
        if misconfig_via_guest_agent(comp_dir, box, verifiable):
            return True
    print("  FAIL  — no planted misconfig could be confirmed on the target box.")
    return False


def check_misconfig_survival(ctx, boxes):
    """Confirm every team's copy of each box carries same verifiable misconfigs (clone race guard).

    Returns a GateResult: present-on-all PASSes, present-on-some FAILs, and — the
    branch the old code was missing — absent-on-every-team FAILs too (the case
    matched no branch at all, so it printed nothing and left all_ok True). All
    probes unprovable (SSH dead) is SKIP, never a pass."""
    print("\n  (cross-team misconfig survival check)")
    groups = {}
    for box in boxes:
        if not box.get("ip"):
            continue
        configs = tuple(_config_name(c) for c in box.get("configurations", []))
        verifiable = [c for c in configs if c in MISCONFIG_CHECKS]
        if not verifiable:
            continue
        groups.setdefault(configs, []).append(box)

    multi_team_groups = [(configs, machines) for configs, machines in groups.items()
                          if len(machines) > 1]
    if not multi_team_groups:
        print("  SKIP  — fewer than 2 teams, or no box's misconfigs are both shared and "
              "verifiable.")
        return gate_skip("misconfig_survival",
                     "fewer than 2 teams or nothing shared+verifiable", gating=False)

    all_ok = True
    any_unverified = False
    # Work units in the serial order: groups -> configs -> machines. Each unit is one
    # independent 60s SSH probe, so a groups x configs x machines loop is minutes of
    # sequential waiting. The probes run on the full MAX_CONCURRENCY pool (pure SSH over
    # the ControlMaster channel, no Proxmox task), and the present/absent/unknown
    # classification is aggregated BACK IN SERIAL ORDER below, so every verdict AND
    # every printed line (present on {present} but MISSING on {absent}) is unchanged.

    def _probe(unit):
        config, m = unit
        cmd, predicate = MISCONFIG_CHECKS[config]
        try:
            proc = context.ssh_via_gateway(ctx, m["ip"], cmd)
        except (subprocess.TimeoutExpired, CheckError):
            return "unknown"
        if proc.returncode != 0:
            return "unknown"
        return "present" if predicate(proc.stdout) else "absent"

    # The serial loop ran (config, machine) pairs in this exact order; the pool keeps
    # that order in its result list, so walk the same nesting and consume in step.
    ordered = [(config, m) for configs, machines in multi_team_groups
               for config in [c for c in configs if c in MISCONFIG_CHECKS]
               for m in machines]
    probe_results = iter(run_concurrent(ordered, _probe, max_workers=MAX_CONCURRENCY))

    def _outcome():
        r = next(probe_results)
        # The serial loop only caught TimeoutExpired/CheckError; any other exception
        # escaped check_misconfig_survival, so a non-caught slot must raise here.
        if isinstance(r, Exception):
            raise r
        return r

    for configs, machines in multi_team_groups:
        verifiable = [c for c in configs if c in MISCONFIG_CHECKS]
        for config in verifiable:
            present, absent, unknown = [], [], []
            for m in machines:
                name = m.get("name", m["ip"])
                outcome = _outcome()
                if outcome == "present":
                    present.append(name)
                elif outcome == "absent":
                    absent.append(name)
                else:
                    unknown.append(name)
            if present and absent:
                all_ok = False
                print(f"  FAIL  '{config}' present on {present} but MISSING on {absent} — "
                      f"didn't survive cloning")
            elif absent and not present:
                # D4: previously this matched no branch — no output, all_ok stayed True.
                # Every team's copy reports the config as absent, so the plant never
                # landed anywhere (or no longer matches); that is not a pass.
                all_ok = False
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  FAIL  '{config}' absent on every team ({absent}){note} — the "
                      f"config didn't plant on any clone")
            elif present and not absent:
                note = f" (unverified: {unknown})" if unknown else ""
                print(f"  PASS  '{config}' present on all {len(present)} team(s): {present}{note}")
            elif unknown and not present and not absent:
                any_unverified = True
                print(f"  SKIP  '{config}' — could not verify on any team ({unknown})")
    if not all_ok:
        return gate_fail("misconfig_survival", "misconfig did not survive cloning everywhere")
    if any_unverified:
        return gate_skip("misconfig_survival", "some configs unprovable (SSH dead)")
    return gate_pass("misconfig_survival", "verifiable configs present on every team")
