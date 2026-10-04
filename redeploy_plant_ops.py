"""The configure half shared by every redeploy mode: scoped nakon plant, hardening, domain re-run."""

import json
import os
import pipeline_api

from config_ops import write_state
from utils import compfile_flag
from pathlib import Path
from redeploy_select_ops import box_platform


def run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """Configure half: DNS, auth, scoped nakon (--only), service hardening."""
    key = Path(ctx["ssh_key_path"])
    scoring_user = os.environ["TF_VAR_vm_username"]
    scoring_ip = ctx["scoring_engine_ip"]

    linux_targets = [t for t in targets if box_platform(t["box"]) == "linux"]
    # Order matters, and it is a documented invariant (docs/architecture.md: "On fresh
    # clones the NOPASSWD sudoers grant lands BEFORE the DNS fix, whose sudo calls
    # soft-fail until the grant does"). Same order as deploy.py phase 4. Re-inverting
    # these two is a silent ~2-minute-per-box regression: without the grant, DNS_FIX_CMD's
    # sudo is rejected, so fix_dns_on_boxes burns its full 8x15s retry ladder before
    # falling back to the root guest agent (hardening_ops.py), which is the only reason
    # the wrong order "works" at all.
    pipeline_api.setup_ubuntu_auth(linux_targets, ctx)
    pipeline_api.fix_dns_on_boxes(linux_targets, ctx)

    pipeline_api.ensure_nat_forwarding(ctx)

    machines = [t["machine"] for t in targets]
    if nakon_bundle is None or nakon_config_path is None:
        # prepare_nakon_assets returned no stage: every plant for this comp rides the
        # golden clone, so there is nothing for nakon to re-apply. The python-side
        # steps below (auth grant, DNS, service hardening) still re-run.
        print("  (no nakon replant stage for this competition — skipping the plant pass)")
    else:
        print(f"  Running Nakon on {len(machines)} machine(s): {', '.join(machines)}")
        # strict=False, mirroring deploy.py's phase-6 stance: these re-plants hit live
        # boxes mid-event, and one flaky/broken pin must not abort a repair sweep.
        nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
        result = pipeline_api.run_nakon(
            key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
            only=machines,
            timeout=max(2400, pipeline_api.PER_MACHINE_NAKON_BUDGET * len(machines)),
            strict=False, jobs=nakon_jobs,
        )
        # Append, never replace: the deploy-time entries belong to verify's plant-integrity
        # line, and a scoped mid-event replant must not retroactively declare the deploy's
        # failed steps resolved — or erase the record that they ever existed.
        state["nakon_failed_steps"] = list(state.get("nakon_failed_steps") or []) + [
            f"redeploy: {line}" for line in result.failed[:20]]
        state_path = comp_dir / ".deploy_state.json"
        if state_path.exists():
            # Shared atomic writer (config_ops.write_state): 0600 at creation, temp+os.replace.
            # Never hand-roll this — .deploy_state.json carries the only copy of the box
            # passwords, so a torn write bricks resume AND redeploy at once (deploy.py's
            # atomic-rename note; winad-testrun 2026-09-25).
            write_state(state_path, state)

    pipeline_api.fix_services_on_boxes(comp_dir, linux_targets, ctx, box_creds=state.get("box_creds"))


def _domain_config_path(comp_dir, fallback):
    full_config = comp_dir / "nakon-config.json"
    return full_config if full_config.exists() else fallback


