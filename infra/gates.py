"""Every refusal the deploy makes BEFORE it builds or destroys anything — in one place.

Two layers, one module:

  - the driver-state refusals (formerly deploy_lib/gates.py): the deploy lock, resume gates
    (missing state / wrong pipeline version / skipped phases / resume-loop / ghost checkpoints),
    the stale-terraform-state refusal — collected here and called from exactly two spots in
    deploy_lib/prepare.py, in a fixed order:
      1. check_resume_gates   — right after the prior state is read.
      2. run_range_gates      — after placement, BEFORE any secret is minted or tfvars written:
                                the stale-terraform-state refusal, then the capacity /
                                collision / template preflight (infra.collisions / .resources).
  - the pre-Proxmox-mutation preflight (formerly the preflight/ package): run_preflight(plan)
    is the single pre-mutation entry point, in this order:
      1. per node share   - templates resolve + carry cloud-init + engine base image + jump
                            source (infra.resources); vmid/bridge collisions
                            (infra.collisions, ownership-proved); datastore headroom
      2. mgmt IPs         - engine static IP, satellite jump IPs vs running guests
                            (infra.collisions, cross-node)
      3. catalog          - `nakon catalog check` over the competition's pins

The small gates (catalog, remote-access) live here too rather than in one-file-each modules.
run_capacity_preflight builds the plan (infra.plan) and calls run_preflight.

Teardown/redeploy mutate under the same ownership proof (infra.ownership.ownership_verdict) -
see destroy_vm_if_exists - so "ours" has one definition everywhere. The destroy-side ownership
rules (tag-scoped reclaim, frozen gate) are enforced where the destroy decision is made - see
golden_plan.py - and in infra.ownership.destroy_vm_if_exists. Every gate raises SystemExit with an
operator-actionable message; none is bypassed except by the explicit --force-from-phase flag
where documented."""

import fcntl
import json
import os
import subprocess
import sys

from constants import NAKON_DIR, PIPELINE_VERSION, SCORING_ENGINE_VMID
from infra.collisions import (check_collisions, engine_mgmt_ip_gate,  # noqa: F401
                              jump_mgmt_ip_gate)
from infra.resources import check_datastore_headroom, check_share_templates
from utils import compfile_flag

_DEPLOY_LOCKS = {}  # path -> open fh (keep referenced so flock survives)


def acquire_deploy_lock(comp_dir):
    """One driver per competition. Two concurrent deploys share the engine's
    /opt/nakon staging dir and each one's 'rm -rf /opt/nakon/*' wipes the
    other's plan archives mid-plant (scrim-extreme-2026-09-20: a pkill'd
    wrapper left run-2's python alive while run-3 started; both died with
    'bundle is missing its plan archive'). The flock is held for the process's life."""
    lock_fh = open(comp_dir / ".deploy.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"  ERROR: another deploy already holds the lock on {comp_dir} — refusing to run "
            "concurrently. Find it with pgrep -f create-competition; kill the PYTHON driver "
            "itself (pkill -f 'python.*create-competition'), not just a wrapper, and confirm "
            "with pgrep before retrying."
        )
    _DEPLOY_LOCKS[str(comp_dir)] = lock_fh


def check_resume_gates(comp_dir, state_path, previous_state, from_phase, force=False):
    """The resume refusals, in order. A fresh deploy (from_phase 1) passes untouched.

    Refuses a resume with no state file (there are no saved secrets to resume with),
    refuses state written by a different pipeline version (the phase numbers are part of
    what the version means), then runs the checkpoint guards: guard_resume_from_phase (do
    not skip phases the state never saw complete) and guard_resume_existence (the
    checkpoint's machines are really there). `force` is --force-from-phase, which relaxes
    only those two."""
    if from_phase > 1 and not state_path.exists():
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} but {state_path} doesn't exist — there are no "
            f"saved team passwords/secrets to resume with, and generating fresh ones while "
            f"skipping the destructive phases would leave the deployed range and its credentials "
            f"out of sync. Re-run without --from-phase for a clean redeploy."
        )
    if from_phase <= 1:
        return
    # A state file from another pipeline version numbers its phases differently, so
    # resuming across versions is refused rather than silently misinterpreted.
    _pv = previous_state.get("pipeline_version")
    if _pv != PIPELINE_VERSION:
        raise SystemExit(
            f"  ERROR: {state_path} was written by pipeline v{_pv}, this code is "
            f"v{PIPELINE_VERSION} — its --from-phase numbers mean different things. Run a "
            f"fresh deploy (no --from-phase), or finish the run on the code that wrote "
            f"the state.")
    # Refuse to skip a phase that never completed: the skipped phases are what build
    # the machines the later ones target (see guard_resume_from_phase).
    guard_resume_from_phase(from_phase, previous_state.get("last_phase"), state_path,
                            force=force)
    # ...and refuse to trust a checkpoint whose work is not actually there. The two
    # guards above read the checkpoint as a fact; this one checks the fact
    # (see guard_resume_existence for the 2026-10-02 soak that proved why).
    guard_resume_existence(from_phase, comp_dir, previous_state, force=force)


