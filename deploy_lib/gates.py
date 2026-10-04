"""Every refusal the deploy makes BEFORE it builds or destroys anything — in one place.

The gates used to be scattered across prepare()'s helpers (a version check in the state
loader, a stale-terraform-state guard buried in the tfvars builder, the capacity preflight in
its own helper, the lock in deploy()). They are collected here and called from exactly two
spots in prepare.py, in a fixed order:

  1. check_resume_gates   — right after the prior state is read: a missing state file, a
                            state written by another pipeline version, a --from-phase that
                            skips unfinished phases, a resume-loop, a checkpoint whose
                            machines do not exist.
  2. run_range_gates      — after placement, BEFORE any secret is minted or tfvars written:
                            the stale-terraform-state host/vmid refusal, then the capacity /
                            collision / template preflight (config_ops.preflight_gates*).

Plus acquire_deploy_lock (one driver per competition dir), taken first thing by deploy(),
and DeployContext's checkpoint gate (CHECKPOINT_GATES / guard_resume_existence), which
refuses to record a phase whose output does not exist.

The destroy-side ownership rules (tag-scoped reclaim, frozen gate) are enforced where the
destroy decision is made — see golden_plan.py — and in range_ops.destroy_vm_if_exists.
Every gate raises SystemExit with an operator-actionable message; none is bypassed except
by the explicit --force-from-phase flag where documented.
"""

import fcntl
import json
import os
from pathlib import Path

from config_ops import preflight_gates
from constants import PIPELINE_VERSION, RESUME_ATTEMPT_LIMIT, SCORING_ENGINE_VMID
from range_ops import team_vmids_from_state, terraform_dir

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
    not skip phases the state never saw complete), guard_resume_streak (no resume-loops)
    and guard_resume_existence (the checkpoint's machines are really there). `force` is
    --force-from-phase, which relaxes only those three."""
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
    _pv = None
    try:
        _pv = json.loads(state_path.read_text()).get("pipeline_version")
    except (ValueError, OSError):
        pass
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
    # ...and refuse a resume-loop: the same phase failing the same way over and
    # over is not a repair (see guard_resume_streak).
    guard_resume_streak(from_phase, previous_state, force=force)
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


def run_capacity_preflight(comp_dir, spec, secrets, identity, place, terraform,
                           prior, from_phase):
    """Run the competition preflight gates, at the phase-2 boundary.

    Gated on from_phase <= 2 because the gates check free capacity and template
    reachability for the work phases 1-2 do; a later resume must not re-refuse against
    an already-deployed range, which is also why check_free is off on a resume. The
    multinode gate is imported locally: only this branch pulls in the placement
    preflight machinery."""
    if from_phase <= 2:
        # The clash gates' "ours" exemption must be run-aware: the prior state's run
        # id is what marks THIS competition directory's leftovers ours. With no prior
        # state it falls back to the minted id — pre-existing comp-tagged VMs then
        # classify as foreign and the gate refuses (fail-closed beats guessing whose
        # VM it is).
        reclaim_tag = prior.previous_state.get("run_id") or secrets.run_id
        if place.placement:
            from config_ops import preflight_gates_multinode
            preflight_gates_multinode(comp_dir, spec.boxes, secrets.teams, identity.engine_vmid, place.placement,
                                      engine_mgmt_ip=terraform.engine_mgmt_ip,
                                      check_free=not prior.resuming, our_run_tag=reclaim_tag)
        else:
            preflight_gates(comp_dir, spec.boxes, secrets.number_of_teams, teams=secrets.teams,
                            engine_vmid=identity.engine_vmid, check_free=not prior.resuming,
                            engine_mgmt_ip=terraform.engine_mgmt_ip, our_run_tag=reclaim_tag)


def run_range_gates(comp_dir, spec, secrets, identity, place, terraform, prior, from_phase):
    """Every refusal that needs the resolved placement, before any credential is minted.

    Order: the cheap local stale-terraform-state check first, then the capacity /
    collision / template preflight (which queries Proxmox). Both must run before the fresh
    secret mint — see prepare()'s order note on credential-desync corpses."""
    check_stale_terraform_state(comp_dir, spec.comp_name, prior.previous_state,
                                identity.engine_vmid, from_phase)
    run_capacity_preflight(comp_dir, spec, secrets, identity, place, terraform, prior,
                           from_phase)


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
    """The three infrastructure probes guard_resume_existence needs, as one dict.

    Indirection on purpose: it is the single seam a test patches to exercise the gate
    offline, and the single place a caller could inject a cheaper probe. Resolved at
    call time so a patch lands."""
    from range_ops import live_vmids
    return {"vmids_from_state": lambda: team_vmids_from_state(comp_dir),
            "vm_exists": live_vmids,
            "box_reachable": _any_box_reachable}


def guard_resume_existence(from_phase, comp_dir, state, force=False,
                           vmids_from_state=None, vm_exists=None, box_reachable=None):
    """Refuse a resume whose skipped phases left no artifacts behind.

    `guard_resume_from_phase` reads the checkpoint, which is a claim about work done —
    and a claim can be written by a run that did nothing. scale8 soak 2026-10-02 attempts
    9-11 resumed at --from-phase 5/6 after a phase-4 death: apply #2 never ran, the
    phase-5 repair sweep is strict=False so it "succeeded" against 32 nonexistent
    machines, and it checkpointed phase 5. Every later wait then raced a ghost, and the
    run spent ~40 minutes in wait_for_windows_sshd before anyone understood why.

    So: before trusting a skipped phase, verify the thing it produces. Each check is a
    separate callable so every branch is testable without Proxmox, and so the checkpoint
    path (C-checkpoint) can reuse them. Escalation stays available — an operator who
    genuinely has the infrastructure passes --force-from-phase; what this removes is the
    SILENT skip.

    Checks, by the phase whose output is being trusted:
      > 2  the engine VM exists (phase 2's terraform apply #1)
      > 4  every team box in terraform state exists as a live VM (apply #2)
      > 5  the postclone sweep ran and at least one box answers on SSH
    """
    if force:
        return
    probes = _artifact_probes(comp_dir)
    probe_vmids = vmids_from_state or probes["vmids_from_state"]
    probe_live = vm_exists or probes["vm_exists"]
    # Phase 2's apply #1 creates the engine; phase 4's apply #2 creates the
    # team boxes (its first half builds the goldens they clone from). So a resume that
    # skips phase 2 needs a live engine, and one that skips phase 4 needs live boxes.
    needs_engine = from_phase > 2
    needs_boxes = from_phase > 4
    needs_live_boxes = from_phase > 5
    if not needs_engine:
        return

    try:
        live = set(probe_live())
    except RuntimeError as e:
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} cannot confirm the machines exist: {e}\n"
            f"        Refusing to resume blind — re-run with --from-phase "
            f"{_safe_resume_target(2)}, or pass --force-from-phase once you have confirmed "
            f"the machines exist by hand."
        )

    if needs_engine:
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
            f"        Refusing to resume blind. Re-run with --from-phase {_safe_resume_target(2)} "
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

    if not needs_live_boxes:
        return

    reachable = box_reachable or probes["box_reachable"]
    ok, why = reachable(comp_dir, state)
    if not ok:
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} trusts the post-clone work, but {why}.\n"
            f"        Re-run with --from-phase 5 so the bootstrap/sweep phases run against the "
            f"existing VMs."
        )


