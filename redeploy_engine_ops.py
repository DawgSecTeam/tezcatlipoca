"""Engine recovery from the engine template and phase-7 reseed."""

import json
import os
import sys
import pipeline_api

from config_ops import write_state
from engine_ops import ensure_nat_forwarding, prepare_engine_from_template, push_event_conf
from nakon_ops import acquire_engine_lock
from template_ops import stored_template_hash
from timing import timed
from ssh_ops import forget_engine_host_key, wait_for_ssh
from utils import run_terraform


def engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=False):
    """M4 engine recovery: re-clone the engine VM from the competition's engine
    template and apply per-deploy state fresh. Team boxes, golden templates, and the
    engine template are untouched. The scoring DB starts EMPTY (fresh volumes) —
    re-seed with create-competition.py --from-phase 7 afterwards."""
    if not state.get("engine_template_vmid"):
        raise SystemExit(
            "  ERROR: .deploy_state.json has no engine_template_vmid — this competition "
            "predates the M4 engine template; redeploy the range instead.")
    engine_vmid = int(state.get("scoring_vm_id") or 1000)
    acquire_engine_lock(engine_vmid)
    node = os.environ["TF_VAR_proxmox_node"]

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
        env = {**os.environ}
        env["TF_VAR_teams"] = json.dumps(teams)
        env["TF_VAR_boxes_per_team"] = json.dumps(boxes)
        env["TF_VAR_event_name"] = name
        tf_cwd = str(comp_dir / "terraform") if (comp_dir / "terraform" / "terraform.tfstate").exists() else "terraform"
        # -target scopes the plan to the engine + its post-boot null_resources so
        # terraform CANNOT touch team_box. Without it, any team box that drifted from
        # state — which a `--mode rebuild` does by design (API re-clone, not terraform) —
        # gets destroyed/recreated during 'engine recovery', wiping defenders' boxes
        # mid-event (winad-testrun 2026-09-25). -replace forces the engine rebuild.
        with timed(comp_dir, "recovery", "terraform_replace_engine"):
            run_terraform(
                ["apply", "-auto-approve", "-parallelism=1",
                 "-replace=proxmox_virtual_environment_vm.scoring_engine",
                 "-target=proxmox_virtual_environment_vm.scoring_engine",
                 "-target=null_resource.team_nics",
                 "-target=null_resource.reboot_scoring_engine",
                 "-target=null_resource.orchestrate"],
                cwd=tf_cwd, env=env, timeout=2400)
        ctx = pipeline_api.read_terraform_ctx(comp_dir)
        forget_engine_host_key(ctx["scoring_engine_ip"])
        wait_for_ssh(ctx["ssh_key_path"], ctx["vm_username"],
                     ctx["scoring_engine_ip"], timeout=300)
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
    state_path = comp_dir / ".deploy_state.json"
    # portal_up goes too: the new engine has no portal until that re-seed's phase 8 re-ships
    # it (portal_ops.deploy_portal reuses the recorded console tokens).
    for flag in ("seeded", "engine_unpaused", "injects_created", "injects_fingerprint",
                 "portal_up"):
        state.pop(flag, None)
    # This write lands LAST in engine-recovery, after terraform already re-cloned the
    # engine and the scoring DB was wiped. A torn or short-lived-0644 write here leaves a
    # live engine with unreadable/eavesdroppable resume state and no second chance
    # (winad-testrun 2026-09-25) — always go through the shared atomic writer.
    write_state(state_path, state)

    print(f"\n  Engine recovered from template (vmid {state['engine_template_vmid']}).")
    print("  The scoring DB is EMPTY — re-seed with:")
    print(f"    python3 create-competition.py --competition {comp_dir.name} "
          f"--from-phase 7 --yes")
    return True


def reseed_event(comp_dir):
    """Phase 7 against the freshly recovered engine: seed, unpause, and create injects with
    offsets anchored at NOW. This is how a reset-and-rerun gets a live inject window —
    offsets resolve at phase-7 time, so a rollback-ready alone left every inject closed
    (winad-scrim2 2026-09-26: all 12 closed ~1.5h before T0)."""
    import subprocess
    subprocess.run([sys.executable, "create-competition.py", "--competition", comp_dir.name,
                    "--from-phase", "7", "--yes"], check=True)


def args_yes_engine_recovery():
    answer = input("  Recover the scoring engine from its template? The scoring DB "
                   "(teams/scores/injects) is LOST and must be re-seeded. [y/N] ").strip()
    return answer.lower() in ("y", "yes")
