"""Phase 1: tear down the previous range (team boxes, then their templates, then bridges)."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config_ops import destroy_bridge_if_exists
from golden_ops import _is_template
from nodes_ops import record_of
from range_ops import destroy_vm_if_exists, proxmox_api
from template_ops import load_template_hashes
from timing import timed
from utils import is_in_path_fw, record_degradation

from deploy_lib.golden_plan import phase1_destroy_waves


def reset_domain_markers(comp_dir):
    """The per-deploy domain done-markers (.nakon-domain-<team>-adds.json) describe THIS
    deploy's DCs — run 2's fresh clones were never promoted, but a surviving run-1 artifact
    made the domain pass skip promotion (live-found 2026-09-25, matrix run 2). A fresh
    deploy resets them; resumes keep them (that is the guard's whole point). The
    per-COMPETITION .template-hashes.json is NOT touched — it drives template reuse."""
    for stale in Path(comp_dir).glob(".nakon-domain-*.json"):
        stale.unlink()


def destroy_owned(ctx, destroy_node, vmid, vm_name):
    """Destroy one VM if it is OURS (prior run's ownership, tag-guarded), timed under
    phase 1. ctx.reclaim_tags carries the PRIOR state's run id — a same-comp VM
    without it belongs to another worktree's run and is refused (2026-10-02
    near-miss); untagged VMs are refused unless they carry this comp's clone marker."""
    with timed(ctx.comp_dir, 1, "destroy_vm", vm_name):
        destroy_vm_if_exists(destroy_node, vmid, expect_tags=ctx.reclaim_tags)


def destroy_pool(ctx, vmid_map, destroy_node):
    """Destroy one wave in parallel (bounded like every other per-box bulk pass)."""
    workers = min(8, len(vmid_map)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(destroy_owned, ctx, destroy_node, vmid, name)
                   for vmid, name in sorted(vmid_map.items())]
        for fut in futures:
            fut.result()


def destroy_node_waves(ctx, destroy_node, node_targets, slot, extra_destroy=None):
    """One wave pair per hosting node, that node's VMs and its slot's golden span
    (multi-node) — the engine node keeps the historical slot-0 behavior."""
    try:
        node_vms = proxmox_api("GET", f"/nodes/{destroy_node}/qemu")["data"]
    except Exception as e:
        print(f"  WARNING: could not scan {destroy_node} for stranded "
              f"clones ({e}) — proceeding")
        record_degradation("could not scan node for stranded clones", f"{destroy_node}: {e}")
        node_vms = []
    wave1, wave2 = phase1_destroy_waves(
        node_vms, node_targets,
        ctx.engine_vmid, ctx.boxes, ctx.reclaim_tags,
        lambda vid: _is_template(destroy_node, vid),
        load_template_hashes(ctx.comp_dir), ctx.golden_hashes,
        frozen_keep=ctx.frozen_keep, slot=slot, extra_destroy=extra_destroy)
    destroy_pool(ctx, wave1, destroy_node)
    destroy_pool(ctx, wave2, destroy_node)


def _bridge_in_use(node, bridge):
    """True when any VM on the node still has a NIC on this bridge.

    Bridges carry no tags of their own and their names (vmbr<team identifier>)
    collide across same-comp deploys sharing team identifiers — phase 1 must not
    rip a bridge out from under another run's VMs. Unreadable configs count as
    in use: fail closed, the bridge survives to teardown/terraform."""
    try:
        vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    except Exception:
        return True
    for v in vms:
        try:
            cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{v['vmid']}/config")["data"]
        except Exception:
            return True
        if any(f"bridge={bridge}" in str(cfg.get(k) or "")
               for k in cfg if str(k).startswith("net")):
            return True
    return False


def _reclaim_bridge(ctx, node, bridge):
    """Destroy a team bridge only when nothing is still attached to it (see
    _bridge_in_use) — a foreign/other-run holder survives with a loud warning."""
    if _bridge_in_use(node, bridge):
        print(f"  WARNING: {bridge} on {node} still has attached VM(s) — NOT destroying "
              f"it (another run's range may share the team identifier)")
        return
    with timed(ctx.comp_dir, 1, "destroy_bridge", bridge):
        destroy_bridge_if_exists(node, bridge)


def phase1_cleanup(ctx):
    """[1/8] Tear down the previous range: team boxes first, then their templates.

    Wave 1: every team box (the computed set covers ALL teams now that terraform
    builds them). Linked clones must die BEFORE their templates. Wave 2 (goldens/engine/stale slots)
    is phase1_destroy_waves' job; this function is only the node/bridge walk and
    the resume markers around it."""
    if ctx.from_phase > 1:
        print("[1/8] Skipped (resume) — leaving existing VMs/bridges in place.")
        return
    print("[1/8] Cleaning up previous deployment (parallel; deletes are metadata-light "
          "— the datastore-saturation hazard belongs to bulk clone writes, not deletes)...")
    # Wave 1: every team box (the computed set covers ALL teams now that terraform
    # builds them). Linked clones must die BEFORE their templates.
    destroy_node_waves(ctx, ctx.node,
                       [t for t in ctx.all_targets if t.get("node") == ctx.node], 0)
    if ctx.placement:
        for sat in ctx.placement["satellites"]:
            sat_rec = record_of(ctx.placement, sat["name"])
            destroy_node_waves(
                ctx, sat_rec.node,
                [t for t in ctx.all_targets if t.get("node") == sat_rec.node],
                sat["slot"],
                extra_destroy={sat["jump_vmid"]: f"jump-{ctx.comp_name}-{sat['slot']}"})
            for team_key in ctx.placement["team_nodes"]:
                if ctx.placement["team_nodes"][team_key] != sat["name"]:
                    continue
                _reclaim_bridge(ctx, sat_rec.node, f"vmbr{ctx.teams[team_key]['identifier']}")
    for team_key, team in ctx.teams.items():
        if ctx.placement and ctx.placement["team_nodes"][team_key] != ctx.placement["engine_node"]:
            continue  # satellite bridge — destroyed above on its own host
        _reclaim_bridge(ctx, ctx.node, f"vmbr{team['identifier']}")
        if any(is_in_path_fw(b) for b in ctx.boxes):
            # In-path firewalls add an engine-node transit bridge per team — same
            # in-use guard, same "another run may share the identifier" tolerance.
            _reclaim_bridge(ctx, ctx.node, f"vmbrW{team['identifier']}")
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)
    reset_domain_markers(ctx.comp_dir)
    time.sleep(5)