def check_stale_terraform_state(comp_dir, comp_name, previous_state, engine_vmid, from_phase):
    """Refuse to point terraform at a different host / engine vmid than its saved state.

    The per-competition terraform workdir carries state from wherever the LAST deploy ran.
    Pointing terraform at a different host (or engine vmid) makes it "reconcile" that state
    against the new endpoint and destroy whatever now sits at the old vmid there
    (2026-09-24: a realm run deleted that host's existing 1090 engine because a dead primary
    attempt left 1090 in this comp's state). Refuse unless the recorded host and vmid agree.
    Runs before the tfvars are written and before any secret is minted, so a refusal
    leaves no half-written state behind."""
    from terraform_workdir import terraform_dir
    tf_dir = terraform_dir(comp_dir)
    if from_phase <= 2 and (tf_dir / "terraform.tfstate").exists() and previous_state:
        cur_endpoint = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
        prev_endpoint = (previous_state.get("deployed_endpoint") or "").rstrip("/")
        prev_engine = previous_state.get("scoring_vm_id")
        host_mismatch = bool(prev_endpoint) and prev_endpoint != cur_endpoint
        vmid_mismatch = prev_engine is not None and int(prev_engine) != engine_vmid
        if host_mismatch or vmid_mismatch:
            raise SystemExit(
                f"  ERROR: {tf_dir}/terraform.tfstate holds state from a deploy against "
                f"{prev_endpoint or 'another host'} (engine vmid {prev_engine}); this run "
                f"targets {cur_endpoint} (engine vmid {engine_vmid}). Terraform would "
                f"reconcile the stale state and destroy whatever sits at those resources "
                f"on the new host. Destroy this competition first: python3 "
                f"destroy-competition.py --competition {comp_name} --yes"
            )


def run_capacity_preflight(ctx, comp_dir, from_phase):
    """Run the competition preflight gates, at the phase-2 boundary.

    Gated on from_phase <= 2 because the gates check free capacity and template
    reachability for the work phases 1-2 do; a later resume must not re-refuse against
    an already-deployed range, which is also why check_free is off on a resume."""
    if from_phase <= 2:
        # The clash gates' "ours" exemption must be run-aware: the prior state's run
        # id is what marks THIS competition directory's leftovers ours. With no prior
        # state it falls back to the minted id — pre-existing comp-tagged VMs then
        # classify as foreign and the gate refuses (fail-closed beats guessing whose
        # VM it is).
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
    """Every refusal that needs the resolved placement, before any credential is minted.

    Order: the cheap local stale-terraform-state check first, then the capacity /
    collision / template preflight (which queries Proxmox). Both must run before the fresh
    secret mint — see prepare()'s order note on credential-desync corpses."""
    check_stale_terraform_state(comp_dir, ctx.comp_name, ctx.previous_state,
                                ctx.engine_vmid, from_phase)
    run_capacity_preflight(ctx, comp_dir, from_phase)


def guard_resume_from_phase(from_phase, last_phase, state_path, force=False):
    """Refuse a --from-phase that skips phases .deploy_state.json never saw complete.

    checkpoint() writes `last_phase` after each phase, so the only safe resume target is
    last_phase + 1 (re-run the phase that died) or earlier. `last_phase` was written but
    never read, so `--from-phase 6` on a range whose last checkpoint was 3 passed both
    existing guards (state exists, pipeline_version matches) and then ran the
    domain/final/beacon/tz-ready chain against machines that were never built, burying
    the real failure in confusing downstream errors. A missing/unusable `last_phase`
    (hand-made or pre-checkpoint state) reads as 0 — the conservative choice.
    --force-from-phase is the escape hatch for an operator who knows the checkpoint is
    stale (e.g. the process died after a phase finished but before its checkpoint
    landed); the default must stay refusing."""
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
    """The two infrastructure probes guard_resume_existence needs, as one dict.

    Indirection on purpose: it is the single seam a test patches to exercise the gate
    offline, and the single place a caller could inject a cheaper probe. Resolved at
    call time so a patch lands."""
    from infra.client import live_vmids
    from terraform_workdir import team_vmids_from_state
    return {"vmids_from_state": lambda: team_vmids_from_state(comp_dir),
            "vm_exists": live_vmids}


