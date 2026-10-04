"""Teardown mechanics: pre-stop, bounded terraform destroy with recovery, tagged-leftover sweep."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from constants import ownership_tags
from range_ops import parse_vm_tags, proxmox_api, wait_for_proxmox_task
from utils import run_terraform


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


def pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=None, expect_tags=None):
    """Hard-stop EVERY team clone before terraform destroy. The bpg provider issues
    a graceful shutdown with a long timeout: a DC whose guest agent is down never
    complies and holds the qm lock, hanging the whole destroy (pfsense-rvb: 6m+
    'Still destroying', qm unlock/stop timing out behind it) — and even a well-
    behaved Linux box burns minutes in ACPI shutdown it doesn't need, since the
    disk is about to be destroyed (operator call 2026-09-30: forced stop + remove,
    not clean shutdown). A hard API stop needs no agent; an already-stopped box
    skips the graceful path entirely. Clones already stopped or deleted are
    simply not running here. team_nodes (multi-node)
    routes each team's sweep to its hosting node.

    expect_tags (the full ownership set, comp tag + run tag) gates every stop: the
    name pattern `<identifier>-<box>` is shared by every worktree running the same
    competition ID, so a name match alone would hard-stop ANOTHER run's boxes.
    No run id (state from before this deploy ran) passes the comp tag set alone."""
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


def sweep_tagged_leftovers(nodes, competition, run_id):
    """Completeness pass for a failed/interrupted teardown: destroy every VM still
    carrying this deploy's FULL ownership tag set (`tezcatlipoca` AND `comp-<name>`
    AND the deploy's `run-<id>` tag) — e.g. clones a killed deploy left out of
    terraform state. Foreign VMs are never candidates: partial tag overlap does not
    count (loadtest-2026-09-30), and a same-comp VM tagged with a DIFFERENT run id
    belongs to another worktree's run — equally untouchable (2026-10-02 near-miss).

    run_id comes from .deploy_state.json (main() refuses to run without one).
    Multi-node: every host in `nodes` is swept."""
    comp_tags = ownership_tags(competition, run_id)
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


def report_remaining(nodes, competition, teams, run_id):
    """Final accounting after every recovery attempt failed: exactly what is still
    standing (per node), so a human decides — nothing is force-deleted here. Every
    competition-tagged VM is classified: OURS (this deploy's run tag), same comp
    DIFFERENT run (another worktree's — never touched by this teardown), or
    carrying no run tag at all."""
    comp_tag = f"comp-{competition}"
    left_total = 0
    for node in nodes:
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
            left = [v for v in vms if comp_tag in str(v.get("tags") or "")]
            for v in left:
                tags = parse_vm_tags(v.get("tags"))
                if run_id in tags:
                    kind = "OURS — still standing, teardown failed on it"
                elif any(t.startswith("run-") for t in tags):
                    kind = ("same competition, DIFFERENT run — NOT touched by this "
                            "teardown (another worktree's?)")
                else:
                    kind = "competition-tagged, carries no run tag"
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


def destroy_with_recovery(env, tf_cwd, placement_nodes, competition, run_id):
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
            sweep_tagged_leftovers(placement_nodes, competition, run_id=run_id)
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
