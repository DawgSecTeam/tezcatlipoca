"""The configure half shared by every redeploy mode: scoped nakon plant, hardening, domain
re-run, and the one post-`tz-base` convergence sequence.

Every mode that resets a box's disk (rollback-base, rebuild, reset's later rungs) must run the
SAME sequence the deploy runs, in the same order — planting the disruptive final stage before
the AD joins is the brick hazard the stage split exists to prevent, and taking `tz-ready`
before the account-file pins bakes a half-planted box in as "as delivered".
"""

import json
from pathlib import Path

import pipeline_api

from common.constants import SNAP_BASE, SNAP_READY
from common.utils import compfile_flag
from estate import config
from estate import state as estate_state
from guests import clean_trails
from guests.converge_ops import record_stage, run_stage, stage_machines
from redeploy.redeploy_select_ops import box_platform


def _nakon_jobs(comp_dir):
    return max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))


def _stage_pass(name, path, targets, rctx, *, strict=False, bundle=None):
    """One scoped nakon stage over `targets`; no-op when the stage file has nothing for them."""
    if not path.exists():
        print(f"  ({name} stage file missing — skipping)")
        return None
    machines = stage_machines(path)
    wanted = {t["machine"] for t in targets}
    selected = [m for m in machines if m["name"] in wanted]
    if not selected:
        print(f"  (no {name}-stage configs for the selected boxes — skipping)")
        return None
    print(f"  {name.capitalize()}-stage pass on {len(selected)} machine(s)...")
    result = run_stage(path, selected, ssh_key=Path(rctx.ctx["ssh_key_path"]),
                       scoring_user=config.active().engine.username,
                       scoring_ip=rctx.ctx["scoring_engine_ip"], jobs=_nakon_jobs(rctx.comp_dir),
                       strict=strict, label=name, comp_dir=rctx.comp_dir, bundle=bundle)
    record_stage(rctx.state, selected, result, lambda: estate_state.write_flat(
        rctx.comp_dir, rctx.state), name)
    return result


def linux_base_ready(targets, rctx):
    """The Linux trio every plant path needs, in the order that avoids retry ladders.

    Order matters: the NOPASSWD sudoers grant must land BEFORE the DNS fix, whose sudo calls
    soft-fail until it does — re-inverted, fix_dns burns its full 8x15s retry ladder first.
    """
    linux_targets = [t for t in targets if box_platform(t["box"]) == "linux"]
    pipeline_api.setup_ubuntu_auth(linux_targets, rctx.ctx)
    pipeline_api.fix_dns_on_boxes(linux_targets, rctx.ctx)
    pipeline_api.prep_apt_on_boxes(linux_targets, rctx.ctx,
                                   use_proxy=_apt_cache(rctx.comp_dir))
    pipeline_api.ensure_nat_forwarding(rctx.ctx)
    return linux_targets


def run_nakon_and_harden(targets, rctx, nakon_config_path, nakon_bundle):
    """Auth/DNS/apt + the post-clone plant + hardening + the late pass (the light modes)."""
    comp_dir, state = rctx.comp_dir, rctx.state
    linux_targets = linux_base_ready(targets, rctx)

    wanted = {t["machine"] for t in targets}
    if nakon_bundle is None or nakon_config_path is None:
        print("  (no nakon replant stage for the selected box(es) — skipping the plant pass)")
    else:
        # `nakon --only` on an absent box errors "matched no machine", which the retry loop
        # misreads as dead SSH — so filter to the staged machines ourselves.
        selected = [m for m in stage_machines(nakon_config_path) if m["name"] in wanted]
        if not selected:
            print("  (no nakon replant stage for the selected box(es) — skipping the plant pass)")
        else:
            print(f"  Running Nakon on {len(selected)} machine(s): "
                  f"{', '.join(m['name'] for m in selected)}")
            # strict=False mirrors phase 6: one flaky pin must not abort a pre-domain pass.
            result = run_stage(nakon_config_path, selected,
                               ssh_key=Path(rctx.ctx["ssh_key_path"]),
                               scoring_user=config.active().engine.username,
                               scoring_ip=rctx.ctx["scoring_engine_ip"],
                               jobs=_nakon_jobs(comp_dir), strict=False, label="redeploy",
                               comp_dir=comp_dir, bundle=nakon_bundle)
            record_stage(state, selected, result,
                         lambda: estate_state.write_flat(comp_dir, state), "redeploy")

    pipeline_api.fix_services_on_boxes(comp_dir, linux_targets, rctx.ctx,
                                       box_creds=state.get("box_creds"))
    run_late_pass(targets, rctx)


