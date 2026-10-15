"""Engine recovery from the engine template and phase-7 reseed."""

import sys
import pipeline_api

from common.constants import DEFAULT_ENGINE_MGMT_GW
from engine.engine_event_ops import push_event_conf
from engine.engine_health_ops import ensure_nat_forwarding
from engine.engine_identity_ops import prepare_engine_from_template
from estate import state as estate_state
from nakonops.nakon_lock_ops import acquire_engine_lock
from engine.template_freeze import stored_template_hash
from common.timing import timed
from common.ssh_ops import forget_engine_host_key, wait_for_ssh
from estate import config


def _identifiers(comp_dir, teams, boxes):
# Slot 0's identifiers, and whether an in-path firewall needs the transit NICs.
    placement = estate_state.open(comp_dir).data.get("placement")
    slots = (placement or {}).get("team_slots") or {}
    teams_on_engine = [teams[k]["identifier"] for k in sorted(teams) if slots.get(k, 0) == 0]
    return placement, teams_on_engine, any(b.get("in_path") for b in boxes)


def _replace_engine(comp_dir, state, teams, boxes):
# Destroy ONLY the engine and re-clone it from the engine template.
#
#     Scoped by construction: the destroy names the engine's vmid and the create builds one
#     VM, so a team box rebuilt out-of-band cannot be wiped mid-event — which is what the
#     old `-target` scoping existed for (winad-testrun 2026-09-25).
    from estate import apply as estate_apply, config as estate_config, engine_setup
    from infra.ownership import Ownership, destroy_vm_if_exists
    _, identifiers, in_path = _identifiers(comp_dir, teams, boxes)
    cfg = estate_config.active()
    node = cfg.proxmox.node
    vmid = int(state["scoring_vmid"])
    ownership = Ownership(comp_dir.name, state["run_id"])
    destroy_vm_if_exists(node, vmid, expect_tags=ownership.tags)
    estate_apply.create_engine(
        estate_state.open(comp_dir), ownership, node, vmid=vmid,
        template_vmid=int(state["engine_template_vmid"]),
        team_identifiers=identifiers, transit_identifiers=identifiers if in_path else [],
        mgmt_ip=config.active().engine.mgmt_ip,
        mgmt_gw=(config.active().engine.mgmt_gw or DEFAULT_ENGINE_MGMT_GW),
        datastore=cfg.proxmox.datastore, username=cfg.engine.username,
        ssh_public_key=cfg.ssh.public_key)
    engine_setup.cold_boot(node, vmid)


def _address_engine(comp_dir, state, teams, boxes, ctx):
# Re-address the fresh engine's team bridges (and re-assert the satellite routes).
    from estate import engine_setup
    from multinode.placement_record import satellite_routes_for
    placement, identifiers, in_path = _identifiers(comp_dir, teams, boxes)
    engine_setup.configure_engine_network(
        ctx, identifiers, identifiers if in_path else [],
        satellite_routes_for(placement) if placement else [])


def engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=False):
# M4 engine recovery: re-clone the engine VM from the competition's engine
#     template and apply per-deploy state fresh. Team boxes, golden templates, and the
#     engine template are untouched. The scoring DB starts EMPTY (fresh volumes) —
#     re-seed with create-competition.py --from-phase 7 afterwards.
    if not state.get("engine_template_vmid"):
        raise SystemExit(
            "  ERROR: .deploy_state.json has no engine_template_vmid — this competition "
            "predates the M4 engine template; redeploy the range instead.")
    engine_vmid = int(state.get("scoring_vm_id") or 1000)
    acquire_engine_lock(engine_vmid)
    node = config.active().proxmox.node

    # Frozen semantics: recovery uses the node's template as-is; a hash mismatch is a
    # loud warning, never a mid-event blocker.
    expected = state.get("engine_template_hash")
    on_node = stored_template_hash(node, int(state["engine_template_vmid"]))
    if expected and on_node and on_node != expected:
        print(f"  WARNING: engine template on the node ({on_node[:12]}) differs from the "
              f"verified hash ({expected[:12]}) — recovering from the node's template "
              f"anyway. If this competition is frozen, investigate after the event.")

    if not assume_yes and not args_yes_engine_recovery():
        return False

    for p in ("postgres_password", "redis_password", "admin_password"):
        if not state.get(p):
            raise SystemExit(f"  ERROR: state has no {p} — cannot re-apply per-deploy "
                             f"engine state. Redeploy the range instead.")

    with timed(comp_dir, "recovery", "engine_recovery_total"):
        with timed(comp_dir, "recovery", "replace_engine"):
            _replace_engine(comp_dir, state, teams, boxes)
        ctx = pipeline_api.engine_ctx(comp_dir)
        forget_engine_host_key(ctx["scoring_engine_ip"])
        wait_for_ssh(ctx["ssh_key_path"], ctx["vm_username"],
                     ctx["scoring_engine_ip"], timeout=300)
        with timed(comp_dir, "recovery", "engine_network"):
            _address_engine(comp_dir, state, teams, boxes, ctx)
        with timed(comp_dir, "recovery", "engine_from_template"):
            prepare_engine_from_template(ctx, state["postgres_password"],
                                         state["redis_password"])
        push_event_conf(comp_dir, teams, boxes, ctx, name,
                        inject_password=state.get("inject_password"),
                        admin_password=state["admin_password"],
                        scoring_password=state.get("scoring_password"),
                        postgres_password=state["postgres_password"],
                        redis_password=state["redis_password"],
                        box_creds=state.get("box_creds") or {},
                        extra_credlists=({"domain": state["domain_creds"]}
                                         if state.get("domain_creds") else None))
        ensure_nat_forwarding(ctx)

    # The scoring DB is empty now, so phase 7's per-step done-flags are stale: without
    # clearing them the advertised --from-phase 7 re-seed skipped every step.
    # portal_up goes too: the new engine has no portal until that re-seed's phase 8 re-ships
    # it (portal_ops.deploy_portal reuses the recorded console tokens).
    for flag in ("seeded", "engine_unpaused", "injects_created", "injects_fingerprint",
                 "portal_up"):
        state.pop(flag, None)
    # This write lands LAST in engine-recovery, after the engine was re-cloned and the
    # scoring DB wiped. A torn write here leaves a live engine with unreadable resume state
    # and no second chance (winad-testrun 2026-09-25) — always through the document's
    # atomic writer.
    estate_state.write_flat(comp_dir, state)

    print(f"\n  Engine recovered from template (vmid {state['engine_template_vmid']}).")
    print("  The scoring DB is EMPTY — re-seed with:")
    print(f"    python3 create-competition.py --competition {comp_dir.name} "
          f"--from-phase 7 --yes")
    return True


def reseed_event(comp_dir):
# Phase 7 against the freshly recovered engine: seed, unpause, and create injects with
#     offsets anchored at NOW. This is how a reset-and-rerun gets a live inject window —
#     offsets resolve at phase-7 time, so a rollback-ready alone left every inject closed
#     (winad-scrim2 2026-09-26: all 12 closed ~1.5h before T0).
    import subprocess
    subprocess.run([sys.executable, "create-competition.py", "--competition", comp_dir.name,
                    "--from-phase", "7", "--yes"], check=True)


def args_yes_engine_recovery():
    answer = input("  Recover the scoring engine from its template? The scoring DB "
                   "(teams/scores/injects) is LOST and must be re-seeded. [y/N] ").strip()
    return answer.lower() in ("y", "yes")
