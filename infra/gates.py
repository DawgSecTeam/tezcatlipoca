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
      1. concurrency      - no other deploy live on this host (flock signal)         [once]
      2. per node share   - templates resolve + carry cloud-init + engine base image + jump
                            source (infra.resources); vmid/bridge collisions
                            (infra.collisions, ownership-proved); datastore headroom
      3. mgmt IPs         - engine static IP, satellite jump IPs vs running guests
                            (infra.collisions, cross-node)
      4. catalog          - `nakon catalog check` over the competition's pins
      5. pins (advisory)  - warns when a managed box carries zero pins (never refuses)
      6. portal (advisory) - Compfile `portal 1`: console-token privileges, tunnel-token
                            hygiene in practice worktrees (never refuses)

The small gates (concurrency warning, catalog, unpinned-pins advisory, remote-access, portal
advisory) live here too rather than in one-file-each modules. `preflight_gates` (single-node)
and `preflight_gates_multinode` build a plan and call run_preflight.

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
from pathlib import Path

from constants import NAKON_DIR, PIPELINE_VERSION, RESUME_ATTEMPT_LIMIT, SCORING_ENGINE_VMID
from infra.collisions import (check_collisions, engine_mgmt_ip_gate,  # noqa: F401
                              jump_mgmt_ip_gate)
from infra.console import probe_permissions
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


def run_capacity_preflight(comp_dir, spec, secrets, identity, place, terraform,
                           prior, from_phase):
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
        reclaim_tag = prior.previous_state.get("run_id") or secrets.run_id
        if place.placement:
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
    from infra.client import live_vmids
    from terraform_workdir import team_vmids_from_state
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


def gate_concurrent_deploys():
    """Warn loudly when another deploy is live on this host, then proceed.

    Concurrency is the supported shape (2026-10-04): worktree-isolated runs with their
    own `--scoring-vmid` / `TF_VAR_team_identifiers` blocks coexist by design, and the
    gates that refuse REAL conflicts are per-resource — the vmid-collision and
    engine-mgmt-IP checks, plus run-id ownership tags scoping every teardown. This flock
    check stays as the loud signal, not a gate: the locks directory is the one
    concurrency signal that cannot lie (a holder is a live process, the kernel releases
    the flock when it dies, so a leftover `.lock` file is never a false positive), which
    is exactly what a WARNING should be built on. Two UNCOORDINATED sessions sharing vmid
    blocks is still the documented cause of the 13xx vmid races, the foreign-golden squat
    and the over-broad sweep (AGENTS.md) — the warning names the holder so the operator
    can check before the collision gates have to.

    The signal is only meaningful because of the call order in `deploy_lib.prepare`: the
    engine lock is taken BEFORE the preflight runs this gate, so any deploy that has got
    far enough to matter is already holding a lock."""
    from nakon_ops import other_deploys_in_flight

    in_flight = other_deploys_in_flight()
    if not in_flight:
        return
    summary = ", ".join(f"{Path(path).name} ({int(age)}s)" for path, age in in_flight[:4])
    if len(in_flight) > 4:
        summary += f", +{len(in_flight) - 4} more"
    print(f"  WARNING: {len(in_flight)} other deploy(s) in flight on this host ({summary}). "
          f"Concurrent ranges are supported — give this one its own --scoring-vmid and "
          f"TF_VAR_team_identifiers blocks (the preflight refuses real collisions); "
          f"teardowns are scoped by run-id ownership tags.")


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


def unpinned_managed_boxes(comp_dir, boxes):
    """Names of managed boxes with no entry in box_services/box_vulns/box_baseline.
    Unreadable pin files yield [] — the catalog gate owns reporting those."""
    pinned = set()
    for fname in ("box_services.json", "box_vulns.json", "box_baseline.json"):
        try:
            data = json.loads((comp_dir / fname).read_text())
        except (OSError, ValueError):
            if fname != "box_baseline.json":
                return []
            continue
        pinned |= {box for box, pins in data.items() if pins}
    return [b["name"] for b in boxes if not b.get("unmanaged") and b["name"] not in pinned]


def warn_unpinned_boxes(comp_dir, boxes):
    """Non-fatal: a zero-pin box plants a 0-step plan, which is a clean no-op."""
    names = unpinned_managed_boxes(comp_dir, boxes)
    if names:
        print(f"  Preflight WARNING: managed box(es) with zero pins: {', '.join(names)} — "
              f"the golden will be pristine and strict passes reconcile the no-op")
    return names


