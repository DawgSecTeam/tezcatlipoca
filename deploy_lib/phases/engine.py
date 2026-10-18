"""Phases 2 and 3: the engine template + the engine and its bridges, then per-deploy engine prep."""


from common.constants import DEFAULT_ENGINE_MGMT_GW, ENGINE_TEMPLATE_NAME
from engine.engine_bootstrap_ops import bootstrap_scoring_engine
from engine.engine_event_ops import push_event_conf
from engine.engine_health_ops import ensure_nat_forwarding
from engine.engine_identity_ops import prepare_engine_from_template
from golden.golden_target_ops import _quote_sshkeys
from net.jump_ops import build_jump_vms, red_segment_from_env
from infra.ownership import destroy_vm_if_exists, retag_ownership
from multinode.placement_record import record_of, resolved_mgmt_bridge
from multinode.routing_ops import verify_satellite_routing
from common.ssh_ops import forget_engine_host_key, wait_for_ssh
from engine.engine_template_ops import build_engine_template, destroy_engine_template, find_engine_template
from engine.template_freeze import (
    engine_hash_inputs, frozen_gate, hash_from_inputs, load_template_hashes, save_template_hashes,
    template_stamp_current,
)
from common.timing import timed
from common.utils import compfile_flag, compfile_value

from deploy_lib.phases._estate import (configure_engine_network, create_engine_and_bridges,
                                       engine_context)
from estate import config


def engine_build_ctx(ctx):
# The ssh/user context the engine-template build VM and the jump VMs are built with.
    return {
        "ssh_key_path": ctx.ssh_key_abs,
        "vm_username": config.active().engine.username,
        "ssh_public_key_quoted": _quote_sshkeys(config.active().ssh.public_key),
    }


def ensure_engine_template(ctx):
# M4 engine template lifecycle: build once per competition, reuse across its test
#     runs; rebuild only on config drift and never when frozen. Records the template vmid
#     and hash in state and the hash file; returns the template vmid.
    from estate import apply as estate_apply
    quotient_ref = compfile_value(ctx.comp_dir / "Compfile", "quotient_ref")
    engine_inputs = engine_hash_inputs(
        int(config.active().require("proxmox.template_vm_id")), quotient_ref,
        estate_apply.create_engine, bootstrap_scoring_engine)
    engine_hash = hash_from_inputs(engine_inputs)
    stored = load_template_hashes(ctx.comp_dir)
    tmpl = find_engine_template(ctx.node, ctx.engine_vmid)
    rebuild = True
    if tmpl:
        entry = stored.get("engine") or {}
        # The node's own hash stamp decides reuse, so a deploy from a fresh workspace
        # (whose hash record the dashboard's seeder stripped) still adopts the
        # engine template a --teams-only teardown kept. The file entry is the corroborating
        # record, not the authority — see engine.template_freeze.template_stamp_current.
        if template_stamp_current(ctx.node, tmpl, engine_hash):
            if entry.get("hash") != engine_hash:
                print("  Engine template reuses on the node's hash stamp (no local hash "
                      f"record in this worktree) — keeping (vmid {tmpl})")
            else:
                print(f"  Engine template hash matches — reusing (vmid {tmpl})")
            rebuild = False
        elif not frozen_gate(ctx.comp_dir, entry.get("inputs"), engine_inputs,
                             "engine template"):
            # frozen + code-only drift: frozen_gate warned; keep the frozen template.
            rebuild = False
    if rebuild:
        if tmpl:
            print("  Engine template hash differs — rebuilding...")
            # The old template's only clone is the deployed engine, and the build VM
            # needs the planned mgmt IP: on a phase-2 resume the old engine is still up
            # (phase 1 leaves a run's VMs alone), so destroy it here.
            # Apply #1 recreates it as a linked clone of the new template.
            destroy_vm_if_exists(ctx.node, ctx.engine_vmid, expect_tags=ctx.reclaim_tags)
            left = destroy_engine_template(
                ctx.node, ctx.engine_vmid,
                expect_tags=ctx.reclaim_tags | {"engine-template"})
            if left:
                from infra.ownership import Refusal
                raise Refusal(
                    saw="; ".join(left),
                    expected="the engine-template slot free, or a template this run owns",
                    remedy="let the run that owns it finish (or run its teardown), then "
                           "resume the deploy",
                    detail="cannot rebuild the engine template over another run's VM: "
                           + "; ".join(left))
        with timed(ctx.comp_dir, 2, "engine_template_build"):
            tmpl, build_info = build_engine_template(
                ctx.node, ctx.comp_dir, ctx.engine_vmid,
                int(config.require("proxmox.template_vm_id")),
                engine_build_ctx(ctx), ctx.postgres_password, ctx.redis_password, quotient_ref,
                engine_hash, engine_inputs, run_id=ctx.run_id,
                mgmt_bridge=resolved_mgmt_bridge(ctx.placement))
        ctx.state["engine_build_info"] = build_info
    elif tmpl:
        # Kept (hash match or frozen): re-stamp its ownership to THIS run so the
        # next teardown's strict guard and preflight recognize it — a template kept
        # across runs carries the earlier run's tag (infra.ownership.retag_ownership).
        retag_ownership(ctx.node, tmpl, ctx.comp_tags | {"engine-template"})
    save_template_hashes(ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.state["engine_template_vmid"] = tmpl
    ctx.state["engine_template_hash"] = engine_hash
    ctx.save_state()
    # Inventory it: the teardown destroys what the document records, in the one order
    # linked clones survive.
    document = ctx.document()
    document.record("engine_template", ctx.node, ENGINE_TEMPLATE_NAME, vmid=tmpl)
    document.save()
    return tmpl


def build_satellite_jumps(ctx, apply_ctx):
# Jump/router per satellite, then the fail-loud routing gate: nothing
#     downstream (satellite golden plants, nakon, scoring) works without
#     engine -> jump -> satellite-bridge paths.
    with timed(ctx.comp_dir, 2, "jump_vms", f"x{len(ctx.placement['satellites'])}"):
        build_jump_vms(ctx.placement, ctx.engine_vmid, engine_build_ctx(ctx), ctx.comp_name,
                       ctx.engine_mgmt_ip,
                       engine_mgmt_gw=(config.active().engine.mgmt_gw
                                       or DEFAULT_ENGINE_MGMT_GW),
                       run_id=ctx.run_id,
                       red_segment=red_segment_from_env())
    with timed(ctx.comp_dir, 2, "routing_converge"):
        verify_satellite_routing(ctx.placement, apply_ctx)
    document = ctx.document()
    for sat in ctx.placement["satellites"]:
        node = record_of(ctx.placement, sat["name"]).node
        document.record("jump", node, f"jump-{sat['name']}", vmid=sat["jump_vmid"])
    document.save()


def phase2_engine_template(ctx):
# [2/8] Engine template lifecycle, then the engine and its bridges.
#
#     M4: the engine template is built once per competition and reused across its test
#     runs — rebuilt only on config drift, never when frozen. The engine is then a linked
#     clone of that template; every team bridge is built now, and phase 4 clones the team
#     boxes once the goldens exist. The checkpoint and the engine-context read stay in the
#     caller (deploy), in that order: a phase-2 resume still needs the coordinates, and a
#     run that dies between them must leave last_phase at 2 on disk.
    if ctx.from_phase > 2:
        print("[2/8] Skipped (resume) — not re-creating the engine.")
        return
    ensure_engine_template(ctx)

    print("[2/8] Creating the team bridges and the scoring engine (team boxes come in "
          "phase 4)...")
    with timed(ctx.comp_dir, 2, "create_engine"):
        create_engine_and_bridges(ctx)

    apply_ctx = ctx.tf_ctx = engine_context(ctx)
    # Fresh engine VM => new host key; drop any stale pin so accept-new re-pins it.
    forget_engine_host_key(apply_ctx["scoring_engine_ip"])
    with timed(ctx.comp_dir, 2, "wait_engine_ssh", apply_ctx["scoring_engine_ip"]):
        wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                     apply_ctx["scoring_engine_ip"], timeout=300)
    with timed(ctx.comp_dir, 2, "engine_network"):
        configure_engine_network(ctx, apply_ctx)
    if ctx.placement and ctx.placement["satellites"]:
        build_satellite_jumps(ctx, apply_ctx)
    # Record where this state's resources live: teardown's ownership refusal compares the
    # loaded endpoint against it.
    ctx.state["deployed_endpoint"] = config.active().proxmox.endpoint.rstrip("/")


