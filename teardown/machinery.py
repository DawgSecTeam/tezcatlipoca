"""Teardown mechanics, moved from destroy_gate_ops / destroy_sweep_ops /
destroy_templates_ops (deleted 2026-10-12, teardown-redesign). The behavior is
the incident-hardened behavior; what changed is the vocabulary: every refusal
is a `Refusal` (saw / expected / remedy), and every destroy-shaped function
takes an `Ownership` instead of a loose tag set."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from constants import GOLDEN_TAG, SCORING_ENGINE_VMID
from golden_ops import destroy_golden_set
from jump_ops import destroy_jump_vms
from placement_record import record_of
from infra.client import proxmox_api, wait_for_proxmox_task
from infra.ownership import parse_vm_tags
from engine_template_ops import destroy_engine_template
from utils import run_terraform
from infra.ownership import Ownership, Refusal

# Concurrent stop/delete worker count for teardown sweeps. Deletes are
# metadata-light on ZFS (the documented datastore-saturation hazard belongs to
# bulk clone writes, not deletes); 4 keeps even a shared node comfortable.
TEARDOWN_WORKERS = 4

# Per-attempt wall-clock bound for `terraform destroy`. The documented hang mode
# is a Windows DC whose guest agent is down holding the qm lock mid-destroy
# (pfsense-rvb: 6m+ "Still destroying"). Every attempt is bounded and a timeout
# is just another failed attempt, so control reaches the recovery steps.
DESTROY_ATTEMPT_TIMEOUT_S = 1800


# a frozen competition's templates are the verified artifacts the event runs on: a full teardown needs --end-of-competition
def refuse_frozen_full_teardown(competition, frozen, full, end_of_competition):
    if full and frozen and not end_of_competition:
        raise Refusal(
            saw=f"competition '{competition}' is FROZEN (frozen_at {frozen.get('frozen_at')})",
            expected="end-of-competition intent before destroying its verified templates",
            remedy="re-run with --end-of-competition if the event is genuinely over",
            detail=f"'{competition}' is FROZEN (frozen_at {frozen.get('frozen_at')}). "
                   f"A full teardown would destroy the verified templates mid-competition. "
                   f"Re-run with --end-of-competition if the event is genuinely over.")


# refuses when ownership cannot be proven: no run id in the state, or the env targets a different endpoint than the deploy used
def load_ownership(comp_dir):
    deployed_state = {}
    state_path = Path(comp_dir) / ".deploy_state.json"
    if state_path.exists():
        try:
            deployed_state = json.loads(state_path.read_text())
        except (ValueError, OSError):
            deployed_state = {}
    run_id = deployed_state.get("run_id") or ""
    if not run_id:
        raise Refusal(
            saw=f"{state_path} with no run id",
            expected="the deploy's run-<id> anchor stamped by every deploy since 0.2.0",
            remedy="remove the range by hand on the node (a state without a run id "
                   "predates run ownership and nothing may destroy it)",
            detail=f"{state_path} carries no run id, so ownership of this competition's VMs "
                   f"cannot be proven and nothing will be destroyed. Every deploy since 0.2.0 "
                   f"stamps one; a range without it predates 0.2.0 — remove it by hand on "
                   f"the node.")
    deployed = (deployed_state.get("deployed_endpoint") or "").rstrip("/")
    current = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
    if deployed and current and deployed != current:
        raise Refusal(
            saw=f"deploy ran against {deployed}, the loaded env targets {current}",
            expected="TF_VAR_* pointed at the deployment's host",
            remedy="load the same env the deploy ran with and re-run",
            detail=f"this competition was deployed against {deployed}, but the "
                   f"loaded env targets {current}. Point TF_VAR_* at the deployment's host "
                   f"(same overrides create-competition ran with) and re-run.")
    return deployed_state, Ownership(Path(comp_dir).name, run_id)


# destroying against an empty state 'succeeds' and removes nothing — refuse when a comp deployed past terraform has no state file
def require_terraform_state(comp_dir, deployed_state):
    if int(deployed_state.get("last_phase") or 0) < 2:
        return
    state_file = Path(comp_dir) / "terraform" / "terraform.tfstate"
    if state_file.exists():
        return
    raise Refusal(
        saw=f"no {state_file} for a comp deployed to phase {deployed_state.get('last_phase')}",
        expected="the per-comp terraform state the destroy re-enters",
        remedy="restore/import the state file, or tear the range down by the recorded "
               "run id's tags on the node",
        detail=f"{state_file} is missing, but this competition was deployed (state reached "
               f"phase {deployed_state.get('last_phase')}, run id "
               f"'{deployed_state.get('run_id')}'). Destroying against an empty state would "
               f"remove nothing. Reconstruct the state file (restore it from backup, or "
               f"`terraform import` the run's VMs), or tear the range down by the recorded "
               f"run id's tags on the node. Nothing was destroyed.")


# hard-stop before destroy: the bpg graceful path hangs on an agent-down DC and holds the qm lock (pfsense-rvb: 6m+ 'Still destroying'); forced stop + remove is the operator call (2026-09-30); ownership gates every stop — the name pattern is shared by every worktree running the same competition ID
def pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=None,
                           ownership=None):
    windows = [b["name"] for b in boxes]  # all clones hard-stop, not just Windows
    if not windows:
        return
    expect_tags = set(ownership.tags) if ownership is not None else None
    live_by_node = {}

    targets = []
    for team_key, team in teams.items():
        node = (team_nodes or {}).get(team_key, default_node)
        if node not in live_by_node:
            try:
                live_by_node[node] = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
            except Exception as e:
                print(f"  WARNING: could not list VMs on {node} — skipping the "
                      f"pre-stop: {e}")
                live_by_node[node] = []
        for v in live_by_node[node]:
            vm_name = v.get("name") or ""
            # team 1 is named `team1-<box>` (terraform main.tf / targets.enumerate_targets)
            prefix = team_key if team_key == "team1" else team["identifier"]
            if vm_name not in {f"{prefix}-{box}" for box in windows}:
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


# the lock is only real while a terraform process is alive; a SIGKILLed deploy/destroy leaves it behind — remove it when none is running
def clear_stale_state_lock(tf_cwd):
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


# full ownership tag set only: partial overlap does not count (loadtest-2026-09-30), and a same-comp VM with a DIFFERENT run id belongs to another worktree's run (2026-10-02 near-miss)
def sweep_tagged_leftovers(nodes, ownership):
    comp_tags = set(ownership.tags)
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


# resumable by construction: each failure gets stale-lock recovery plus a run-id-scoped sweep, then a retry; a per-attempt timeout is just another failed attempt so control reaches the recovery steps (utils.run_terraform raises TimeoutExpired even with check=False)
def destroy_with_recovery(env, tf_cwd, nodes, ownership):
    for attempt in (1, 2, 3, 4):
        if attempt > 1:
            clear_stale_state_lock(tf_cwd)
            sweep_tagged_leftovers(nodes, ownership)
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


def engine_vmid_from_state(deployed_state):
    engine_vmid = deployed_state.get("scoring_vm_id") or SCORING_ENGINE_VMID
    try:
        return int(engine_vmid)
    except (TypeError, ValueError):
        return SCORING_ENGINE_VMID


# templates are the linked clones' base disks and are not in terraform state — they die only after terraform destroy removed every clone; returns the vmids skipped as foreign
def teardown_templates(comp_dir, deployed_state, ownership, boxes, placement, full):
    skipped = []
    engine_vmid = engine_vmid_from_state(deployed_state)
    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    if not full:
        print("  Teams-only teardown — templates kept for THIS competition's next run only.")
        print("  Goldens never carry across competitions; run a full teardown (no --teams-only) once the "
              "run's goal is met.")
        return skipped
    print(f"  Destroying golden templates (engine vmid {engine_vmid} + 150 + i)...")
    skipped += destroy_golden_set(node, engine_vmid, len(boxes), slot=0,
                                  expect_tags=ownership.with_role(GOLDEN_TAG).tags)
    if placement and placement["satellites"]:
        # Multi-node: each satellite's golden copies and the jump VMs die on
        # their own hosts, then the engine template here.
        for sat in placement["satellites"]:
            sat_rec = record_of(placement, sat["name"])
            print(f"  Destroying satellite '{sat['name']}' golden set (slot "
                  f"{sat['slot']}) + jump vmid {sat['jump_vmid']}...")
            skipped += destroy_golden_set(sat_rec.node, engine_vmid, len(boxes),
                                          slot=sat["slot"],
                                          expect_tags=ownership.with_role(GOLDEN_TAG).tags)
        destroy_jump_vms(placement, set(ownership.tags))
    print(f"  Destroying the engine template (vmid {engine_vmid} + 140)...")
    try:
        destroy_engine_template(node, engine_vmid,
                                expect_tags=ownership.with_role("engine-template").tags)
    except Refusal as e:
        skipped.append(str(e))
    # A destroyed template's hash record is a loaded gun for the reuse path: the
    # next deploy would 'reuse' a hash with nothing behind it and clone from a
    # dead vmid. Drop the record alongside the templates.
    hashes_path = Path(comp_dir) / ".template-hashes.json"
    if hashes_path.exists():
        hashes_path.unlink()
        print("  Template hash record (.template-hashes.json) removed.")
    return skipped
