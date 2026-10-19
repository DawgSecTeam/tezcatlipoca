"""Every refusal the deploy makes before it builds or destroys anything."""

import fcntl
import json
import subprocess
import sys

from common.constants import NAKON_DIR, PIPELINE_VERSION, SCORING_ENGINE_VMID
from infra.collisions import (check_collisions, check_mgmt_bridge, check_placement,  # noqa: F401
                              engine_mgmt_ip_gate, jump_mgmt_ip_gate, share_mgmt_bridge)
from infra.resources import check_datastore_headroom, check_share_templates
from common.utils import compfile_flag

_DEPLOY_LOCKS = {}  # path -> open fh (keep referenced so flock survives)


def acquire_deploy_lock(comp_dir):
    """One driver per competition: concurrent deploys share the engine's /opt/nakon staging
    dir and each one's `rm -rf /opt/nakon/*` wipes the other's plan archives mid-plant."""
    lock_fh = open(comp_dir / ".deploy.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"  ERROR: another deploy already holds the lock on {comp_dir} — refusing to run "
            "concurrently. Find it with `pgrep -af create-competition` and kill that PID "
            "(never `pkill -f`: the pattern matches every concurrent session's deploy on "
            "the host), then confirm it is gone before retrying."
        )
    _DEPLOY_LOCKS[str(comp_dir)] = lock_fh


def check_resume_gates(comp_dir, state_path, previous_state, from_phase, force=False):
    """Resume refusals, in order: missing state, foreign pipeline version, phases that
    never completed, a checkpoint whose machines are gone. `force` relaxes the last two."""
    if from_phase > 1 and not state_path.exists():
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} but {state_path} doesn't exist — there are no "
            f"saved team passwords/secrets to resume with, and generating fresh ones while "
            f"skipping the destructive phases would leave the deployed range and its credentials "
            f"out of sync. Re-run without --from-phase for a clean redeploy."
        )
    if from_phase <= 1:
        return
    _pv = previous_state.get("pipeline_version")
    if _pv != PIPELINE_VERSION:
        raise SystemExit(
            f"  ERROR: {state_path} was written by pipeline v{_pv}, this code is "
            f"v{PIPELINE_VERSION} — its --from-phase numbers mean different things. Run a "
            f"fresh deploy (no --from-phase), or finish the run on the code that wrote "
            f"the state.")
    guard_resume_from_phase(from_phase, previous_state.get("last_phase"), state_path,
                            force=force)
    guard_resume_existence(from_phase, comp_dir, previous_state, force=force)


def check_stale_estate_state(comp_dir, comp_name, previous_state, engine_vmid, from_phase):
    """Refuse to point a deploy at a host the competition's state did not come from: the
    ownership proof would aim at absent VMs and the real range would stand. Runs before any secret mint."""
    from estate import config as estate_config
    if from_phase > 2 or not previous_state:
        return
    cur_endpoint = estate_config.active().proxmox.endpoint.rstrip("/")
    prev_endpoint = (previous_state.get("deployed_endpoint")
                     or previous_state.get("endpoint") or "").rstrip("/")
    prev_engine = previous_state.get("scoring_vm_id")
    host_mismatch = bool(prev_endpoint) and prev_endpoint != cur_endpoint
    vmid_mismatch = prev_engine is not None and int(prev_engine) != engine_vmid
    if host_mismatch or vmid_mismatch:
        raise SystemExit(
            f"  ERROR: {comp_name}'s state was written by a deploy against "
            f"{prev_endpoint or 'another host'} (engine vmid {prev_engine}); this run "
            f"targets {cur_endpoint} (engine vmid {engine_vmid}). Its recorded VMs are not "
            f"on this host, so nothing here could own or reclaim them. Destroy this "
            f"competition from the workspace that deployed it: python3 "
            f"destroy-competition.py --competition {comp_name} --yes"
        )


def run_capacity_preflight(ctx, comp_dir, from_phase):
    """Phase-2 boundary only: a later resume must not re-refuse against an already-deployed range."""
    if from_phase <= 2:
        # "Ours" is run-aware: the prior state's run id marks this comp dir's leftovers; with no
        # prior state, comp-tagged leftovers classify foreign and the gate refuses (fail-closed).
        reclaim_tag = ctx.previous_state.get("run_id") or ctx.run_id
        from infra.plan import multinode_plan, single_node_plan
        if ctx.placement:
            run_preflight(multinode_plan(comp_dir, ctx.boxes, ctx.teams, ctx.engine_vmid,
                                         ctx.placement, ctx.engine_mgmt_ip,
                                         not ctx.resuming, reclaim_tag))
        else:
            run_preflight(single_node_plan(comp_dir, ctx.boxes, ctx.number_of_teams,
                                           ctx.teams, ctx.engine_vmid, not ctx.resuming,
                                           ctx.engine_mgmt_ip, reclaim_tag))


