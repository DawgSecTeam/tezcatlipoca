"""Teardown mechanics: every refusal is a Refusal and every destroy-shaped call takes an Ownership."""

from concurrent.futures import ThreadPoolExecutor

from common.constants import GOLDEN_TAG, SCORING_ENGINE_VMID
from estate import state as estate_state
from golden.golden_ops import destroy_golden_set
from net.jump_ops import destroy_jump_vms
from multinode.placement_record import record_of
from infra.client import proxmox_api, wait_for_proxmox_task
from infra.ownership import parse_vm_tags
from engine.engine_template_ops import destroy_engine_template
from engine.template_freeze import forget_template_hashes
from infra.ownership import Ownership, Refusal, ownership_identity
from pathlib import Path
from estate import config

# Deletes are metadata-light on ZFS; 4 workers keeps even a shared node comfortable.
TEARDOWN_WORKERS = 4


def refuse_frozen_full_teardown(competition, frozen, full, end_of_competition):
    if full and frozen and not end_of_competition:
        raise Refusal(
            saw=f"competition '{competition}' is FROZEN (frozen_at {frozen.get('frozen_at')})",
            expected="end-of-competition intent before destroying its verified templates",
            remedy="re-run with --end-of-competition if the event is genuinely over",
            detail=f"'{competition}' is FROZEN (frozen_at {frozen.get('frozen_at')}). "
                   f"A full teardown would destroy the verified templates mid-competition. "
                   f"Re-run with --end-of-competition if the event is genuinely over.")


def load_ownership(comp_dir):
    state_path = estate_state.state_path(comp_dir)
    deployed_state = estate_state.load_flat(comp_dir)
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
    current = config.active().proxmox.endpoint.rstrip("/")
    if deployed and current and deployed != current:
        raise Refusal(
            saw=f"deploy ran against {deployed}, the loaded env targets {current}",
            expected="the loaded profile pointed at the deployment's host",
            remedy="load the same env the deploy ran with and re-run",
            detail=f"this competition was deployed against {deployed}, but the "
                   f"loaded env targets {current}. Point the profile at the deployment's host "
                   f"(same overrides create-competition ran with) and re-run.")
    return deployed_state, Ownership(
        ownership_identity(comp_dir, deployed_state.get("comp_name")), run_id)


# Hard-stop, never graceful: the API shutdown path hangs on an agent-down DC and holds the qm lock for minutes.
def pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=None,
                           ownership=None):
    windows = [b["name"] for b in boxes]
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
            # team 1 is named `team1-<box>` (targets.enumerate_targets)
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
            return (f"  WARNING: pre-stop of {vm_name} failed ({e}). If the destroy hangs on "
                    f"it: kill -9 $(cat /var/run/qemu-server/{vmid}.pid) on the node, "
                    f"then re-run.")

    with ThreadPoolExecutor(max_workers=TEARDOWN_WORKERS) as pool:
        for line in pool.map(_hard_stop, targets):
            print(line, flush=True)


# Full ownership set only: a same-comp VM with a different run id is another worktree's run.
def sweep_tagged_leftovers(nodes, ownership, keep_templates=False):
    comp_tags = set(ownership.tags)
    ours = []
    for node in nodes:
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        except Exception as e:
            print(f"  WARNING: leftover sweep skipped on {node} — listing failed: {e}")
            continue
        # A reused template carries THIS run's tags, so teams-only must exempt it by the PVE
        # `template` flag rather than by tag matching (caught live: an engine template was swept).
        ours += [dict(v, _node=node) for v in vms
                 if comp_tags <= parse_vm_tags(v.get("tags"))
                 and not (keep_templates and v.get("template"))]
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


_LINUX_LOG_CMD = (
    "echo '### journalctl'; journalctl --no-pager -n 2000 2>&1; "
    "for f in /var/log/auth.log /var/log/secure /var/log/syslog /var/log/messages; do "
    "[ -f \"$f\" ] && { echo \"### $f\"; tail -n 2000 \"$f\"; }; done; "
    "echo '### dmesg'; dmesg -T 2>&1")
