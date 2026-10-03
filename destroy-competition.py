"""Teardown: destroy API-cloned VMs and `terraform destroy`."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import urllib3
from dotenv import load_dotenv

import artifacts_ops
from constants import GOLDEN_TAG, SCORING_ENGINE_VMID, ownership_tags
from golden_ops import destroy_golden_set
from jump_ops import destroy_jump_vms
from nodes_ops import activate_placement, read_placement, record_of
from range_ops import parse_vm_tags, proxmox_api, wait_for_proxmox_task
from template_ops import destroy_engine_template, frozen_state
from utils import load_compfile, pick_competition, run_terraform

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)

# Concurrent stop/delete worker count for teardown sweeps. Deletes are
# metadata-light on ZFS (the documented datastore-saturation hazard belongs to
# bulk clone writes, not deletes); 4 keeps even a shared node comfortable.
TEARDOWN_WORKERS = 4

# Per-attempt wall-clock bound for `terraform destroy`. Without it,
# utils.run_terraform waits on proc.wait(timeout=None) forever (utils.py), and the
# documented hang mode is a Windows DC whose guest agent is down holding the qm lock
# mid-destroy (see pre_stop_windows_boxes). A hung destroy also never reaches the
# clear_stale_state_lock/sweep_tagged_leftovers recovery below, and prints nothing —
# the operator just loses the range teardown to a silent hang. Every `apply` in the
# pipeline is already bounded (deploy.py); destroy was the one unbounded call.
# 1800s matches the scoring-engine/apply budget: a full destroy of a multi-team range
# fits well inside it, and a real timeout is a hang, not slow progress.
DESTROY_ATTEMPT_TIMEOUT_S = 1800


def load_destroyable_competitions():
    return [
        p.name
        for p in sorted(Path("competitions").iterdir())
        if p.is_dir()
        and (p / "Compfile").exists()
        and (p / "teams.json").exists()
        and (p / "boxes.json").exists()
    ]


def _entry_node(entry, fallback):
    """Per-VM node: targets.json carries 'node' for multi-node ranges; the env node
    (engine node when a placement is active) covers single-node entries."""
    return entry.get("node") or fallback if isinstance(entry, dict) else fallback


def destroy_cloned_vms(cloned_vms_path, default_node):
    cloned_vms = json.loads(cloned_vms_path.read_text())
    if not cloned_vms:
        return

    # Only purge vmids that still exist AND whose VM name is obviously this
    # competition's (clone names look like '102-dc01'): the deterministic vmid
    # scheme collides across competitions, and cloned_vms.json can outlive the
    # range — purging bare vmids on a stale file would hit someone else's VM.
    # Multi-node: each entry names its own node; listings come per node.
    live_by_node = {}
    listing_failed = set()
    for entry in cloned_vms.values():
        n = _entry_node(entry, default_node)
        if n not in live_by_node:
            try:
                live_by_node[n] = {
                    int(v["vmid"]): (v.get("name") or "")
                    for v in proxmox_api("GET", f"/nodes/{n}/qemu")["data"]
                }
            except Exception as e:
                # Fail CLOSED: the old behavior purged the node's entries without
                # existence checks when the listing failed — blind deletes on a
                # shared node are exactly what this tool must never do.
                print(f"  WARNING: could not list VMs on {n} — that node's clone-map "
                      f"entries are SKIPPED (re-run teardown once the API answers): {e}")
                listing_failed.add(n)
                live_by_node[n] = None

    print(f"  Destroying {len(cloned_vms)} cloned VM(s) before terraform destroy "
          f"({TEARDOWN_WORKERS} workers)...")

    def _destroy_clone(item):
        vm_key, entry = item
        vmid = entry["vmid"] if isinstance(entry, dict) else entry
        node = _entry_node(entry, default_node)
        live = live_by_node.get(node)
        if node in listing_failed:
            return (f"    Skipping {vm_key} (vmid {vmid}) — node {node} listing failed; "
                    f"NOT deleting without a live check")
        if live is not None:
            name = live.get(int(vmid))
            if name is None:
                return f"    Skipping {vm_key} (vmid {vmid}) — VM does not exist on {node}"
            if name != vm_key:
                return (f"    Skipping {vm_key} (vmid {vmid}) — VM exists as '{name}', which "
                        f"doesn't match this competition's clone name")
        try:
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
            wait_for_proxmox_task(node, upid)
        except Exception:
            pass
        try:
            upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}",
                               params={"purge": 1})["data"]
            wait_for_proxmox_task(node, upid)
            return f"    Deleted {vm_key} (vmid {vmid})"
        except Exception as e:
            return f"    WARNING: could not delete {vm_key} (vmid {vmid}): {e}"

    with ThreadPoolExecutor(max_workers=TEARDOWN_WORKERS) as pool:
        for line in pool.map(_destroy_clone, cloned_vms.items()):
            print(line, flush=True)


def pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=None, expect_tags=None):
    """Hard-stop EVERY team clone before terraform destroy. The bpg provider issues
    a graceful shutdown with a long timeout: a DC whose guest agent is down never
    complies and holds the qm lock, hanging the whole destroy (pfsense-rvb: 6m+
    'Still destroying', qm unlock/stop timing out behind it) — and even a well-
    behaved Linux box burns minutes in ACPI shutdown it doesn't need, since the
    disk is about to be destroyed (operator call 2026-09-30: forced stop + remove,
    not clean shutdown). A hard API stop needs no agent; an already-stopped box
    skips the graceful path entirely. Clones already stopped/deleted by
    destroy_cloned_vms are simply not running here. team_nodes (multi-node)
    routes each team's sweep to its hosting node.

    expect_tags (the full ownership set, comp tag + run tag) gates every stop: the
    name pattern `<identifier>-<box>` is shared by every worktree running the same
    competition ID, so a name match alone would hard-stop ANOTHER run's boxes.
    Legacy state (no run id) passes the comp tag set and keeps today's behavior."""
    windows = [b["name"] for b in boxes]  # all clones hard-stop, not just Windows
    if not windows:
        return
    live_by_node = {}

    def _live(node):
        if node not in live_by_node:
            try:
                live_by_node[node] = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
            except Exception as e:
                print(f"  WARNING: could not list VMs on {node} — skipping the "
                      f"pre-stop: {e}")
                live_by_node[node] = []
        return live_by_node[node]

    targets = []
    for team_key, team in teams.items():
        node = (team_nodes or {}).get(team_key, default_node)
        for v in _live(node):
            vm_name = v.get("name") or ""
            if vm_name not in {f"{team['identifier']}-{box}" for box in windows}:
                continue
            if expect_tags is not None and not expect_tags <= parse_vm_tags(v.get("tags")):
                print(f"  Skipping pre-stop of {vm_name} (vmid {v['vmid']}) on {node} — "
                      f"tags '{v.get('tags')}' miss the expected ownership set "
                      f"{sorted(expect_tags)}; likely ANOTHER run of this competition ID")
                continue
            targets.append((vm_name, int(v["vmid"]), node))

    def _hard_stop(item):
        vm_name, vmid, node = item
        try:
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
            wait_for_proxmox_task(node, upid)
            return f"  Pre-stopped {vm_name} (vmid {vmid}) — graceful-shutdown hang avoided"
        except Exception as e:
            return (f"  WARNING: pre-stop of {vm_name} failed ({e}). If terraform "
                    f"destroy hangs on it: kill -9 $(cat /var/run/qemu-server/"
                    f"{vmid}.pid) on the node, then re-run.")

    # status/stop is the hard API stop (no guest agent involved) — a VM we are
    # about to destroy gets no graceful shutdown.
    with ThreadPoolExecutor(max_workers=TEARDOWN_WORKERS) as pool:
        for line in pool.map(_hard_stop, targets):
            print(line, flush=True)


