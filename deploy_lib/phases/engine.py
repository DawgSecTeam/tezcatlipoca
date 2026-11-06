"""Phases 2 and 3: the engine, its template/image lifecycle and bridges, then per-deploy engine prep."""


import providers

from common.constants import DEFAULT_ENGINE_MGMT_GW
from engine.engine_event_ops import push_event_conf
from engine.engine_health_ops import ensure_nat_forwarding
from engine.engine_identity_ops import prepare_engine_from_template
from common.ssh_ops import quote_sshkeys
from net.jump_ops import build_jump_vms, red_segment_from_env
from multinode.placement_record import record_of
from multinode.routing_ops import verify_satellite_routing
from common.ssh_ops import forget_engine_host_key, wait_for_ssh
from common.timing import timed
from common.utils import compfile_flag, compfile_value

from deploy_lib.phases._estate import (configure_engine_network, create_engine_and_bridges,
                                       engine_context)
from estate import config


def engine_build_ctx(ctx):
    return {
        "ssh_key_path": ctx.ssh_key_abs,
        "vm_username": config.active().engine.username,
        "ssh_public_key_quoted": quote_sshkeys(config.active().ssh.public_key),
    }


def ensure_engine_template(ctx):
    providers.of_ctx(ctx).engine_template(ctx)


def build_satellite_jumps(ctx, apply_ctx):
# fail-loud: nothing downstream (golden plants, nakon, scoring) works without the engine -> jump -> satellite-bridge paths.
    # A satellite team's gateway is its jump, so the jump needs the C2 callback DNAT to carry
    # a payload's check-in to red01 (phase 7 would otherwise install it only on the engine).
    from red import gaia_ops
    callback_port = gaia_ops.c2_plan(ctx.comp_dir)["callback_port"]
    with timed(ctx.comp_dir, 2, "jump_vms", f"x{len(ctx.placement['satellites'])}"):
        build_jump_vms(ctx.placement, ctx.engine_vmid, engine_build_ctx(ctx), ctx.comp_name,
                       ctx.engine_mgmt_ip,
                       engine_mgmt_gw=(config.active().engine.mgmt_gw
                                       or DEFAULT_ENGINE_MGMT_GW),
                       run_id=ctx.run_id,
                       red_segment=red_segment_from_env(),
                       callback_port=callback_port)
    with timed(ctx.comp_dir, 2, "routing_converge"):
        verify_satellite_routing(ctx.placement, apply_ctx)
    document = ctx.document()
    for sat in ctx.placement["satellites"]:
        node = record_of(ctx.placement, sat["name"]).node
        document.record("jump", node, f"jump-{sat['name']}", vmid=sat["jump_vmid"])
    document.save()


def phase2_engine_template(ctx):
# the checkpoint and the engine-context read stay in the caller (deploy), in that order: a
# phase-2 resume still needs the coordinates; a run dying between them must leave last_phase 2.
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
    # teardown's ownership refusal compares the loaded endpoint against this record
    ctx.state["deployed_endpoint"] = (providers.endpoint_label(ctx.platform)
                                      if ctx.platform == providers.GCP
                                      else config.active().proxmox.endpoint.rstrip("/"))


def phase3_prepare_engine(ctx):
    if ctx.from_phase > 3:
        print("[3/8] Skipped (resume).")
        return
    # .env lands BEFORE compose up: the fresh postgres volume initializes with this run's credentials
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

    # a headscale failure fails the deploy: an unreachable range must not be handed out quietly
    if compfile_flag(ctx.comp_dir / "Compfile", "remote_access", 1):
        with timed(ctx.comp_dir, 3, "remote_access"):
            from net import remote_access_ops
            remote_access_ops.setup_remote_access(ctx)
    else:
        print("  Remote access: skipped (Compfile remote_access 0).")