def run_range_gates(ctx, comp_dir, from_phase):
    """Local stale-estate check first, then the Proxmox-querying preflight; both before the
    fresh-secret mint, so a refusal never leaves a desynced credential set."""
    check_stale_estate_state(comp_dir, ctx.comp_name, ctx.previous_state,
                             ctx.engine_vmid, from_phase)
    run_capacity_preflight(ctx, comp_dir, from_phase)


def guard_resume_from_phase(from_phase, last_phase, state_path, force=False):
    """Refuses a --from-phase beyond last_phase + 1 — the skipped phases built the machines
    later ones target. A missing last_phase reads as 0; --force-from-phase is the escape hatch."""
    if force:
        return
    last = last_phase if isinstance(last_phase, int) and last_phase >= 0 else 0
    if from_phase <= last + 1:
        return
    raise SystemExit(
        f"  ERROR: --from-phase {from_phase} skips phases {last + 1}-{from_phase - 1}, but "
        f"{state_path} records phase {last} as the last one that completed. Those phases "
        f"never ran, so the later phases would target machines that do not exist yet. "
        f"Resume at --from-phase {last + 1} (re-runs the phase that died), or pass "
        f"--force-from-phase to skip ahead deliberately."
    )


def _artifact_probes(comp_dir):
    """Test seam for guard_resume_existence's probes; resolved at call time so a patch lands."""
    from infra.client import live_vmids
    from estate.state import team_vmids
    return {"vmids_from_state": lambda: team_vmids(comp_dir),
            "vm_exists": live_vmids}


def guard_resume_existence(from_phase, comp_dir, state, force=False,
                           vmids_from_state=None, vm_exists=None):
    """A checkpoint is a claim about work done; before trusting a skipped phase, verify its
    output: >2 needs the engine live, >4 every recorded team box. --force-from-phase skips this."""
    if force:
        return
    probes = _artifact_probes(comp_dir)
    probe_vmids = vmids_from_state or probes["vmids_from_state"]
    probe_live = vm_exists or probes["vm_exists"]
    if from_phase <= 2:
        return
    needs_boxes = from_phase > 4

    try:
        live = set(probe_live())
    except RuntimeError as e:
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} cannot confirm the machines exist: {e}\n"
            f"        Refusing to resume blind — re-run with --from-phase "
            f"2, or pass --force-from-phase once you have confirmed "
            f"the machines exist by hand."
        )

    engine_vmid = state.get("scoring_vm_id", SCORING_ENGINE_VMID)
    if int(engine_vmid) not in live:
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} skips the phase that creates the scoring "
            f"engine, but vmid {engine_vmid} does not exist.\n"
            f"        Every later phase targets it (scoring setup, engine context, "
            f"box images).\n"
            f"        Re-run with --from-phase 2 to recreate it."
        )

    if not needs_boxes:
        return

    try:
        vmids = probe_vmids()
    except RuntimeError as e:
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} would trust phases 2-{from_phase - 1}, but their "
            f"output cannot be verified: {e}\n"
            f"        Refusing to resume blind. Re-run with --from-phase 2 "
            f"to rebuild, or pass --force-from-phase once you have confirmed the machines "
            f"exist by hand."
        )

    missing = [v for v in vmids if v not in live]
    if not vmids or missing:
        found = ("no team boxes at all are recorded in the state document" if not vmids else
                 f"{len(missing)} of {len(vmids)} recorded team box(es) are gone: "
                 f"{', '.join(str(v) for v in missing[:8])}"
                 f"{' …' if len(missing) > 8 else ''}")
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} skips the phases that create the team boxes, "
            f"but {found}.\n"
            f"        The later phases would target machines that do not exist (this is the "
            f"scale8 soak's poisoned resume: the pre-domain pass reported success against 32 "
            f"ghosts and checkpointed it).\n"
            f"        Re-run with --from-phase 4 to recreate them."
        )


