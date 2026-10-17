"""Engine recovery from the engine template and phase-7 reseed."""

import sys
import pipeline_api

from common.constants import (DEFAULT_ENGINE_MGMT_GW, RED_SEGMENT_GW,
                              RED_SEGMENT_SUBNET, red_bridge_name)
from common.utils import competition_name, stages_red
from multinode.placement_record import resolved_mgmt_bridge
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
    placement = estate_state.EstateState.load(comp_dir).data.get("placement")
    slots = (placement or {}).get("team_slots") or {}
    teams_on_engine = [teams[k]["identifier"] for k in sorted(teams) if slots.get(k, 0) == 0]
    return placement, teams_on_engine, any(b.get("in_path") for b in boxes)


def _replace_engine(comp_dir, state, teams, boxes):
# Scoped by construction — the destroy names one vmid and the create builds one VM, so no
#     team box can be wiped.
    from estate import apply as estate_apply, config as estate_config, engine_setup
    from infra.ownership import Ownership, destroy_vm_if_exists
    placement, identifiers, in_path = _identifiers(comp_dir, teams, boxes)
    cfg = estate_config.active()
    node = cfg.proxmox.node
    vmid = int(state["scoring_vmid"])
    ownership = Ownership(competition_name(comp_dir), state["run_id"])
    destroy_vm_if_exists(node, vmid, expect_tags=ownership.tags)
    estate_apply.create_engine(
        estate_state.EstateState.load(comp_dir), ownership, node, vmid=vmid,
        template_vmid=int(state["engine_template_vmid"]),
        team_identifiers=identifiers, transit_identifiers=identifiers if in_path else [],
        mgmt_ip=config.active().engine.mgmt_ip,
        mgmt_gw=(config.active().engine.mgmt_gw or DEFAULT_ENGINE_MGMT_GW),
        mgmt_bridge=resolved_mgmt_bridge(placement),
        username=cfg.engine.username,
        ssh_public_key=cfg.ssh.public_key,
        red_bridge=red_bridge_name(state.get("run_id")) if stages_red(comp_dir) else None)
    engine_setup.cold_boot(node, vmid)


def _address_engine(comp_dir, state, teams, boxes, ctx):
    from estate import engine_setup
    from multinode.placement_record import satellite_routes_for
    placement, identifiers, in_path = _identifiers(comp_dir, teams, boxes)
    engine_setup.configure_engine_network(
        ctx, identifiers, identifiers if in_path else [],
        satellite_routes_for(placement) if placement else [],
        red_segment=({"subnet": RED_SEGMENT_SUBNET, "gw": RED_SEGMENT_GW}
                     if stages_red(comp_dir) else None))


def engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=False):
    if not state.get("engine_template_vmid"):
        raise SystemExit(
            "  ERROR: the state document has no engine_template_vmid — this competition "
            "predates the M4 engine template; redeploy the range instead.")
    engine_vmid = int(state.get("scoring_vm_id") or 1000)
    acquire_engine_lock(engine_vmid)
    node = config.active().proxmox.node

    # Frozen semantics: a hash mismatch is a loud warning, never a mid-event blocker.
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

    # The DB is empty, so phase 7's done-flags are stale — leaving them made the
    # --from-phase 7 re-seed skip every step; portal_up too, the new engine has no portal yet.
    for flag in ("seeded", "engine_unpaused", "injects_created", "injects_fingerprint",
                 "portal_up"):
        state.pop(flag, None)
    # Lands last, after the engine was re-cloned and the DB wiped — a torn write here leaves
    # a live engine with unreadable resume state and no second chance.
    estate_state.write_flat(comp_dir, state)

    print(f"\n  Engine recovered from template (vmid {state['engine_template_vmid']}).")
    print("  The scoring DB is EMPTY — re-seed with:")
    print(f"    python3 create-competition.py --competition {competition_name(comp_dir)} "
          f"--from-phase 7 --yes")
    return True


def reseed_event(comp_dir):
# Offsets resolve at phase-7 time — a rollback-ready alone left every inject already closed.
    import subprocess
    subprocess.run([sys.executable, "create-competition.py", "--competition", competition_name(comp_dir),
                    "--from-phase", "7", "--yes"], check=True)


def args_yes_engine_recovery():
    answer = input("  Recover the scoring engine from its template? The scoring DB "
                   "(teams/scores/injects) is LOST and must be re-seeded. [y/N] ").strip()
    return answer.lower() in ("y", "yes")
