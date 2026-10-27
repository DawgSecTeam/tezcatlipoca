"""Reclaim an orphaned run: the VMs a discarded comp dir left behind."""

import os

import requests

from common.constants import comp_tag
from infra.client import destroy_bridge_if_exists, node_bridges, proxmox_api
from infra.ownership import (Ownership, Refusal, bridge_owned_by, destroy_vm_if_exists,
                             parse_vm_tags)

TEMPLATE_ROLES = ("tezcatlipoca-golden", "engine-template")
RETIRE_PATH = "/api/ranges/retire"


def refuse_when_state_exists(comp, run_tag):
# orphan mode is for a run whose comp dir is gone; a dir that still holds the run id is the sanctioned path
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
# Dashboard address + bearer, inherited from the parent; a manual host has none and that must not fail the reclaim.
    # Deliberately NO .env fallback: a stray file would let a reclaim retire a range on
    # whatever dashboard it names. The handoff exists only in the parent's environment.
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
# A reclaim is not a run, so the dashboard store is only updated here; reporting must never fail the reclaim.
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


def reclaim(comp, run_tag, *, node, nodes=None, allow_templates=False, log=print):
# Destroys VMs carrying {tezcatlipoca, comp-<comp>, <run_tag>}; `remaining` non-empty means the reclaim did not finish.
    ownership = Ownership(comp, run_tag)
    nodes = list(dict.fromkeys([node, *(nodes or [])]))
    vms = []
    for n in nodes:
        try:
            vms += [dict(v, _node=n) for v in proxmox_api("GET", f"/nodes/{n}/qemu")["data"]]
        except Exception as e:                               # noqa: BLE001 - reported below
            log(f"  WARNING: could not list VMs on {n}: {e}")
    destroyed, kept, notes = [], [], []
    # Non-templates FIRST: a linked clone's base volume still in use makes the template's
    # destroy fail, and vmid order puts a golden (1xxx) before its team boxes (12xx+).
    ordered = sorted(vms, key=lambda v: (bool(parse_vm_tags(v.get("tags")) & set(TEMPLATE_ROLES)),
                                         v["vmid"]))
    for vm in ordered:
        tags = parse_vm_tags(vm.get("tags"))
        if comp_tag(comp) in tags and not ownership.tags <= tags:
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
        destroy_vm_if_exists(vm["_node"], vm["vmid"],
                             expect_tags=ownership.with_role(*role).tags, require_owned=True)
        destroyed.append({"vmid": vm["vmid"], "name": vm.get("name"),
                          "role": "+".join(sorted(role)) or "vm"})
    # Bridges carry no tags, so their comment is the whole proof: only this run's come out.
    bridges = []
    for n in nodes:
        for iface, comment in node_bridges(n).items():
            if not iface.startswith("vmbr"):
                continue
            if bridge_owned_by(comment, comp, run_tag):
                log(f"  bridge {iface} on {n} ({comment})")
                if destroy_bridge_if_exists(n, iface):
                    bridges.append(iface)
            elif "comp-" in comment or "tezcatlipoca" in comment:
                notes.append(f"bridge {iface} on {n} is another run's ({comment}) — not touched")
            elif iface != "vmbr0" and iface[4:].isdigit():
                notes.append(f"bridge {iface} on {n} carries no ownership comment — not touched")
    remaining = []
    for n in nodes:
        try:
            remaining += [v["vmid"] for v in proxmox_api("GET", f"/nodes/{n}/qemu")["data"]
                          if v["vmid"] in {d["vmid"] for d in destroyed}]
        except Exception:                                    # noqa: BLE001 - already warned
            pass
    # A leftover means the reclaim did not finish, so the range still exists and must stay live.
    if not remaining:
        retire_range(run_tag, log=log)
    return {"destroyed": destroyed, "kept_templates": kept, "notes": notes,
            "bridges": bridges, "remaining": remaining, "scanned": len(vms),
            "nodes": nodes}