def _box_username(rctx):
    from common.utils import load_users_config
    return load_users_config(rctx.comp_dir)[0]


def _apt_cache(comp_dir):
    return bool(compfile_flag(comp_dir / "Compfile", "apt_cache", 1))


def post_base_converge(targets, rctx, *, take_base=False):
    """The full post-`tz-base` sequence a disk-resetting mode must run.

    predomain -> fix_services -> domains -> final -> late -> trail wipe -> tz-ready.
    Returns False when the domain chain could not run (the caller must NOT take `tz-ready`).
    """
    comp_dir, node = rctx.comp_dir, rctx.node
    linux_targets = linux_base_ready(targets, rctx)

    if take_base:
        print(f"  Snapshotting rebuilt boxes as '{SNAP_BASE}'...")
        for t in targets:
            pipeline_api.take_snapshot(t.get("node") or node, t["vmid"], SNAP_BASE,
                                       description="tezcatlipoca: reset to base, pre-Nakon")

    _stage_pass("predomain", comp_dir / ".nakon-predomain.json", targets, rctx)
    pipeline_api.fix_services_on_boxes(comp_dir, linux_targets, rctx.ctx,
                                       box_creds=rctx.state.get("box_creds"))
    try:
        domain_settled = rerun_domain_configs(targets, rctx, comp_dir / ".nakon-postclone.json")
    except Exception:
        print("  tz-ready NOT re-taken — the domain chain failed above, so the boxes are "
              "not in an 'as delivered' state.")
        raise
    _stage_pass("final", comp_dir / ".nakon-final.json", targets, rctx)
    # After every account writer above, so the packet's promised modes reach the box.
    run_late_pass(targets, rctx)

    try:
        clean_trails.clean_trails(
            comp_dir,
            clean_trails.creds_for(rctx.ctx["ssh_key_path"],
                                   rctx.ctx["scoring_engine_ip"],
                                   _box_username(rctx), rctx.state.get("box_password")),
            targets=clean_trails.targets_from(targets))
    except Exception as e:                                   # noqa: BLE001 - never fatal
        print(f"  WARNING: trail wipe failed ({type(e).__name__}: {e}) — box logs still "
              f"hold the deploy")

    if not domain_settled:
        print("  tz-ready NOT re-taken — domain configuration could not run (see above); "
              "snapshotting now would bake a broken state in as 'delivered'.")
        return False
    print(f"  Snapshotting boxes as '{SNAP_READY}'...")
    for t in targets:
        pipeline_api.take_snapshot(t.get("node") or node, t["vmid"], SNAP_READY,
                                   description="tezcatlipoca: as delivered (re-taken by redeploy)")
    return True


def run_late_pass(targets, rctx):
# fix_services_on_boxes' credlist useradd recreates /etc/passwd, /etc/shadow and /etc/group at
#     default modes, so this last pass re-applies the mode pins after every account writer.
    comp_dir = rctx.comp_dir
    path = comp_dir / ".nakon-late.json"
    if not path.exists():
        print("  (.nakon-late.json is missing — no account-file mode pins to replay; "
              "the range predates the last pass)")
        return
    wanted = {t["machine"] for t in targets}
    selected = [m for m in stage_machines(path) if m["name"] in wanted]
    if not selected:
        return
    print(f"  Account-file mode pass on {len(selected)} machine(s) (after hardening)...")
    result = run_stage(path, selected, ssh_key=Path(rctx.ctx["ssh_key_path"]),
                       scoring_user=config.active().engine.username,
                       scoring_ip=rctx.ctx["scoring_engine_ip"],
                       jobs=_nakon_jobs(comp_dir), strict=False, label="late",
                       comp_dir=comp_dir)
    record_stage(rctx.state, selected, result,
                 lambda: estate_state.write_flat(comp_dir, rctx.state), "late")


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
        if not stage_machines(postclone):
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
