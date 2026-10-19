"""The configure half shared by every redeploy mode: scoped nakon plant, hardening, domain re-run."""

import json
import pipeline_api

from estate import state as estate_state

from common.utils import compfile_flag
from pathlib import Path
from redeploy.redeploy_select_ops import box_platform
from estate import config


def run_nakon_and_harden(targets, rctx, nakon_config_path, nakon_bundle):
    ctx, comp_dir, state = rctx.ctx, rctx.comp_dir, rctx.state
    key = Path(ctx["ssh_key_path"])
    scoring_user = config.active().engine.username
    scoring_ip = ctx["scoring_engine_ip"]

    linux_targets = [t for t in targets if box_platform(t["box"]) == "linux"]
    # Order matters: the NOPASSWD sudoers grant must land BEFORE the DNS fix, whose sudo calls
    # soft-fail until it does — re-inverted, fix_dns burns its full 8x15s retry ladder first.
    pipeline_api.setup_ubuntu_auth(linux_targets, ctx)
    pipeline_api.fix_dns_on_boxes(linux_targets, ctx)

    pipeline_api.ensure_nat_forwarding(ctx)

    machines = [t["machine"] for t in targets]
    if nakon_bundle is not None and nakon_config_path is not None:
        # `nakon --only` on an absent box errors "matched no machine", which the retry loop misreads as dead SSH.
        staged = {m["name"] for m in json.loads(Path(nakon_config_path).read_text())["machines"]}
        machines = [m for m in machines if m in staged]
    if nakon_bundle is None or nakon_config_path is None or not machines:
        print("  (no nakon replant stage for the selected box(es) — skipping the plant pass)")
    else:
        print(f"  Running Nakon on {len(machines)} machine(s): {', '.join(machines)}")
        # strict=False mirrors phase 6: one flaky pin must not abort a mid-event pre-domain pass.
        nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
        result = pipeline_api.run_nakon(
            key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
            only=machines,
            timeout=max(2400, pipeline_api.PER_MACHINE_NAKON_BUDGET * len(machines)),
            strict=False, jobs=nakon_jobs,
        )
        # Append, never replace: verify's plant-integrity line reads this.
        state["nakon_failed_steps"] = list(state.get("nakon_failed_steps") or []) + [
            f"redeploy: {line}" for line in result.failed[:20]]
        # The document holds the only copy of the box passwords; write_flat is the atomic writer.
        estate_state.write_flat(comp_dir, state)

    pipeline_api.fix_services_on_boxes(comp_dir, linux_targets, ctx, box_creds=state.get("box_creds"))
    run_late_pass(targets, rctx)


def run_late_pass(targets, rctx):
# fix_services_on_boxes' credlist useradd recreates /etc/passwd, /etc/shadow and /etc/group at
#     default modes, so this last pass re-applies the mode pins after every account writer.
    ctx, comp_dir, state = rctx.ctx, rctx.comp_dir, rctx.state
    path = comp_dir / ".nakon-late.json"
    if not path.exists():
        print("  (.nakon-late.json is missing — no account-file mode pins to replay; "
              "the range predates the last pass)")
        return
    staged = {m["name"] for m in json.loads(path.read_text())["machines"]}
    machines = [t["machine"] for t in targets if t["machine"] in staged]
    if not machines:
        return
    print(f"  Account-file mode pass on {len(machines)} machine(s) (after hardening)...")
    result = pipeline_api.run_nakon(
        Path(ctx["ssh_key_path"]), config.active().engine.username, ctx["scoring_engine_ip"],
        pipeline_api.build_nakon_bundle(path), path, only=machines,
        timeout=max(2400, pipeline_api.PER_MACHINE_NAKON_BUDGET * len(machines)),
        strict=False, jobs=max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4)))
    if result.failed:
        state["nakon_failed_steps"] = list(state.get("nakon_failed_steps") or []) + [
            f"redeploy late-stage: {line}" for line in result.failed[:20]]
        estate_state.write_flat(comp_dir, state)


def _domain_config_path(comp_dir, fallback):
    full_config = comp_dir / "nakon-config.json"
    return full_config if full_config.exists() else fallback


def rerun_domain_configs(targets, rctx, nakon_config_path):
# False means the chain could not re-run: callers must NOT re-take tz-ready, or they bake in a broken state.
    ctx, comp_dir, state = rctx.ctx, rctx.comp_dir, rctx.state
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        return True
    roles = json.loads(roles_path.read_text())
    domain_targets = [t for t in targets if roles.get(t["box_name"])]
    if not domain_targets:
        return True

    box_password = state.get("box_password")
    if not box_password:
        print("  WARNING: the state document has no box_password — cannot re-run domain "
              "configuration for the reset domain-role box(es). Rejoin by hand, or re-run "
              "create-competition.py --from-phase 6.")
        return False

    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    affected_teams = {t["team_key"] for t in domain_targets}

    print("  Domain-role box(es) were reset to a pre-domain state — re-running domain "
          "configuration for " + ", ".join(sorted(affected_teams)) + "...")
    # The stage file may hold only the selected box types; the domain chain needs every team member.
    domain_config_path = _domain_config_path(comp_dir, nakon_config_path)
    for team_key in sorted(affected_teams):
        dc_reset = any(
            roles.get(t["box_name"]) == "dc" and t["team_key"] == team_key
            for t in domain_targets
        )
        if dc_reset:
            # A reset DC wipes all AD content and every chain step keys on its own done-marker,
            # so all of this team's markers are void; deleting only some re-skips account creation.
            for marker in sorted(comp_dir.glob(f".nakon-domain-{team_key}-*.json")):
                print(f"  Deleting stale domain marker {marker.name} — the DC was reset, "
                      f"so the whole domain chain (promotion, AD content, member joins) "
                      f"must re-run.")
                marker.unlink()
        pipeline_api.deploy_domain_configs(
            {team_key: teams[team_key]}, boxes, comp_dir, domain_config_path,
            Path(ctx["ssh_key_path"]), config.active().engine.username,
            ctx["scoring_engine_ip"], box_password, promote_dc=dc_reset,
            force_member_join=dc_reset,
        )
    return True


def prepare_nakon_assets(comp_dir, boxes):
# Pre-domain re-plants run the post-clone stage only; golden-stage installs ride the linked clone.
    postclone = comp_dir / ".nakon-postclone.json"
    if postclone.exists():
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