def _safe_resume_target(phase):
    """The earliest phase that rebuilds the missing artifact — what the operator
    should actually type, rather than the phase they asked for."""
    return max(1, phase)


# Phases whose output is independently checkable, so their checkpoint can be verified
# before it is written. Phase 3 is "prepare the engine clone" (apply #1 has created it),
# phase 4 is "goldens + apply #2" (the team boxes now exist). Everything else either
# builds nothing durable (1, 7) or builds images/state that the resume gates already
# cover indirectly — inventing a proxy check would refuse good deploys, which is worse
# than the lie it would prevent.
CHECKPOINT_GATES = {3, 4}


def _any_box_reachable(comp_dir, state):
    """(ok, why) for 'the post-clone sweep ran and at least one box answers'.

    Two separate facts, deliberately: the marker proves the sweep executed, the SSH
    probe proves the boxes are real. A range whose marker exists but whose boxes are
    dark is exactly the state that produced the phantom repair sweep."""
    marker = Path(comp_dir) / ".postclone-swept"
    if not marker.exists():
        return False, f"{marker.name} is absent (the post-clone sweep never completed)"
    # Cheapest and most specific first: with no team boxes on record there is nothing to
    # probe, and reading the terraform context to discover that would run terraform (and
    # could fail) for no reason.
    targets = _team_box_ips(comp_dir)
    if not targets:
        return False, "no team box addresses are recorded to probe"
    try:
        from ssh_ops import read_terraform_ctx, ssh_via_gateway
        ctx = read_terraform_ctx(comp_dir)
    except Exception as e:                                  # noqa: BLE001 - reported
        return False, f"the engine context could not be read to probe a box ({e})"
    for ip in targets[:3]:
        try:
            proc = ssh_via_gateway(ctx, ip, "echo RESUME-OK", timeout=25)
        except Exception:                                   # noqa: BLE001 - try the next
            continue
        if "RESUME-OK" in (proc.stdout or ""):
            return True, ""
    return False, (f"none of the first {min(3, len(targets))} team box(es) answered over SSH "
                   f"({', '.join(targets[:3])})")


def _team_box_ips(comp_dir):
    """Every team box IP recorded for this competition, engine excluded.

    targets.json is written by phase 2 and is the deploy's own record of what it built;
    nakon-config.json is the fallback for a range deployed before targets.json existed."""
    try:
        data = json.loads((Path(comp_dir) / "targets.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for name, rec in (data.get("targets") or {}).items():
        ip = (rec or {}).get("ip") if isinstance(rec, dict) else None
        if ip and str(name).startswith("team"):
            out.append(str(ip))
    return out


def guard_resume_streak(from_phase, state, limit=RESUME_ATTEMPT_LIMIT, force=False):
    """Refuse the (limit+1)-th consecutive resume of a phase failing the same way.

    `docs/e2e-testing.md` has always said "max 2 repair-resume cycles; a third
    consecutive resume is not a repair, it's a resume-loop", but that lived only in
    prose and cde-2026 burned eleven attempts (deploy6 -> deploy16, 2026-09-29/30)
    on a single Windows golden. This is the enforceable version. It refuses *before*
    any infrastructure work, and names the path that actually helps.
    """
    if force:
        return
    streak = state.get("failure_streak") or {}
    count = int(streak.get("count") or 0)
    if streak.get("phase") != from_phase or count < limit:
        return
    raise SystemExit(
        f"  ERROR: phase {from_phase} has now failed {count} time(s) in a row with the "
        f"same error:\n"
        f"    {streak.get('signature')}\n"
        f"  That is a resume-loop, not a repair (docs/e2e-testing.md: max 2 repair-resume "
        f"cycles). Resuming again would repeat it. Either fix the underlying cause and "
        f"pass --force-from-phase once you have, or tear the range down and redeploy:\n"
        f"    python3 destroy-competition.py <competition>\n"
        f"    python3 create-competition.py --competition <competition> --teams N --yes"
    )