def catalog_check_paths(comp_dir):
    """Paths for `nakon catalog check`, rewritten only when the bundle uses constructs the
    catalog can't know: score-only pins name no config; baseline pins plant like vulns."""
    services_path = comp_dir / "box_services.json"
    vulns_path = comp_dir / "box_vulns.json"
    if not services_path.exists() and not vulns_path.exists():
        # A fresh competition's pins are generated after these gates; None means "no pins to check".
        return (None, None, None)
    services = json.loads(services_path.read_text()) if services_path.exists() else {}
    vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}

    def _is_score_only(c):
        return isinstance(c, dict) and c.get("score_only")

    filtered_services = {box: [p for p in pins if not _is_score_only(p)]
                         for box, pins in services.items()}
    merged_vulns = {box: list(v) + list(baseline.get(box, []))
                    for box, v in vulns.items()}
    if (filtered_services == services and merged_vulns == vulns
            and services_path.exists() and vulns_path.exists()):
        return (services_path, vulns_path, None)

    tmp_services = comp_dir / ".catalog-check-services.json"
    tmp_vulns = comp_dir / ".catalog-check-vulns.json"
    tmp_services.write_text(json.dumps(filtered_services, indent=2))
    tmp_vulns.write_text(json.dumps(merged_vulns, indent=2))

    def cleanup():
        tmp_services.unlink(missing_ok=True)
        tmp_vulns.unlink(missing_ok=True)

    return tmp_services, tmp_vulns, cleanup


def catalog_gate(comp_dir):
    svc_path, vuln_path, cleanup = catalog_check_paths(comp_dir)
    if svc_path is None:
        print("  Preflight: no pin files yet (fresh competition) — "
              "catalog check runs after pin generation")
        return
    catalog = subprocess.run(
        [sys.executable, "-m", "nakon", "catalog", "check",
         "--boxes-json", str((comp_dir / "boxes.json").resolve()),
         "--box-services", str(svc_path.resolve()),
         "--box-vulns", str(vuln_path.resolve())],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=600)
    if cleanup:
        cleanup()
    if catalog.returncode != 0:
        print(catalog.stdout)
        print(catalog.stderr)
        raise SystemExit(
            "  ERROR: nakon catalog check reported errors for this competition's pins — "
            "fix or trim box_vulns.json/box_services.json before deploying (details above).")
    print("  Preflight: nakon catalog check 0 errors")


def remote_access_gate(plan):
    """Headscale env contract present and the host reachable. Remote access ships with every
    deploy, so a dead host stops it at the gate, before any Proxmox mutation."""
    from net import remote_access_ops

    identifiers = sorted({str(t["identifier"])
                          for share in plan.shares
                          for t in (share.teams or {}).values()
                          if isinstance(t, dict) and t.get("identifier") is not None})
    if not identifiers:
        return
    remote_access_ops._headscale_env()  # SystemExit naming the missing vars
    try:
        reachable = remote_access_ops.hs_cli("health", timeout=30, check=False).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        reachable = False
    if not reachable:
        raise SystemExit(
            "  ERROR: headscale is unreachable over ssh "
            f"({remote_access_ops._headscale_env()['TEZ_HEADSCALE_SSH_USER']}@"
            f"{remote_access_ops._headscale_env()['TEZ_HEADSCALE_SSH_HOST']}). Remote "
            "access ships with every deploy, so the headscale host must be reachable "
            "before the deploy touches Proxmox. Fix TEZ_HEADSCALE_* in .env or set "
            "`remote_access 0` in the Compfile to opt out.")


def run_preflight(plan):
    """The single pre-Proxmox-mutation entry point."""
    if compfile_flag(plan.comp_dir / "Compfile", "remote_access", 1):
        remote_access_gate(plan)
    # Self-collision first: no vmid in two of this run's bands, no team block over one.
    check_placement(plan.placement)
    for share in plan.shares:
        share.raw_vms = share.fetch_vms()
        vms = ([vm for vm in share.raw_vms if vm.get("node") == share.node]
               if share.scope_to_node else share.raw_vms)
        if share.check_templates or share.engine_base:
            check_share_templates(share, plan.boxes, vms)
        if share.check_collisions:
            check_collisions(share, plan, vms)
        if share.check_headroom:
            check_datastore_headroom(
                share.node, share.datastore, plan.boxes, share.num_teams,
                label=share.label, node_name=share.name or None)
        check_mgmt_bridge(share.node, share_mgmt_bridge(plan, share))
    if plan.engine_mgmt_ip:
        engine = next(s for s in plan.shares if s.is_engine)
        engine_mgmt_ip_gate(engine.node, engine.raw_vms, plan.engine_vmid,
                            plan.engine_mgmt_ip, ours_tags=plan.our_tags)
    if plan.placement:
        jump_mgmt_ip_gate(plan)
    from infra.red01 import red01_gate
    red01_gate(plan)
    catalog_gate(plan.comp_dir)
