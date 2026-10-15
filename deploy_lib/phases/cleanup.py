"""Phase 1: reclaim this competition's interrupted clones, then reset the comp dir.

A deploy never tears a RANGE down. A leftover team box, engine, jump VM or bridge is a
preflight refusal (infra.collisions) naming destroy-competition.py as the reclaim path;
this competition's own templates are tolerated there on purpose, because phase 2/4 adopt
them or rebuild them by hash. What is left for phase 1 is the debris no run owns: an
UNTAGGED VM whose description carries this competition's clone marker
(infra.ownership.MARKER — the clone POST wrote the description, a crash or host reboot
kept the tag PUT from ever running), which no other destroy path can claim."""

from pathlib import Path

from estate import state as estate_state
from engine.template_freeze import engine_template_vmid
from infra.client import proxmox_api
from infra.ownership import MARKER, destroy_vm_if_exists, ownership_verdict
from multinode.placement_record import golden_vmid_for_slot, record_of


def reset_domain_markers(comp_dir):
# The per-deploy domain done-markers (.nakon-domain-<team>-adds.json) describe THIS
#     deploy's DCs — run 2's fresh clones were never promoted, but a surviving run-1 artifact
#     made the domain pass skip promotion (live-found 2026-09-25, matrix run 2). A fresh
#     deploy resets them; resumes keep them (that is the guard's whole point). The
#     document's template record is NOT touched — it drives template reuse.
    for stale in Path(comp_dir).glob(".nakon-domain-*.json"):
        stale.unlink()


def planned_vmids(ctx):
# Every vmid this deploy uses: team boxes, the engine, the engine template, each slot's
#     goldens and the satellite jump VMs. The reclaim scan is filtered to these, so phase 1
#     never walks a node looking for things to delete.
    satellites = (ctx.placement or {}).get("satellites", [])
    slots = [0] + [s["slot"] for s in satellites]
    return ({t["vmid"] for t in ctx.all_targets}
            | {ctx.engine_vmid, engine_template_vmid(ctx.engine_vmid)}
            | {golden_vmid_for_slot(ctx.engine_vmid, slot, i)
               for slot in slots for i in range(len(ctx.boxes))}
            | {s["jump_vmid"] for s in satellites})


def reclaim_interrupted_clones(ctx):
# Destroy the untagged VMs at our slots that carry this competition's clone marker. The
#     verdict is the ownership proof every destroy path uses: OWNED means a run still owns
#     the VM (a leftover range, which preflight has already refused), MISSING is another
#     run's, UNPROVEN is unknowable — only MARKER is destroyed, by the guarded destroyer.
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
# [1/8] Reclaim interrupted clones, then reset the comp dir's per-deploy markers.
#
#     Nothing a run owns is touched here: preflight refuses a leftover range and
#     destroy-competition.py reclaims it. Resumes keep their markers (that is the guard's
#     whole point), so this is a fresh-deploy-only phase.
    if ctx.from_phase > 1:
        print("[1/8] Skipped (resume) — keeping the run's markers in place.")
        return
    print("[1/8] Reclaiming interrupted clones and resetting local deploy state...")
    reclaim_interrupted_clones(ctx)
    reset_domain_markers(ctx.comp_dir)
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)
    # A fresh deploy starts a new teardown cycle: the journal of the PREVIOUS
    # teardown must not survive into this run's — with a reused run id (the
    # comp dir mints it once) the stale journal would skip red01/artifacts of
    # the NEW range (live-found 2026-10-12: '[red01] skipped — journalled done'
    # on the redeployed range). Resumes of THIS deploy keep it (that is the
    # resumability contract).
    if estate_state.state_path(ctx.comp_dir).exists():
        document = estate_state.EstateState.load(ctx.comp_dir)
        document.section("teardown").clear()
        document.save()