def clear_stale_state_lock(tf_cwd):
    """A SIGKILLed deploy/destroy leaves .terraform.tfstate.lock.info behind; every
    later destroy then dies on 'Error acquiring the state lock'. The lock is only
    real while a terraform process is alive — with none running, remove it.
    Returns True when a stale lock was cleared."""
    lock_file = Path(tf_cwd) / ".terraform.tfstate.lock.info"
    if not lock_file.exists():
        return False
    running = subprocess.run(["pgrep", "-x", "terraform"], capture_output=True)
    if running.returncode == 0:
        print("  state lock present AND a terraform process is alive — not touching "
              "it; settle that process first")
        return False
    lock_file.unlink()
    print("  removed stale terraform state lock (no terraform process running)")
    return True


def sweep_tagged_leftovers(nodes, competition, run_id=None, legacy=False):
    """Completeness pass for a failed/interrupted teardown: destroy every VM still
    carrying this deploy's FULL ownership tag set (`tezcatlipoca` AND `comp-<name>`
    AND the deploy's `run-<id>` tag) — e.g. clones a killed deploy left out of
    terraform state. Foreign VMs are never candidates: partial tag overlap does not
    count (loadtest-2026-09-30), and a same-comp VM tagged with a DIFFERENT run id
    belongs to another worktree's run — equally untouchable (2026-10-02 near-miss).

    run_id comes from .deploy_state.json. With none (a pre-run-id deploy) the sweep
    is SKIPPED unless `legacy` (--legacy-tags) is passed explicitly — comp tags
    alone cannot prove which run tagged the VM. Multi-node: every host in `nodes`
    is swept."""
    if run_id:
        comp_tags = ownership_tags(competition, run_id)
    elif legacy:
        comp_tags = {"tezcatlipoca", f"comp-{competition}"}
        print("  WARNING: --legacy-tags — no run id in .deploy_state.json, so the "
              "sweep matches by comp tags ONLY; it cannot prove which run tagged "
              "each VM (another worktree running this competition ID would be hit).")
    else:
        print("  Leftover sweep SKIPPED: no run id in .deploy_state.json, so ownership "
              "cannot be proven. Pass --legacy-tags to sweep by comp tags only.")
        return
    ours = []
    for node in nodes:
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        except Exception as e:
            print(f"  WARNING: leftover sweep skipped on {node} — listing failed: {e}")
            continue
        ours += [dict(v, _node=node) for v in vms
                 if comp_tags <= parse_vm_tags(v.get("tags"))]
    if not ours:
        return
    print(f"  Leftover sweep: {len(ours)} VM(s) carrying this deploy's full ownership set "
          f"({sorted(comp_tags)}) — destroying ({TEARDOWN_WORKERS} workers)...")

    def _kill(v):
        vmid, node = v["vmid"], v["_node"]
        try:
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
            wait_for_proxmox_task(node, upid)
        except Exception:
            pass
        try:
            upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}",
                               params={"purge": 1})["data"]
            wait_for_proxmox_task(node, upid)
            return f"    swept {v.get('name')} (vmid {vmid}) on {node}"
        except Exception as e:
            return f"    WARNING: sweep could not delete vmid {vmid}: {e}"

    with ThreadPoolExecutor(max_workers=TEARDOWN_WORKERS) as pool:
        for line in pool.map(_kill, ours):
            print(line, flush=True)


