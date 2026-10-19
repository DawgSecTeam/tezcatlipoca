"""Phase 1: reclaim this competition's interrupted clones (untagged VMs carrying its clone marker), then reset the comp dir."""

from pathlib import Path

from estate import state as estate_state
from engine.template_freeze import engine_template_vmid
from infra.client import proxmox_api
from infra.ownership import MARKER, destroy_vm_if_exists, ownership_verdict
from multinode.placement_record import golden_vmid_for_slot, record_of


def reset_domain_markers(comp_dir):
    # Domain done-markers describe THIS deploy's DCs: a prior run's marker makes the
    # domain pass skip promotion on fresh clones. Fresh deploy resets; resumes keep.
    for stale in Path(comp_dir).glob(".nakon-domain-*.json"):
        stale.unlink()


def planned_vmids(ctx):
    # The reclaim scan is filtered to these — phase 1 never hunts a node for things to delete.
    satellites = (ctx.placement or {}).get("satellites", [])
    slots = [0] + [s["slot"] for s in satellites]
    return ({t["vmid"] for t in ctx.all_targets}
            | {ctx.engine_vmid, engine_template_vmid(ctx.engine_vmid)}
            | {golden_vmid_for_slot(ctx.engine_vmid, slot, i)
               for slot in slots for i in range(len(ctx.boxes))}
            | {s["jump_vmid"] for s in satellites})


def reclaim_interrupted_clones(ctx):
    # Only the MARKER verdict (clone marker, no tags) is destroyed: OWNED is a live
    # range (preflight refused it), MISSING/UNPROVEN is another run's or unknowable.
    wanted = planned_vmids(ctx)
    nodes = [ctx.node] + [record_of(ctx.placement, s["name"]).node
                          for s in (ctx.placement or {}).get("satellites", [])]
    for node in dict.fromkeys(nodes):
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        except Exception as e:
            print(f"  WARNING: could not scan {node} for interrupted clones ({e}) — "
                  f"proceeding")
            continue
        for vm in vms:
            if vm["vmid"] not in wanted:
                continue
            verdict, _ = ownership_verdict(node, vm["vmid"], vm.get("tags"), ctx.reclaim_tags)
            if verdict != MARKER:
                continue
            print(f"  vmid {vm['vmid']} carries this competition's clone marker but no tags "
                  f"(lock={vm.get('lock') or 'none'}) — reclaiming")
            destroy_vm_if_exists(node, vm["vmid"], expect_tags=ctx.reclaim_tags)


def phase1_cleanup(ctx):
    if ctx.from_phase > 1:
        print("[1/8] Skipped (resume) — keeping the run's markers in place.")
        return
    print("[1/8] Reclaiming interrupted clones and resetting local deploy state...")
    reclaim_interrupted_clones(ctx)
    reset_domain_markers(ctx.comp_dir)
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)
    # Fresh deploy starts a new teardown cycle: the comp dir mints its run id once,
    # so a surviving journal would skip the NEW range's steps. Resumes keep it.
    if estate_state.state_path(ctx.comp_dir).exists():
        document = estate_state.EstateState.load(ctx.comp_dir)
        document.section("teardown").clear()
        document.save()