def remote_access_gate(plan):
    """Headscale remote-access gate: the env contract is present, the headscale host is
    reachable, and no OTHER node already advertises a subnet this deploy will claim.

    Raise SystemExit on the three refusal conditions; silent pass otherwise. Runs before
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
    conflicts = remote_access_ops.route_conflicts(plan.comp_name, identifiers)
    if conflicts:
        raise SystemExit(
            "  ERROR: headscale already has live subnet route(s) overlapping this "
            "competition's team subnets — two routers advertising the same /24 are one "
            "HA route group and would cross-route each other's participants:\n    "
            + "\n    ".join(conflicts)
            + "\n  Tear down the other range first, or deploy with disjoint team "
              "identifiers (TF_VAR_team_identifiers).")


def _linked_worktree(path):
    """True inside a linked git worktree (its `.git` is a file, not a directory)."""
    for d in [Path(path).resolve(), *Path(path).resolve().parents]:
        git = d / ".git"
        if git.exists():
            return git.is_file()
    return False


def _node_tokens(placement):
    if placement:
        for name, rec in placement["nodes"].items():
            yield name, rec["endpoint"], os.environ.get(rec["token_env"], "")
    else:
        yield (os.environ.get("TF_VAR_proxmox_node", "pve"),
               os.environ.get("TF_VAR_proxmox_endpoint", ""),
               os.environ.get("TF_VAR_proxmox_api_token", ""))


def portal_gate(comp_dir, placement=None, probe=probe_permissions):
    """Advisory gate: will the student portal (Compfile `portal 1`) be able to do its job?

    Never refuses — the portal is non-fatal by design (portal_ops) — but its two failure
    modes are far cheaper to hear about before an hour of cloning than at phase 8:

      - the deploy token cannot mint the per-comp console user/ACLs (infra.console), which
        would leave the portal up with consoles off;
      - a practice run from a linked worktree is about to claim the event's stable public
        hostname (TEZ_PORTAL_TUNNEL_TOKEN in a copied .env) — portal_ops refuses only when
        another LIVE range answers on it, so an idle event hostname would be taken silently.
    """
    from portal_ops import portal_enabled

    if not portal_enabled(comp_dir):
        return
    for name, endpoint, token in _node_tokens(placement):
        if not endpoint or not token:
            print(f"  WARNING: student portal — no endpoint/token for node {name}; its consoles "
                  "will be off")
            continue
        try:
            missing = probe(endpoint, token)
        except Exception as e:  # noqa: BLE001 - advisory
            print(f"  WARNING: student portal — could not probe {name}'s token permissions "
                  f"({type(e).__name__}: {e})")
            continue
        if missing:
            print(f"  WARNING: student portal — the deploy token on {name} lacks "
                  f"{', '.join(missing)}: the portal will come up with consoles OFF on that "
                  "node (docs/portal.md → Operating it)")
    if os.environ.get("TEZ_PORTAL_TUNNEL_TOKEN") and _linked_worktree(comp_dir):
        print("  WARNING: student portal — TEZ_PORTAL_TUNNEL_TOKEN is set in a linked worktree. "
              "A practice run will claim the stable public hostname "
              f"({os.environ.get('TEZ_PORTAL_HOSTNAME') or 'unset'}) unless a live range "
              "already answers on it; unset it for practice runs.")


def run_preflight(plan):
    """The single pre-Proxmox-mutation entry point."""
    gate_concurrent_deploys()
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
    catalog_gate(plan.comp_dir)
    warn_unpinned_boxes(plan.comp_dir, plan.boxes)
    portal_gate(plan.comp_dir, plan.placement)


def preflight_gates(comp_dir, boxes, num_teams, teams=None, engine_vmid=SCORING_ENGINE_VMID,
                    check_free=True, engine_mgmt_ip=None, our_run_tag=None):
    """Single-node preflight (a one-share plan over the whole cluster view).

    The collision gate (check_free) makes concurrent competitions on one node safe: it fails
    fast if this comp's engine vmid, any team vmid, or any team bridge already exists (i.e.
    belongs to another range). our_run_tag (the prior state's run id) scopes the "ours"
    exemption to THIS deploy's lineage — a same-comp VM without it is another run's and stays
    a collision (2026-10-02 near-miss)."""
    from infra.plan import single_node_plan
    run_preflight(single_node_plan(comp_dir, boxes, num_teams, teams, engine_vmid, check_free,
                                   engine_mgmt_ip, our_run_tag))


def preflight_gates_multinode(comp_dir, boxes, teams, engine_vmid, placement,
                              engine_mgmt_ip=None, check_free=True, our_run_tag=None):
    """Per-node preflight for a multi-node placement: every hosting node gets its own
    template/collision/headroom gate scoped to exactly what it will hold (engine node:
    engine base + slot-0 goldens + its teams; each satellite: its slot's goldens, the
    jump VM, its teams). A satellite that would have died mid-clone on another host fails
    here instead."""
    from infra.plan import multinode_plan
    run_preflight(multinode_plan(comp_dir, boxes, teams, engine_vmid, placement,
                                 engine_mgmt_ip, check_free, our_run_tag))