_WINDOWS_LOG_PS = (
    "$ErrorActionPreference='SilentlyContinue'; "
    "foreach ($l in 'Security','System','Application') { \"### $l\"; "
    "Get-WinEvent -LogName $l -MaxEvents 500 | "
    "Format-List TimeCreated,Id,ProviderName,Message }")


def collect_box_logs(ctx, out_dir):
    """Last copy of every owned VM's logs: run before anything destructive; an unreadable box is recorded, never fatal."""
    from infra.guest_exec import guest_agent_exec_root, guest_agent_exec_windows
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    # An Alpine jump VM has no journalctl and its 180 s timeout would stall the whole
    # collection; the guest-agent channel is also independent, so collect concurrently.
    targets = [(node, vm) for node, vm in ctx.inventory.owned(ctx.ownership)
               if vm.get("status") == "running"
               and not str(vm.get("name") or "").startswith("jump-")]

    def _one(item):
        node, vm = item
        vmid = int(vm["vmid"])
        name = vm.get("name") or f"vm{vmid}"
        try:
            cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
            windows = str(cfg.get("ostype") or "").lower().startswith("win")
            if windows:
                rc, out, err = guest_agent_exec_windows(node, vmid, _WINDOWS_LOG_PS,
                                                        timeout=180)
            else:
                rc, out, err = guest_agent_exec_root(node, vmid, _LINUX_LOG_CMD,
                                                     timeout=180)
            text = (out or "") + (("\n### stderr\n" + err) if err else "")
            if rc != 0 and not out:
                text = f"log collection failed (rc {rc})\n{err or ''}"
        except Exception as exc:                             # noqa: BLE001 - recorded
            text = f"log collection failed: {type(exc).__name__}: {exc}"
        target = out_dir / f"{name}-{vmid}.log"
        target.write_text(text)
        print(f"  logs        : {name} (vmid {vmid}) -> {target.name}")
        return str(target)

    with ThreadPoolExecutor(max_workers=TEARDOWN_WORKERS) as pool:
        for path in pool.map(_one, targets):
            if path:
                written.append(path)
    return written


def engine_vmid_from_state(deployed_state):
    engine_vmid = deployed_state.get("scoring_vm_id") or SCORING_ENGINE_VMID
    try:
        return int(engine_vmid)
    except (TypeError, ValueError):
        return SCORING_ENGINE_VMID


# Templates are the clones' base disks: they can only go after every clone is destroyed.
def teardown_templates(comp_dir, deployed_state, ownership, boxes, placement, full):
    """Reclaim this competition's templates by vmid math; a message means "not this run's"."""
    skipped = []
    engine_vmid = engine_vmid_from_state(deployed_state)
    node = config.active().proxmox.node
    if not full:
        print("  Teams-only teardown — templates kept for THIS competition's next run only.")
        print("  Goldens never carry across competitions; run a full teardown (no --teams-only) once the "
              "run's goal is met.")
        return skipped
    print(f"  Destroying golden templates (engine vmid {engine_vmid} + 150 + i)...")
    skipped += destroy_golden_set(node, engine_vmid, len(boxes), slot=0,
                                  expect_tags=ownership.with_role(GOLDEN_TAG).tags)
    if placement and placement["satellites"]:
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
        skipped += destroy_engine_template(
            node, engine_vmid, expect_tags=ownership.with_role("engine-template").tags)
    except Refusal as e:
        skipped.append(str(e))
    # A stale hash record makes the next deploy clone from a dead vmid.
    forget_template_hashes(comp_dir)
    print("  Template hash record removed from the state document.")
    # The caller only reaches this fallback when the document records no template of its own,
    # so a kept template at the slot belongs to another run: it is that range's to reclaim, and
    # failing here stranded a range whose deploy died before adopting the cache (live: cde-2026).
    return skipped