def report_remaining(nodes, competition, teams, run_id=None):
    """Final accounting after every recovery attempt failed: exactly what is still
    standing (per node), so a human decides — nothing is force-deleted here. Every
    competition-tagged VM is classified: OURS (this deploy's run tag), same comp
    DIFFERENT run (another worktree's — never touched by this teardown), or
    untagged-run (a pre-run-id deploy of this competition)."""
    comp_tag = f"comp-{competition}"
    left_total = 0
    for node in nodes:
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
            left = [v for v in vms if comp_tag in str(v.get("tags") or "")]
            for v in left:
                tags = parse_vm_tags(v.get("tags"))
                if run_id and run_id in tags:
                    kind = "OURS — still standing, teardown failed on it"
                elif any(t.startswith("run-") for t in tags):
                    kind = ("same competition, DIFFERENT run — NOT touched by this "
                            "teardown (another worktree's?)")
                else:
                    kind = "competition-tagged, no run id recorded (pre-run-id deploy)"
                print(f"    still standing on {node}: {v.get('name')} (vmid {v['vmid']})"
                      f" — {kind}")
            left_total += len(left)
            if not left:
                print(f"    no competition-tagged VMs remain on {node}")
        except Exception as e:
            print(f"    WARNING: could not list remaining VMs on {node}: {e}")
        try:
            idents = {str(t["identifier"]) for t in teams.values()}
            # vmbrW<id>: the transit bridges an in-path firewall lineup adds.
            names = {f"vmbr{i}" for i in idents} | {f"vmbrW{i}" for i in idents}
            nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
            bridges = [n["iface"] for n in nets
                       if n.get("type") == "bridge" and n["iface"] in names]
            for b in bridges:
                print(f"    bridge still present on {node}: {b} (terraform state "
                      f"should own it — resolve the state, then re-run)")
        except Exception as e:
            print(f"    WARNING: could not list bridges on {node}: {e}")


