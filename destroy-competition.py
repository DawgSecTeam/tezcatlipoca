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

from constants import GOLDEN_TAG, SCORING_ENGINE_VMID
from golden_ops import destroy_golden_set
from jump_ops import destroy_jump_vms
from nodes_ops import activate_placement, read_placement, record_of
from range_ops import proxmox_api, wait_for_proxmox_task
from template_ops import destroy_engine_template, frozen_state
from utils import load_compfile, pick_competition, run_terraform

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)

# Concurrent stop/delete worker count for teardown sweeps. Deletes are
# metadata-light on ZFS (the documented datastore-saturation hazard belongs to
# bulk clone writes, not deletes); 4 keeps even a shared node comfortable.
TEARDOWN_WORKERS = 4


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
    for entry in cloned_vms.values():
        n = _entry_node(entry, default_node)
        if n not in live_by_node:
            vmid = entry["vmid"] if isinstance(entry, dict) else entry
            try:
                live_by_node[n] = {
                    int(v["vmid"]): (v.get("name") or "")
                    for v in proxmox_api("GET", f"/nodes/{n}/qemu")["data"]
                }
            except Exception as e:
                print(f"  WARNING: could not list VMs on {n} — purging without existence "
                      f"checks: {e}")
                live_by_node[n] = None

    print(f"  Destroying {len(cloned_vms)} cloned VM(s) before terraform destroy "
          f"({TEARDOWN_WORKERS} workers)...")

    def _destroy_clone(item):
        vm_key, entry = item
        vmid = entry["vmid"] if isinstance(entry, dict) else entry
        node = _entry_node(entry, default_node)
        live = live_by_node.get(node)
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


def pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=None):
    """Hard-stop Windows team clones before terraform destroy. The bpg provider
    issues a graceful shutdown with a long timeout; a DC whose guest agent is
    down never complies and holds the qm lock, hanging the whole destroy
    (pfsense-rvb: 6m+ 'Still destroying', qm unlock/stop timing out behind it).
    A hard API stop needs no agent, and an already-stopped box skips the
    graceful path entirely. Clones already stopped/deleted by
    destroy_cloned_vms are simply not running here. team_nodes (multi-node)
    routes each team's sweep to its hosting node."""
    windows = [b["name"] for b in boxes if "win" in (b.get("template") or "").lower()]
    if not windows:
        return
    live_by_node = {}

    def _live(node):
        if node not in live_by_node:
            try:
                live_by_node[node] = {
                    v.get("name"): int(v["vmid"])
                    for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
            except Exception as e:
                print(f"  WARNING: could not list VMs on {node} — skipping the Windows "
                      f"pre-stop: {e}")
                live_by_node[node] = {}
        return live_by_node[node]

    targets = []
    for team_key, team in teams.items():
        node = (team_nodes or {}).get(team_key, default_node)
        live = _live(node)
        for box in windows:
            vm_name = f"{team['identifier']}-{box}"
            vmid = live.get(vm_name)
            if vmid is not None:
                targets.append((vm_name, vmid, node))

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


def sweep_tagged_leftovers(nodes, competition):
    """Completeness pass for a failed/interrupted teardown: destroy every VM still
    carrying this competition's FULL tag set (`tezcatlipoca` AND `comp-<name>`) —
    e.g. clones a killed deploy left out of terraform state. Foreign VMs are never
    candidates: partial tag overlap does not count (loadtest-2026-09-30). Multi-node:
    every host in `nodes` is swept."""
    comp_tags = {"tezcatlipoca", f"comp-{competition}"}
    ours = []
    for node in nodes:
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        except Exception as e:
            print(f"  WARNING: leftover sweep skipped on {node} — listing failed: {e}")
            continue
        ours += [dict(v, _node=node) for v in vms
                 if comp_tags <= {t.strip() for t in str(v.get("tags") or "")
                                  .replace(";", ",").split(",") if t.strip()}]
    if not ours:
        return
    print(f"  Leftover sweep: {len(ours)} VM(s) still tagged comp-{competition} — "
          f"destroying ({TEARDOWN_WORKERS} workers)...")

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


def report_remaining(nodes, competition, teams):
    """Final accounting after every recovery attempt failed: exactly what is still
    standing (per node), so a human decides — nothing is force-deleted here."""
    comp_tag = f"comp-{competition}"
    left_total = 0
    for node in nodes:
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
            left = [v for v in vms if comp_tag in str(v.get("tags") or "")]
            for v in left:
                print(f"    still standing on {node}: {v.get('name')} (vmid {v['vmid']})")
            left_total += len(left)
            if not left:
                print(f"    no competition-tagged VMs remain on {node}")
        except Exception as e:
            print(f"    WARNING: could not list remaining VMs on {node}: {e}")
        try:
            idents = {str(t["identifier"]) for t in teams.values()}
            nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
            bridges = [n["iface"] for n in nets
                       if n.get("type") == "bridge"
                       and n["iface"] in {f"vmbr{i}" for i in idents}]
            for b in bridges:
                print(f"    bridge still present on {node}: {b} (terraform state "
                      f"should own it — resolve the state, then re-run)")
        except Exception as e:
            print(f"    WARNING: could not list bridges on {node}: {e}")


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
    # The deploy's stale-state guard, mirrored: tearing down against a DIFFERENT
    # host than the one recorded in state makes terraform reconcile foreign
    # resources (live-found 2026-09-25: a realm-deployed comp destroyed with the
    # repo .env loaded the primary's vars and died on "required variable" noise —
    # the next host could be less lucky).
    state_path = comp_dir / ".deploy_state.json"
    if state_path.exists():
        try:
            deployed = (json.loads(state_path.read_text()).get("deployed_endpoint") or "").rstrip("/")
        except (ValueError, OSError):
            deployed = ""
        current = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
        if deployed and current and deployed != current:
            raise SystemExit(
                f"  ERROR: this competition was deployed against {deployed}, but the "
                f"loaded env targets {current}. Point TF_VAR_* at the deployment's host "
                f"(same overrides create-competition ran with) and re-run.")
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

    cloned_vms_path = comp_dir / "cloned_vms.json"
    if cloned_vms_path.exists():
        destroy_cloned_vms(cloned_vms_path, default_node)
    pre_stop_windows_boxes(teams, boxes, default_node, team_nodes=team_nodes)

    # Destroy from this competition's own per-comp state dir when it exists (multi-tenant
    # deploys), so we tear down only this comp's engine/boxes/bridges; fall back to the
    # legacy shared terraform/ dir for competitions deployed before per-comp isolation.
    per_comp_tf = comp_dir / "terraform"
    tf_cwd = str(per_comp_tf) if (per_comp_tf / "terraform.tfstate").exists() else "terraform"

    print(f"\nRunning terraform destroy for '{name}' (state: {tf_cwd})...")
    # Teardown is resumable: every pass makes progress, and each failure gets
    # (a) stale state-lock recovery (a SIGKILLed deploy leaves the lock behind),
    # (b) a tag-scoped sweep of clones a killed deploy left out of terraform state,
    # then a retry — until terraform completes or nothing of ours is left standing.
    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    destroyed = False
    for attempt in (1, 2, 3, 4):
        if attempt > 1:
            clear_stale_state_lock(tf_cwd)
            sweep_tagged_leftovers(placement_nodes, competition)
        proc = run_terraform(
            ["destroy", "-parallelism=4", "-auto-approve"],
            cwd=tf_cwd,
            env=env,
            check=False,
        )
        if proc.returncode == 0:
            destroyed = True
            break
        print(f"  terraform destroy failed (attempt {attempt}/4, partial progress kept)...")
    if not destroyed:
        report_remaining(placement_nodes, competition, teams)
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
    engine_vmid = SCORING_ENGINE_VMID
    state_path = comp_dir / ".deploy_state.json"
    if state_path.exists():
        try:
            engine_vmid = int(json.loads(state_path.read_text()).get("scoring_vm_id")
                              or SCORING_ENGINE_VMID)
        except (ValueError, OSError):
            pass
    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    if args.full:
        print(f"  Destroying golden templates (engine vmid {engine_vmid} + 150 + i)...")
        destroy_golden_set(node, engine_vmid, len(boxes), slot=0,
                           expect_tags={"tezcatlipoca", GOLDEN_TAG, f"comp-{competition}"})
        if placement and placement["satellites"]:
            # Multi-node: each satellite's golden copies and the jump VMs die on
            # their own hosts, then the engine template here.
            for sat in placement["satellites"]:
                sat_rec = record_of(placement, sat["name"])
                print(f"  Destroying satellite '{sat['name']}' golden set (slot "
                      f"{sat['slot']}) + jump vmid {sat['jump_vmid']}...")
                destroy_golden_set(sat_rec.node, engine_vmid, len(boxes), slot=sat["slot"],
                                   expect_tags={"tezcatlipoca", GOLDEN_TAG,
                                                f"comp-{competition}"})
            destroy_jump_vms(placement, {"tezcatlipoca", f"comp-{competition}"})
        print(f"  Destroying the engine template (vmid {engine_vmid} + 140)...")
        destroy_engine_template(node, engine_vmid,
                                expect_tags={"tezcatlipoca", f"comp-{competition}",
                                             "engine-template"})
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
