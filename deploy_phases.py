"""The seven deploy phases, one function per phase, plus their per-phase helpers.

deploy() used to inline all of this in a single 940-line function (CC ~194) whose
phases were `if from_phase <= N:` blocks reading ~160 locals. Each phase here
takes the DeployContext prepare() built and touches only that, so a phase
boundary is a call boundary: a phase can be read, tested, or re-run on its own.

Phases reach back into deploy.py through this module's `deploy` handle for the
pure helpers that live there (phase1_destroy_waves, golden_rebuild_gate,
record_stage_coverage, reset_domain_markers) and for the names those helpers read
from deploy.py's globals (_is_template, load_template_hashes). That indirection
is deliberate, not laziness: tests/test_deploy_phases.py monkeypatches
deploy._is_template / deploy.stored_template_hash / deploy.frozen_gate /
deploy.destroy_vm_if_exists, and a `from deploy import phase1_destroy_waves`
would freeze the original function object, making those patches — and any patch
of the phase helpers themselves — silently ineffective. It also keeps the module
graph one-directional at import time: deploy.py imports this module lazily
inside deploy(), so `import deploy` here always resolves to a fully-initialised
module (and `python3 deploy.py` does not trip over a partially-initialised one).
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

import deploy
from config_ops import destroy_bridge_if_exists
from nodes_ops import record_of
from range_ops import destroy_vm_if_exists, proxmox_api
from timing import timed


def destroy_owned(ctx, destroy_node, vmid, vm_name):
    """Destroy one VM if it is ours (tag-guarded), timed under phase 1."""
    with timed(ctx.comp_dir, 1, "destroy_vm", vm_name):
        legacy_name = vm_name if vm_name.startswith("golden-") else None
        destroy_vm_if_exists(destroy_node, vmid, expect_tags=ctx.comp_tags,
                             legacy_name=legacy_name)


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
        node_vms = []
    wave1, wave2 = deploy.phase1_destroy_waves(
        node_vms, node_targets, ctx.legacy_clones if slot == 0 else {},
        ctx.engine_vmid, ctx.boxes, ctx.comp_tags,
        lambda vid: deploy._is_template(destroy_node, vid),
        deploy.load_template_hashes(ctx.comp_dir), ctx.golden_hashes,
        frozen_keep=ctx.frozen_keep, slot=slot, extra_destroy=extra_destroy)
    destroy_pool(ctx, wave1, destroy_node)
    destroy_pool(ctx, wave2, destroy_node)


def phase1_cleanup(ctx):
    """[1/7] Tear down the previous range: team boxes first, then their templates.

    Wave 1: every team box (the computed set covers ALL teams now that terraform
    builds them) plus any legacy API clones from a pre-golden range. Linked
    clones must die BEFORE their templates. Wave 2 (goldens/engine/stale slots)
    is phase1_destroy_waves' job; this function is only the node/bridge walk and
    the resume markers around it."""
    print("[1/7] Cleaning up previous deployment (parallel; deletes are metadata-light "
          "— the datastore-saturation hazard belongs to bulk clone writes, not deletes)...")
    cloned_path = ctx.comp_dir / "cloned_vms.json"
    ctx.legacy_clones = {}
    if cloned_path.exists():
        try:
            ctx.legacy_clones = {int(v): str(k) for k, v in json.loads(cloned_path.read_text()).items()}
        except (ValueError, TypeError, OSError):
            print("  WARNING: could not parse cloned_vms.json — relying on computed vmids")

    # Wave 1: every team box (the computed set covers ALL teams now that terraform
    # builds them) plus any legacy API clones from a pre-golden range. Linked
    # clones must die BEFORE their templates.
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
                with timed(ctx.comp_dir, 1, "destroy_bridge", f"vmbr{ctx.teams[team_key]['identifier']}"):
                    destroy_bridge_if_exists(sat_rec.node, f"vmbr{ctx.teams[team_key]['identifier']}")
    for team_key, team in ctx.teams.items():
        if ctx.placement and ctx.placement["team_nodes"][team_key] != ctx.placement["engine_node"]:
            continue  # satellite bridge — destroyed above on its own host
        with timed(ctx.comp_dir, 1, "destroy_bridge", f"vmbr{team['identifier']}"):
            destroy_bridge_if_exists(ctx.node, f"vmbr{team['identifier']}")
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)
    deploy.reset_domain_markers(ctx.comp_dir)
    time.sleep(5)
