"""Phases 2 and 3: the engine template + terraform apply #1, then per-deploy engine prep."""

import os

from constants import DEFAULT_ENGINE_MGMT_GW
from engine_ops import (bootstrap_scoring_engine, ensure_nat_forwarding,
                        install_round_loop_guard, prepare_engine_from_template,
                        push_event_conf)
from golden_ops import _quote_sshkeys
from jump_ops import build_jump_vms, red_segment_from_env
from range_ops import destroy_vm_if_exists, retag_ownership
from routing_ops import verify_satellite_routing
from ssh_ops import forget_engine_host_key, read_terraform_ctx, wait_for_ssh
from template_ops import (build_engine_template, destroy_engine_template,
                          engine_hash_inputs, find_engine_template, frozen_gate,
                          hash_from_inputs, load_template_hashes,
                          save_template_hashes, stored_template_hash)
from timing import timed
from utils import compfile_flag, compfile_value, run_terraform

from deploy_lib.phases._terraform import terraform_env_and_cwd, write_tfvars
from pathlib import Path


def engine_build_ctx(ctx):
    """The ssh/user context the engine-template build VM and the jump VMs are built with."""
    return {
        "ssh_key_path": ctx.ssh_key_abs,
        "vm_username": os.environ["TF_VAR_vm_username"],
        "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"]),
    }


def ensure_engine_template(ctx):
    """M4 engine template lifecycle: build once per competition, reuse across its test
    runs; rebuild only on config drift and never when frozen. Records the template vmid
    and hash in state and the hash file; returns the template vmid."""
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
            destroy_vm_if_exists(ctx.node, ctx.engine_vmid, expect_tags=ctx.reclaim_tags)
            destroy_engine_template(ctx.node, ctx.engine_vmid,
                                    expect_tags=ctx.reclaim_tags | {"engine-template"})
        with timed(ctx.comp_dir, 2, "engine_template_build"):
            tmpl, build_info = build_engine_template(
                ctx.node, ctx.comp_dir, ctx.engine_vmid,
                int(os.environ["TF_VAR_template_vm_id"]),
                engine_build_ctx(ctx), ctx.postgres_password, ctx.redis_password, quotient_ref,
                engine_hash, engine_inputs, run_id=ctx.run_id)
        ctx.state["engine_build_info"] = build_info
    elif tmpl:
        # Kept (hash match or frozen): re-stamp its ownership to THIS run so the
        # next teardown's strict guard and preflight recognize it — a template kept
        # across runs carries the earlier run's tag (range_ops.retag_ownership).
        retag_ownership(ctx.node, tmpl, ctx.comp_tags | {"engine-template"})
    save_template_hashes(ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.state["engine_template_vmid"] = tmpl
    ctx.state["engine_template_hash"] = engine_hash
    ctx.save_state()
    return tmpl


def apply_engine_terraform(ctx):
    """terraform init + apply #1: only the engine (a linked clone of the engine template —
    seconds, no bulk disk copy) and the bridges are built (team_box's for_each is empty:
    build_team_boxes=false), plus team_nics' netplan for every team bridge and the
    cold-boot that surfaces the engine's team NICs."""
    tf_env, tf_cwd = terraform_env_and_cwd(ctx)
    run_terraform(["init"], cwd=tf_cwd, env=tf_env, timeout=300)
    with timed(ctx.comp_dir, 2, "terraform_apply"):
        run_terraform(["apply", "-auto-approve", "-parallelism=1"], cwd=tf_cwd, env=tf_env, timeout=2400)


def build_satellite_jumps(ctx, apply_ctx):
    """Jump/router per satellite, then the fail-loud routing gate: nothing
    downstream (satellite golden plants, nakon, scoring) works without
    engine -> jump -> satellite-bridge paths."""
    with timed(ctx.comp_dir, 2, "jump_vms", f"x{len(ctx.placement['satellites'])}"):
        build_jump_vms(ctx.placement, ctx.engine_vmid, engine_build_ctx(ctx), ctx.comp_name,
                       ctx.engine_mgmt_ip,
                       engine_mgmt_gw=os.environ.get("TF_VAR_engine_mgmt_gw",
                                                     DEFAULT_ENGINE_MGMT_GW),
                       run_id=ctx.run_id,
                       red_segment=red_segment_from_env())
    with timed(ctx.comp_dir, 2, "routing_converge"):
        verify_satellite_routing(ctx.placement, apply_ctx)


def phase2_engine_template(ctx):
    """[2/8] Engine template lifecycle, then terraform apply #1.

    M4: the engine template is built once per competition and reused across its test
    runs — rebuilt only on config drift, never when frozen. Apply #1 then builds the
    engine (a linked clone of that template, seconds) and every team bridge, with
    team_box's for_each deliberately empty; apply #2 in phase 4 flips it on once the
    goldens exist. The checkpoint and the terraform-context read stay in the caller
    (deploy), in that order: a phase-2 resume still needs the outputs, and a run that
    dies between them must leave last_phase at 2 on disk."""
    if ctx.from_phase > 2:
        print("[2/8] Skipped (resume) — not re-running terraform apply.")
        return
    tmpl = ensure_engine_template(ctx)

    print("[2/8] Terraform apply #1 (engine from template + bridges; team boxes "
          "come in apply #2)...")
    ctx.tfvars["engine_clone_id"] = tmpl
    write_tfvars(ctx)
    apply_engine_terraform(ctx)

    apply_ctx = read_terraform_ctx(ctx.comp_dir)
    # Fresh engine VM => new host key; drop any stale pin so accept-new re-pins it.
    forget_engine_host_key(apply_ctx["scoring_engine_ip"])
    with timed(ctx.comp_dir, 2, "wait_engine_ssh", apply_ctx["scoring_engine_ip"]):
        wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                     apply_ctx["scoring_engine_ip"], timeout=300)
    if ctx.placement and ctx.placement["satellites"]:
        build_satellite_jumps(ctx, apply_ctx)
    # Record where this state's resources live, for the stale-state guard
    # (gates.check_stale_terraform_state).
    ctx.state["deployed_endpoint"] = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")


def phase3_prepare_engine(ctx):
    """[3/8] Apply this deploy's per-competition state to the engine clone."""
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

    # Opt-in (Compfile `round_loop_guard 1`). After an engine reboot the containers come
    # back but the round loop does not, and the scoreboard freezes while everything that
    # reads it keeps working. Off by default: it is an unattended actor on a live engine,
    # and the account it logs in as only exists from this deploy onward.
    if compfile_flag(ctx.comp_dir / "Compfile", "round_loop_guard", 0) and ctx.scoring_password:
        with timed(ctx.comp_dir, 3, "round_loop_guard"):
            install_round_loop_guard(ctx.tf_ctx, ctx.comp_dir, ctx.scoring_password)

    ensure_nat_forwarding(ctx.tf_ctx)

    # Remote access (headscale): every deploy enrolls the engine as a subnet router,
    # pushes the tailnet->team SNAT unit, and re-applies this competition's scoped
    # ACL block + participant keys. `remote_access 0` in the Compfile opts out; a
    # failure here fails the deploy — an unreachable headscale must not quietly
    # yield a range nobody can reach.
    if compfile_flag(ctx.comp_dir / "Compfile", "remote_access", 1):
        with timed(ctx.comp_dir, 3, "remote_access"):
            import remote_access_ops
            remote_access_ops.setup_remote_access(ctx)
    else:
        print("  Remote access: skipped (Compfile remote_access 0).")