def phase3_prepare_engine(ctx):
# [3/8] Apply this deploy's per-competition state to the engine clone.
    if ctx.from_phase > 3:
        print("[3/8] Skipped (resume).")
        return
    # M4: the deployed engine is a linked clone of the engine template — the
    # heavy bootstrap ran once on the template build VM. Per-deploy state is
    # applied fresh here: .env (BEFORE compose up, so the fresh postgres volume
    # initializes with this competition's credentials), fresh-volume compose up
    # (an empty scoring DB every run), and the cacher check.
    print("[3/8] Preparing scoring engine from template (fresh volumes, event.conf)...")
    with timed(ctx.comp_dir, 3, "engine_from_template"):
        prepare_engine_from_template(ctx.tf_ctx, ctx.postgres_password, ctx.redis_password)

    print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
    with timed(ctx.comp_dir, 3, "push_event_conf"):
        push_event_conf(ctx.comp_dir, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.name,
                        inject_password=ctx.inject_password, admin_password=ctx.admin_password,
                        scoring_password=ctx.scoring_password,
                        postgres_password=ctx.postgres_password, redis_password=ctx.redis_password,
                        box_creds=ctx.box_creds,
                        extra_credlists=({"domain": ctx.domain_creds}
                                         if ctx.domain_creds else None))

    ensure_nat_forwarding(ctx.tf_ctx)

    # Remote access (headscale): every deploy enrolls the engine as a subnet router,
    # pushes the tailnet->team SNAT unit, and re-applies this competition's scoped
    # ACL block + participant keys. `remote_access 0` in the Compfile opts out; a
    # failure here fails the deploy — an unreachable headscale must not quietly
    # yield a range nobody can reach.
    if compfile_flag(ctx.comp_dir / "Compfile", "remote_access", 1):
        with timed(ctx.comp_dir, 3, "remote_access"):
            from net import remote_access_ops
            remote_access_ops.setup_remote_access(ctx)
    else:
        print("  Remote access: skipped (Compfile remote_access 0).")

