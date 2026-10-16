"""Reclaim an orphaned run: the VMs a discarded comp dir left behind.

Every other destroy path reads its `Ownership` from a comp dir's state, so a workspace that is
already gone strands its VMs — preflight refuses them as a leftover range and teardown refuses
for want of a run id. This closes that hole with the SAME proof, never a weaker one: the caller
names the comp tag and the run tag, and a VM is touched only when its own tag set carries both
plus `tezcatlipoca`. Two deliberate narrowings: the clone-marker adoption the other paths allow
is refused (`require_owned=True` — a marker names the competition, not the run, so it can be a
live worktree's interrupted clone), and templates (golden / engine-template) are irreversible
and need `allow_templates` because there is no freeze record left to consult. Bridges have no
tags, so they come out only when their PVE comment proves this competition and run id (see
`infra.ownership.bridge_comment`); an uncommented or foreign bridge is named and left alone. No
state document, no terminal plan."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from infra.client import destroy_bridge_if_exists, node_bridges, proxmox_api
from infra.ownership import (Ownership, Refusal, bridge_owned_by, destroy_vm_if_exists,
                             parse_vm_tags)

TEMPLATE_ROLES = ("tezcatlipoca-golden", "engine-template")
REPO_ROOT = Path(__file__).resolve().parents[1]
RETIRE_PATH = "/api/ranges/retire"


def refuse_when_state_exists(comp, run_tag):
# The orphan mode is for a run whose comp dir is gone. If some comp dir still owns this run id,
# that dir is the sanctioned path — and the only one that can consult the freeze record.
    from common.comp_paths import comps_root
    from estate import state as estate_state
    root = comps_root()
    if not root.is_dir():
        return
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        if (estate_state.read_flat(candidate) or {}).get("run_id") == run_tag:
            raise Refusal(
                saw=f"{candidate} holds state for {run_tag}",
                expected="a run whose comp dir no longer exists",
                remedy=f"teardown the range from its own state: "
                       f"destroy-competition.py --comp-dir {candidate} --yes")


def dashboard_handoff():
# The dashboard's address + bearer, which it hands every driver it starts (its own
# TEZ_DASHBOARD_* are inherited by the child process). Not estate config: a manual host has
# none, and that must not fail the reclaim — retire_range prints the call instead.
    if not (os.environ.get("TEZ_DASHBOARD_HOST") and os.environ.get("TEZ_DASHBOARD_TOKENS")):
        load_dotenv(REPO_ROOT / ".env")
    host = os.environ.get("TEZ_DASHBOARD_HOST", "")
    # The service BINDS 0.0.0.0, which is a wildcard, not a connect address.
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    tokens = os.environ.get("TEZ_DASHBOARD_TOKENS", "")
    token = next((p.split("=", 1)[1].strip() for p in tokens.split(",") if "=" in p), "")
    if not (host and token):
        return "", ""
    return f"http://{host}:{os.environ.get('TEZ_DASHBOARD_PORT') or 8787}", token


def _retire_call(run_tag):
    return ("    curl -fsS -X POST http://<dashboard-host>:<port>/api/ranges/retire "
            "-H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' "
            f"-d '{{\"run\": \"{run_tag}\", \"by\": \"orphan reclaim\"}}'")


def retire_range(run_tag, *, log=print):
# A reclaim is not a run, so no destroy reaches the dashboard store and the board keeps
# reading the range live (the defect this closes). Reporting must never fail the reclaim, but
# it must never be silent either.
    base, token = dashboard_handoff()
    if not (base and token):
        log("  dashboard    : no handoff (TEZ_DASHBOARD_HOST/TEZ_DASHBOARD_TOKENS) — the board "
            "still reads this range as live; retire it with:")
        log(_retire_call(run_tag))
        return None
    try:
        resp = requests.request("POST", base + RETIRE_PATH,
                                json={"run": run_tag, "by": "orphan reclaim"},
                                headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException as exc:
        log(f"  dashboard    : unreachable ({exc}) — the board was NOT updated; retire it with:")
        log(_retire_call(run_tag))
        return None
    if resp.status_code != 200:
        try:
            detail = str(resp.json().get("detail", "")).strip()
        except ValueError:
            detail = resp.text.strip()
        log(f"  dashboard    : refused ({resp.status_code}) {detail} — the board was NOT updated")
        return None
    view = resp.json()
    log(f"  dashboard    : retired {view.get('key')} (state {view.get('state')})")
    return view


def reclaim(comp, run_tag, *, node, allow_templates=False, log=print):
# Destroy the VMs carrying {tezcatlipoca, comp-<comp>, <run_tag>}. Returns destroyed /
#     kept-templates / notes / remaining; `remaining` non-empty means the reclaim did not finish.
    ownership = Ownership(comp, run_tag)
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    destroyed, kept, notes = [], [], []
    for vm in sorted(vms, key=lambda v: v["vmid"]):
        tags = parse_vm_tags(vm.get("tags"))
        if f"comp-{comp}" in tags and not ownership.tags <= tags:
            others = sorted(t for t in tags if t.startswith("run-"))
            notes.append(f"vmid {vm['vmid']} ({vm.get('name')}) is comp-{comp} but carries "
                         f"{', '.join(others) or 'no run tag'}, not {run_tag} — not touched")
            continue
        if not ownership.tags <= tags:
            continue
        role = tags & set(TEMPLATE_ROLES)
        if role and not allow_templates:
            kept.append({"vmid": vm["vmid"], "name": vm.get("name"), "role": sorted(role)})
            continue
        log(f"  vmid {vm['vmid']} ({vm.get('name')}) — "
            f"{'+'.join(sorted(role)) or 'plain vm'}")
        destroy_vm_if_exists(node, vm["vmid"],
                             expect_tags=ownership.with_role(*role).tags, require_owned=True)
        destroyed.append({"vmid": vm["vmid"], "name": vm.get("name"),
                          "role": "+".join(sorted(role)) or "vm"})
    # Bridges carry no tags, so their comment is the whole proof. Only this run's come out; a
    # foreign or uncommented bridge is left alone and named.
    bridges = []
    for iface, comment in node_bridges(node).items():
        if not iface.startswith("vmbr"):
            continue
        if bridge_owned_by(comment, comp, run_tag):
            log(f"  bridge {iface} ({comment})")
            if destroy_bridge_if_exists(node, iface):
                bridges.append(iface)
        elif "comp-" in comment or "tezcatlipoca" in comment:
            notes.append(f"bridge {iface} is another run's ({comment}) — not touched")
        elif iface != "vmbr0" and iface[4:].isdigit():
            notes.append(f"bridge {iface} carries no ownership comment — not touched")
    remaining = [v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                 if v["vmid"] in {d["vmid"] for d in destroyed}]
    # A leftover means the reclaim did not finish, so the range still exists and the board
    # must keep showing it live rather than retire it.
    if not remaining:
        retire_range(run_tag, log=log)
    return {"destroyed": destroyed, "kept_templates": kept, "notes": notes,
            "bridges": bridges, "remaining": remaining, "scanned": len(vms)}