def guard_resume_existence(from_phase, comp_dir, state, force=False,
                           vmids_from_state=None, vm_exists=None):
    """Refuse a resume whose skipped phases left no artifacts behind.

    `guard_resume_from_phase` reads the checkpoint, which is a claim about work done —
    and a claim can be written by a run that did nothing. scale8 soak 2026-10-02 attempts
    9-11 resumed at --from-phase 5/6 after a phase-4 death: apply #2 never ran, the
    phase-5 repair sweep is strict=False so it "succeeded" against 32 nonexistent
    machines, and it checkpointed phase 5. Every later wait then raced a ghost, and the
    run spent ~40 minutes in wait_for_windows_sshd before anyone understood why.

    So: before trusting a skipped phase, verify the thing it produces. Each check is a
    separate callable so every branch is testable without Proxmox. Escalation stays
    available — an operator who genuinely has the infrastructure passes --force-from-phase;
    what this removes is the SILENT skip.

    Checks, by the phase whose output is being trusted:
      > 2  the engine VM exists (phase 2's terraform apply #1)
      > 4  every team box in terraform state exists as a live VM (apply #2)
    """
    if force:
        return
    probes = _artifact_probes(comp_dir)
    probe_vmids = vmids_from_state or probes["vmids_from_state"]
    probe_live = vm_exists or probes["vm_exists"]
    # Phase 2's apply #1 creates the engine; phase 4's apply #2 creates the
    # team boxes (its first half builds the goldens they clone from). So a resume that
    # skips phase 2 needs a live engine, and one that skips phase 4 needs live boxes.
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
            f"        Every later phase targets it (scoring setup, terraform context, "
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
        found = ("no team boxes at all are recorded in terraform state" if not vmids else
                 f"{len(missing)} of {len(vmids)} recorded team box(es) are gone: "
                 f"{', '.join(str(v) for v in missing[:8])}"
                 f"{' …' if len(missing) > 8 else ''}")
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} skips the phases that create the team boxes, "
            f"but {found}.\n"
            f"        The later phases would target machines that do not exist (this is the "
            f"scale8 soak's poisoned resume: the repair sweep reported success against 32 "
            f"ghosts and checkpointed it).\n"
            f"        Re-run with --from-phase 4 to recreate them."
        )


def catalog_check_paths(comp_dir):
    """Paths for `nakon catalog check`, filtered when the bundle uses constructs the
    catalog can't know about. Score-only pins ({"score_only": true}) name no catalog
    config; box_baseline.json pins plant like vulns but live in their own file. When
    neither is present the bundle's real files pass straight through. Returns
    (services_path, vulns_path, cleanup_fn)."""
    services = json.loads((comp_dir / "box_services.json").read_text())
    vulns = json.loads((comp_dir / "box_vulns.json").read_text())
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}

    def _is_score_only(c):
        return isinstance(c, dict) and c.get("score_only")

    filtered_services = {box: [p for p in pins if not _is_score_only(p)]
                         for box, pins in services.items()}
    merged_vulns = {box: list(v) + list(baseline.get(box, []))
                    for box, v in vulns.items()}
    if filtered_services == services and merged_vulns == vulns:
        return (comp_dir / "box_services.json", comp_dir / "box_vulns.json", None)

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
    """Headscale remote-access gate: the env contract is present and the headscale host is
    reachable. The subnet-overlap refusal lives in remote_access_ops.setup_remote_access.

    Raise SystemExit on the two refusal conditions; silent pass otherwise. Runs before
    any Proxmox mutation (remote access ships with every deploy, so a dead headscale host
    should stop a deploy at the gate, not mid-phase-3)."""
    import remote_access_ops

    identifiers = sorted({str(t["identifier"])
                          for share in plan.shares
                          for t in (share.teams or {}).values()
                          if isinstance(t, dict) and t.get("identifier") is not None})
    if not identifiers:
        return
    remote_access_ops._headscale_env()  # SystemExit naming the missing vars
    # cheap liveness probe: `headscale health` via the container
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
    if plan.engine_mgmt_ip:
        engine = next(s for s in plan.shares if s.is_engine)
        engine_mgmt_ip_gate(engine.node, engine.raw_vms, plan.engine_vmid,
                            plan.engine_mgmt_ip, ours_tags=plan.our_tags)
    if plan.placement:
        jump_mgmt_ip_gate(plan)
    from infra.red01 import red01_gate
    red01_gate(plan)
    catalog_gate(plan.comp_dir)