def rerun_domain_configs(targets, ctx, comp_dir, state, nakon_config_path):
    """Re-run AD domain chain for affected teams (promote DC only if reset).

    Returns True when the boxes' domain state may be treated as settled — no
    domain semantics at all, or the chain re-ran clean — and False when the
    chain was supposed to run but could not. Callers must NOT re-take tz-ready
    on False: snapshotting then would bake a broken state in as 'as delivered'."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        return True
    roles = json.loads(roles_path.read_text())
    domain_targets = [t for t in targets if roles.get(t["box_name"])]
    if not domain_targets:
        return True

    box_password = state.get("box_password")
    if not box_password:
        print("  WARNING: .deploy_state.json has no box_password — cannot re-run domain "
              "configuration for the reset domain-role box(es). Rejoin by hand, or re-run "
              "create-competition.py --from-phase 6.")
        return False

    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    affected_teams = {t["team_key"] for t in domain_targets}

    print("  Domain-role box(es) were reset to a pre-domain state — re-running domain "
          "configuration for " + ", ".join(sorted(affected_teams)) + "...")
    # The post-clone stage file may contain only the selected/rebuilt box types. Domain
    # orchestration still needs every member machine in the affected team so it can
    # rejoin them after a DC reset; use the full source-of-truth machine list here.
    domain_config_path = _domain_config_path(comp_dir, nakon_config_path)
    for team_key in sorted(affected_teams):
        dc_reset = any(
            roles.get(t["box_name"]) == "dc" and t["team_key"] == team_key
            for t in domain_targets
        )
        if dc_reset:
            # A reset DC wipes the ENTIRE AD content: promotion state, the svc-support
            # and packet accounts, the AD misconfigs, and every member's machine
            # account. Each chain step keys on its own done-marker, so ALL of this
            # team's domain markers are void (live-found 2026-10-03: deleting only the
            # ADDS marker re-promoted the DC but skipped account re-creation —
            # svc-support went missing and verify's domain gate failed).
            for marker in sorted(comp_dir.glob(f".nakon-domain-{team_key}-*.json")):
                print(f"  Deleting stale domain marker {marker.name} — the DC was reset, "
                      f"so the whole domain chain (promotion, AD content, member joins) "
                      f"must re-run.")
                marker.unlink()
        pipeline_api.deploy_domain_configs(
            {team_key: teams[team_key]}, boxes, comp_dir, domain_config_path,
            Path(ctx["ssh_key_path"]), os.environ["TF_VAR_vm_username"],
            ctx["scoring_engine_ip"], box_password, promote_dc=dc_reset,
            force_member_join=dc_reset,
        )
    return True


def prepare_nakon_assets(comp_dir, boxes):
    """(nakon_config_path, nakon_bundle) for the replant modes.

    A function rather than main() inline so the reset ladder can build them LAZILY —
    a reset that settles at the tz-ready rung must not demand stage files it would
    never have used."""
    # Repair re-plants run the POST-CLONE stage only — the golden-stage installs ride the
    # linked clone and re-running them over live boxes mid-event is exactly what the stage
    # split removed.
    postclone = comp_dir / ".nakon-postclone.json"
    if postclone.exists():
        # A comp whose plants are ALL golden-stage (e.g. every cde-2026-style pin)
        # has an empty postclone stage — live-found 2026-10-03: build_nakon_bundle
        # refuses an empty machine list, so a legitimate no-replant must not try.
        # Auth/DNS/service hardening below still re-runs; the probe judges health.
        if not json.loads(postclone.read_text()).get("machines"):
            print("  (postclone stage is empty — every plant rides the golden clone; "
                  "nakon has nothing to re-plant. Auth/DNS/hardening still re-runs.)")
            return None, None
    else:
        print("  WARNING: .nakon-postclone.json is missing — regenerating the stage split "
              "from nakon-config.json")
        if not (comp_dir / "nakon-config.json").exists():
            raise SystemExit(
                "  ERROR: neither .nakon-postclone.json nor nakon-config.json exists — "
                "cannot build the post-clone stage config for this mode.")
        pipeline_api.generate_stage_configs(
            comp_dir, json.loads((comp_dir / "teams.json").read_text()), boxes,
            unbooted=pipeline_api.unbooted_golden_boxes(comp_dir))
    return postclone, pipeline_api.build_nakon_bundle(postclone)