def destroy_with_recovery(env, tf_cwd, placement_nodes, competition, run_id=None,
                          legacy=False):
    """Run `terraform destroy` with bounded attempts and recovery between them.

    Teardown is resumable: every pass makes progress, and each failure gets
    (a) stale state-lock recovery (a SIGKILLed deploy leaves the lock behind),
    (b) a run-id-scoped sweep of clones a killed deploy left out of terraform state,
    then a retry — until terraform completes or nothing of ours is left standing.

    Returns True when terraform reported success, False once the attempts are exhausted.
    A per-attempt timeout is treated as just another failed attempt so control reaches
    the recovery steps instead of escaping as an exception (utils.run_terraform raises
    TimeoutExpired even with check=False)."""
    for attempt in (1, 2, 3, 4):
        if attempt > 1:
            clear_stale_state_lock(tf_cwd)
            sweep_tagged_leftovers(placement_nodes, competition, run_id=run_id, legacy=legacy)
        try:
            proc = run_terraform(
                ["destroy", "-parallelism=4", "-auto-approve"],
                cwd=tf_cwd,
                env=env,
                timeout=DESTROY_ATTEMPT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # utils.run_terraform already SIGINT'd the process group and reaped
            # stragglers, so the state lock is released and a retry is safe.
            print(f"  terraform destroy timed out after {DESTROY_ATTEMPT_TIMEOUT_S}s "
                  f"(attempt {attempt}/4, partial progress kept)...")
            continue
        if proc.returncode == 0:
            return True
        print(f"  terraform destroy failed (attempt {attempt}/4, partial progress kept)...")
    return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Destroy a deployed competition. Default (teams-only) keeps the "
                    "competition's golden + engine templates for the next test run of "
                    "the SAME competition; --full removes them too. Goldens never "
                    "carry across competitions — tear down --full once the run's "
                    "goal is met.")
    parser.add_argument("--competition", metavar="NAME",
                        help="competition directory under competitions/ (skips the picker)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the type-the-ID confirmation (for scripted teardown)")
    parser.add_argument("--full", action="store_true",
                        help="M4 full teardown: also destroy the golden templates and the "
                             "engine template (after every clone is gone). Default is "
                             "teams-only: templates are kept and the next deploy reuses "
                             "or rebuilds them by hash.")
    parser.add_argument("--end-of-competition", action="store_true", dest="end_of_competition",
                        help="required with --full when the competition is FROZEN — an "
                             "accidental full teardown during the event must be impossible.")
    parser.add_argument("--legacy-tags", action="store_true", dest="legacy_tags",
                        help="Enable the comp-tag-only leftover sweep for a competition "
                             "whose state predates per-deploy run ids. Without a run id the "
                             "sweep cannot prove which run tagged each VM — another "
                             "worktree running the same competition ID would be hit — so "
                             "it is refused without this flag.")
    parser.add_argument("--allow-untagged", action="store_true", dest="allow_untagged",
                        help="Also destroy UNTAGGED VMs sitting on this competition's "
                             "computed vmids (pre-tagging-era ranges). Default: refuse and "
                             "report them.")
    parser.add_argument("--skip-artifacts", action="store_true", dest="skip_artifacts",
                        help="Skip collecting this run's test artifacts into "
                             "competitions/<id>/.automated-tests/<run-id>/ before destroying. "
                             "Normally collected first, because the red report and the blue "
                             "logs only exist while the boxes do.")
    parser.add_argument("--artifacts-timeout", type=int, default=45, metavar="SEC",
                        dest="artifacts_timeout",
                        help="Per-file timeout for the artifact pull (default 45s). The pull "
                             "never blocks the destroy; it warns and proceeds.")
    args = parser.parse_args()

    print("=" * 64)
    print("  COMPETITION TEARDOWN TOOL")
    print("=" * 64)
    print("Selects a deployed competition and runs terraform destroy to")
    print("remove all provisioned VMs, bridges, and networking.\n")

    competitions = load_destroyable_competitions()
    if not competitions:
        print("No destroyable competitions found.")
        print("(Competitions must have been deployed with create-competition.py")
        print("after teams.json support was added to qualify.)")
        sys.exit()

    if args.competition:
        if args.competition not in competitions:
            print(f"'{args.competition}' is not a destroyable competition. Found: {', '.join(competitions)}")
            sys.exit(1)
        competition = args.competition
    else:
        competition = pick_competition(competitions, label="destroyable", action="Select a competition to destroy")
    if competition is None:
        print("Quitting.")
        sys.exit()

    comp_dir = Path("competitions") / competition
    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    frozen = frozen_state(comp_dir)

    # Multi-node: the placement record is authoritative — point the env at the
    # engine's host and register the node routes before anything node-scoped runs.
    placement = read_placement(comp_dir)
    team_nodes = {}
    default_node = os.environ.get("TF_VAR_proxmox_node", "pve")
    placement_nodes = [default_node]
    if placement:
        placement_nodes = [placement["engine_node"]] + [
            record_of(placement, s["name"]).node for s in placement["satellites"]]
    if placement:
        activate_placement(placement)
        team_nodes = placement["team_nodes"]
        sats = ", ".join(f"{s['name']} (slot {s['slot']}, teams {','.join(s['teams'])})"
                         for s in placement["satellites"])
        print(f"  Multi-node placement: engine on '{placement['engine_node']}'"
              + (f"; satellites: {sats}" if sats else ""))

    # The refusal must fire BEFORE any destruction — a frozen competition's templates
    # are the verified artifacts the event runs on.
    if args.full and frozen and not args.end_of_competition:
        print(f"  ERROR: '{competition}' is FROZEN (frozen_at {frozen.get('frozen_at')}). "
              f"A full teardown would destroy the verified templates mid-competition. "
              f"Re-run with --end-of-competition if the event is genuinely over.")
        sys.exit(1)

    mode = "FULL (templates destroyed)" if args.full else "teams-only (templates kept)"
    print("─── About to destroy " + "─" * 42)
    print(f"  Competition : {name} ({competition})")
    print(f"  Teams       : {len(teams)}")
    print(f"  Boxes       : {len(boxes)} type(s)")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}")
    print(f"  Mode        : {mode}"
          + ("  [FROZEN — end-of-competition flag present]" if frozen and args.end_of_competition else ""))
    # .deploy_state.json is read ONCE: it carries the deploy's run id (the
    # destruction-ownership anchor) and the engine vmid, plus the endpoint guard.
    deployed_state = {}
    state_path = comp_dir / ".deploy_state.json"
    if state_path.exists():
        try:
            deployed_state = json.loads(state_path.read_text())
        except (ValueError, OSError):
            deployed_state = {}
    run_id = deployed_state.get("run_id") or ""
    if deployed_state:
        deployed = (deployed_state.get("deployed_endpoint") or "").rstrip("/")
        current = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
        if deployed and current and deployed != current:
            raise SystemExit(
                f"  ERROR: this competition was deployed against {deployed}, but the "
                f"loaded env targets {current}. Point TF_VAR_* at the deployment's host "
                f"(same overrides create-competition ran with) and re-run.")
    if run_id:
        print(f"  Ownership: run id '{run_id}' — only VMs carrying this deploy's FULL "
              f"tag set (tezcatlipoca + comp-{competition} + {run_id}) will be touched.")
    else:
        print(f"  Ownership: COMP TAGS ONLY (no run id in .deploy_state.json — a "
              f"pre-run-id deploy). The leftover sweep is OFF unless --legacy-tags is "
              "passed; recorded-vmid deletes keep the comp-tag guard.")
    print()
    print("  This will run: terraform destroy -parallelism=4 -auto-approve")
    if args.full:
        print("  Then the golden templates and the engine template are destroyed "
              "(clones first — their base disks depend on them).")
    else:
        print("  The golden templates and the engine template are KEPT (M4 teams-only): "
              "the next deploy reuses them by hash.")
    print()

    if args.yes:
        print(f"  --yes: skipping confirmation for '{competition}'.")
    else:
        confirm = input(f"  Type the competition ID to confirm ({competition}): ").strip()
        if confirm != competition:
            print("Cancelled — nothing was destroyed.")
            sys.exit()

    env = {**os.environ}
    env["TF_VAR_teams"] = json.dumps(teams)
    env["TF_VAR_boxes_per_team"] = json.dumps(boxes)
    env["TF_VAR_event_name"] = name

    # ── Collect this run's test artifacts — BEFORE anything is stopped or deleted ──────────
    # Last chance by construction: the red report and the blue logs live on machines that are
    # about to be destroyed, and the calls immediately below end that (destroy_cloned_vms
    # purges clones; pre_stop_windows_boxes hard-stops every team box, after which a stopped
    # guest's agent can no longer answer). This is also the safety net for a run whose harness
    # died: teardown is the one script AGENTS.md tells you to re-run until clean, so it is the
    # only step guaranteed to happen. It warns and proceeds — a dead box must never wedge a
    # teardown — and records what it could not get in collection.json / REPORT.md.
    if args.skip_artifacts:
        print("\n  Artifacts  : skipped (--skip-artifacts)")
    else:
        print("\nCollecting test artifacts before teardown...")
        try:
            artifacts_ops.collect_for_teardown(
                comp_dir, run_id=run_id or None, teams=teams, boxes=boxes,
                node=default_node, nodes=placement_nodes,
                script="destroy-competition.py", timeout=args.artifacts_timeout)
        except Exception as e:  # never let bookkeeping outrank destroying the range
            print(f"  WARNING: artifact collection failed ({type(e).__name__}: {e}) — "
                  "continuing with the teardown; nothing was destroyed by this step.")
        print()

    cloned_vms_path = comp_dir / "cloned_vms.json"
    if cloned_vms_path.exists():
        destroy_cloned_vms(cloned_vms_path, default_node)
    pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=team_nodes,
                           expect_tags=ownership_tags(competition, run_id))

    # Destroy from this competition's own per-comp state dir when it exists (multi-tenant
    # deploys), so we tear down only this comp's engine/boxes/bridges; fall back to the
    # legacy shared terraform/ dir for competitions deployed before per-comp isolation.
    per_comp_tf = comp_dir / "terraform"
    tf_cwd = str(per_comp_tf) if (per_comp_tf / "terraform.tfstate").exists() else "terraform"

    print(f"\nRunning terraform destroy for '{name}' (state: {tf_cwd})...")
    if not destroy_with_recovery(env, tf_cwd, placement_nodes, competition,
                                 run_id=run_id, legacy=args.legacy_tags):
        report_remaining(placement_nodes, competition, teams, run_id=run_id)
        sys.exit("ERROR: terraform destroy did not complete after recovery attempts — "
                 "resolve what is listed above, then re-run this command (it is safe "
                 "to re-run: teardown is idempotent).")

    # Archive the clone map on success: a surviving cloned_vms.json next to a
    # destroyed range is a loaded gun for the vmid-collision guard above.
    if cloned_vms_path.exists():
        archived = cloned_vms_path.with_name(
            cloned_vms_path.name + f".destroyed-{time.strftime('%Y%m%d-%H%M%S')}")
        cloned_vms_path.rename(archived)
        print(f"  Clone map archived as {archived.name}")

    # M3: the golden templates are API-created (not in terraform state) and are the
    # linked clones' base disks — they die only after terraform destroy removed every
    # clone. Empty for pre-golden ranges too: the vmids simply won't exist.
    engine_vmid = deployed_state.get("scoring_vm_id") or SCORING_ENGINE_VMID
    try:
        engine_vmid = int(engine_vmid)
    except (TypeError, ValueError):
        engine_vmid = SCORING_ENGINE_VMID
    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    if args.full:
        print(f"  Destroying golden templates (engine vmid {engine_vmid} + 150 + i)...")
        destroy_golden_set(node, engine_vmid, len(boxes), slot=0,
                           expect_tags=ownership_tags(competition, run_id, GOLDEN_TAG),
                           allow_untagged=args.allow_untagged)
        if placement and placement["satellites"]:
            # Multi-node: each satellite's golden copies and the jump VMs die on
            # their own hosts, then the engine template here.
            for sat in placement["satellites"]:
                sat_rec = record_of(placement, sat["name"])
                print(f"  Destroying satellite '{sat['name']}' golden set (slot "
                      f"{sat['slot']}) + jump vmid {sat['jump_vmid']}...")
                destroy_golden_set(sat_rec.node, engine_vmid, len(boxes), slot=sat["slot"],
                                   expect_tags=ownership_tags(competition, run_id, GOLDEN_TAG),
                                   allow_untagged=args.allow_untagged)
            destroy_jump_vms(placement, ownership_tags(competition, run_id),
                             allow_untagged=args.allow_untagged)
        print(f"  Destroying the engine template (vmid {engine_vmid} + 140)...")
        destroy_engine_template(node, engine_vmid,
                                expect_tags=ownership_tags(competition, run_id,
                                                           "engine-template"),
                                allow_untagged=args.allow_untagged)
        # A destroyed template's hash record is a loaded gun for the reuse path: the
        # next deploy would 'reuse' a hash with nothing behind it and clone from a
        # dead vmid. Drop the record alongside the templates.
        hashes_path = comp_dir / ".template-hashes.json"
        if hashes_path.exists():
            hashes_path.unlink()
            print("  Template hash record (.template-hashes.json) removed.")
    else:
        print("  Teams-only teardown — templates kept for THIS competition's next run only.")
        print("  Goldens never carry across competitions; tear down --full once the run's goal is met.")

    print(f"\nInfrastructure for '{competition}' destroyed.")
    print(f"Competition files preserved at competitions/{competition}/")


if __name__ == "__main__":
    main()
