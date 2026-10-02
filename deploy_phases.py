"""The seven deploy phases, one function per phase, plus their per-phase helpers.

deploy() used to inline all of this in a single 940-line function (CC ~194) whose
phases were `if from_phase <= N:` blocks reading ~160 locals. Each phase here
takes the DeployContext prepare() built and touches only that, so a phase
boundary is a call boundary: a phase can be read, tested, or re-run on its own.

Phases reach back into deploy.py through this module's `deploy` handle for the
four pure helpers that are DEFINED there (phase1_destroy_waves,
golden_rebuild_gate, record_stage_coverage, reset_domain_markers). That
indirection is deliberate, not laziness: those functions read their own module
globals, and tests/test_deploy_phases.py monkeypatches
deploy._is_template / deploy.stored_template_hash / deploy.frozen_gate /
deploy.destroy_vm_if_exists to exercise golden_rebuild_gate offline — a
`from deploy import golden_rebuild_gate` would still work for that, but reaching
through the handle documents where the helper lives and keeps any future
monkeypatch of the helper itself effective. Everything else is imported from the
module that owns it. The handle also keeps the module graph one-directional at
import time: deploy.py imports this module lazily inside deploy(), so
`import deploy` here always resolves to a fully-initialised module (and
`python3 deploy.py` does not trip over a partially-initialised one).
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import deploy
from config_ops import destroy_bridge_if_exists, write_text_atomic
from constants import DEFAULT_ENGINE_MGMT_GW
from engine_ops import bootstrap_scoring_engine
from golden_ops import _is_template, _quote_sshkeys
from jump_ops import build_jump_vms
from nodes_ops import record_of
from range_ops import (destroy_vm_if_exists, proxmox_api, terraform_dir,
                       terraform_plugin_cache_dir)
from routing_ops import verify_satellite_routing
from ssh_ops import (forget_engine_host_key, read_terraform_ctx, wait_for_ssh)
from template_ops import (build_engine_template, destroy_engine_template,
                          engine_hash_inputs, find_engine_template, frozen_gate,
                          hash_from_inputs, load_template_hashes,
                          save_template_hashes, stored_template_hash)
from timing import timed
from utils import compfile_value, run_terraform


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
        lambda vid: _is_template(destroy_node, vid),
        load_template_hashes(ctx.comp_dir), ctx.golden_hashes,
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


def phase2_engine_template(ctx):
    """[2/7] Engine template lifecycle, then terraform apply #1.

    M4: the engine template is built once per competition and reused across its test
    runs — rebuilt only on config drift, never when frozen. Apply #1 then builds the
    engine (a linked clone of that template, seconds) and every team bridge, with
    team_box's for_each deliberately empty; apply #2 in phase 4 flips it on once the
    goldens exist. The checkpoint and the terraform-context read stay in the caller
    (deploy), in that order: a phase-2 resume still needs the outputs, and a run that
    dies between them must leave last_phase at 2 on disk."""
    # --- M4: engine template lifecycle (build once per competition, reuse
    # across its test runs; rebuild only on config drift and never when frozen).
    main_tf_text = Path("terraform/main.tf").read_text()
    quotient_ref = compfile_value(ctx.comp_dir / "Compfile", "quotient_ref")
    engine_inputs = engine_hash_inputs(
        int(os.environ["TF_VAR_template_vm_id"]), quotient_ref,
        main_tf_text, bootstrap_scoring_engine)
    engine_hash = hash_from_inputs(engine_inputs)
    stored = load_template_hashes(ctx.comp_dir)
    tmpl = find_engine_template(ctx.node, ctx.engine_vmid)
    rebuild = True
    if tmpl:
        entry = stored.get("engine") or {}
        if stored_template_hash(ctx.node, tmpl) == engine_hash and entry.get("hash") == engine_hash:
            print(f"  Engine template hash matches — reusing (vmid {tmpl})")
            rebuild = False
        elif not frozen_gate(ctx.comp_dir, entry.get("inputs"), engine_inputs,
                             "engine template"):
            # frozen + code-only drift: frozen_gate warned; keep the frozen template.
            rebuild = False
    if rebuild:
        if tmpl:
            print("  Engine template hash differs — rebuilding...")
            # The old template's only clone is the deployed engine, and the
            # build VM needs the planned mgmt IP — on a phase-2 resume the old
            # engine is still up (phase 1 was skipped), so destroy it here.
            # Apply #1 recreates it as a linked clone of the new template.
            destroy_vm_if_exists(ctx.node, ctx.engine_vmid, expect_tags=ctx.comp_tags)
            destroy_engine_template(ctx.node, ctx.engine_vmid, expect_tags={
                "tezcatlipoca", f"comp-{ctx.comp_name}", "engine-template"})
        ctx_early = {
            "ssh_key_path": ctx.ssh_key_abs,
            "vm_username": os.environ["TF_VAR_vm_username"],
            "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"]),
        }
        with timed(ctx.comp_dir, 2, "engine_template_build"):
            tmpl, build_info = build_engine_template(
                ctx.node, ctx.comp_dir, ctx.engine_vmid,
                int(os.environ["TF_VAR_template_vm_id"]),
                ctx_early, ctx.postgres_password, ctx.redis_password, quotient_ref,
                engine_hash, engine_inputs)
        ctx.state["engine_build_info"] = build_info
    save_template_hashes(ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.state["engine_template_vmid"] = tmpl
    ctx.state["engine_template_hash"] = engine_hash
    ctx.save_state()

    print("[2/7] Terraform apply #1 (engine from template + bridges; team boxes "
          "come in apply #2)...")
    ctx.tfvars["engine_clone_id"] = tmpl
    write_text_atomic(ctx.tfvars_path, json.dumps(ctx.tfvars, indent=2))
    tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
    tf_cwd = str(terraform_dir(ctx.comp_dir))
    run_terraform(["init"], cwd=tf_cwd, env=tf_env, timeout=300)
    # team_box's for_each is empty here (build_team_boxes=false): only the engine
    # (a linked clone of the engine template — seconds, no bulk disk copy) and the
    # bridges are built, plus team_nics' netplan for every team bridge and the
    # cold-boot that surfaces the engine's team NICs.
    with timed(ctx.comp_dir, 2, "terraform_apply"):
        run_terraform(["apply", "-auto-approve", "-parallelism=1"], cwd=tf_cwd, env=tf_env, timeout=2400)

    apply_ctx = read_terraform_ctx(ctx.comp_dir)
    # Fresh engine VM => new host key; drop any stale pin so accept-new re-pins it.
    forget_engine_host_key(apply_ctx["scoring_engine_ip"])
    with timed(ctx.comp_dir, 2, "wait_engine_ssh", apply_ctx["scoring_engine_ip"]):
        wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                     apply_ctx["scoring_engine_ip"], timeout=300)
    if ctx.placement and ctx.placement["satellites"]:
        # Jump/router per satellite, then the fail-loud routing gate: nothing
        # downstream (satellite golden plants, nakon, scoring) works without
        # engine -> jump -> satellite-bridge paths.
        jump_ctx = {"ssh_key_path": ctx.ssh_key_abs,
                    "vm_username": os.environ["TF_VAR_vm_username"],
                    "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"])}
        with timed(ctx.comp_dir, 2, "jump_vms", f"x{len(ctx.placement['satellites'])}"):
            build_jump_vms(ctx.placement, ctx.engine_vmid, jump_ctx, ctx.comp_name,
                           ctx.engine_mgmt_ip,
                           engine_mgmt_gw=os.environ.get("TF_VAR_engine_mgmt_gw",
                                                         DEFAULT_ENGINE_MGMT_GW))
        with timed(ctx.comp_dir, 2, "routing_converge"):
            verify_satellite_routing(ctx.placement, apply_ctx)
    # Record where this state's resources live, for the stale-state guard above.
    ctx.state["deployed_endpoint"] = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
