"""Teardown: destroy API-cloned VMs and `terraform destroy`."""

import json
import os
import sys
import time
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from constants import GOLDEN_TAG, SCORING_ENGINE_VMID
from golden_ops import destroy_golden_set
from range_ops import proxmox_api, wait_for_proxmox_task
from template_ops import destroy_engine_template, frozen_state
from utils import load_compfile, pick_competition, run_terraform

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)


def load_destroyable_competitions():
    return [
        p.name
        for p in sorted(Path("competitions").iterdir())
        if p.is_dir()
        and (p / "Compfile").exists()
        and (p / "teams.json").exists()
        and (p / "boxes.json").exists()
    ]


def destroy_cloned_vms(cloned_vms_path):
    cloned_vms = json.loads(cloned_vms_path.read_text())
    if not cloned_vms:
        return

    node = os.environ.get("TF_VAR_proxmox_node", "pve")

    # Only purge vmids that still exist AND whose VM name is obviously this
    # competition's (clone names look like '102-dc01'): the deterministic vmid
    # scheme collides across competitions, and cloned_vms.json can outlive the
    # range — purging bare vmids on a stale file would hit someone else's VM.
    live = {}
    try:
        for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]:
            live[int(v["vmid"])] = v.get("name") or ""
    except Exception as e:
        print(f"  WARNING: could not list VMs on {node} — purging without existence "
              f"checks: {e}")
        live = None

    print(f"  Destroying {len(cloned_vms)} cloned VM(s) before terraform destroy...")
    for vm_key, vmid in cloned_vms.items():
        if live is not None:
            name = live.get(int(vmid))
            if name is None:
                print(f"    Skipping {vm_key} (vmid {vmid}) — VM does not exist on {node}")
                continue
            if name != vm_key:
                print(f"    Skipping {vm_key} (vmid {vmid}) — VM exists as '{name}', which "
                      f"doesn't match this competition's clone name")
                continue
        try:
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
            wait_for_proxmox_task(node, upid)
        except Exception:
            pass
        try:
            upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}", params={"purge": 1})["data"]
            wait_for_proxmox_task(node, upid)
            print(f"    Deleted {vm_key} (vmid {vmid})")
        except Exception as e:
            print(f"    WARNING: could not delete {vm_key} (vmid {vmid}): {e}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Destroy a deployed competition. Default (teams-only) keeps the "
                    "competition's golden + engine templates for the next test run; "
                    "--full removes them too.")
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
    print("  This will run: terraform destroy -parallelism=1 -auto-approve")
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
        destroy_cloned_vms(cloned_vms_path)

    # Destroy from this competition's own per-comp state dir when it exists (multi-tenant
    # deploys), so we tear down only this comp's engine/boxes/bridges; fall back to the
    # legacy shared terraform/ dir for competitions deployed before per-comp isolation.
    per_comp_tf = comp_dir / "terraform"
    tf_cwd = str(per_comp_tf) if (per_comp_tf / "terraform.tfstate").exists() else "terraform"

    print(f"\nRunning terraform destroy for '{name}' (state: {tf_cwd})...")
    # One destroy pass can die halfway when resources were deleted out-of-band
    # (manually removed -fix templates/VMs, scrim-dress-2026-09-20 teardown):
    # every pass still makes progress on the remaining resources, so retry
    # once before giving up with the range half-torn-down.
    for attempt in (1, 2):
        proc = run_terraform(
            ["destroy", "-parallelism=1", "-auto-approve"],
            cwd=tf_cwd,
            env=env,
            check=False,
        )
        if proc.returncode == 0:
            break
        if attempt == 1:
            print("  terraform destroy failed — retrying once (partial progress is kept)...")
    else:
        sys.exit(f"ERROR: terraform destroy failed twice (rc={proc.returncode}); "
                 "inspect `terraform state list` under terraform/ and tear down the rest by hand.")

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
        destroy_golden_set(node, engine_vmid, len(boxes),
                           expect_tags={"tezcatlipoca", GOLDEN_TAG, f"comp-{competition}"})
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
        print("  Teams-only teardown — golden + engine templates kept for reuse.")

    print(f"\nInfrastructure for '{competition}' destroyed.")
    print(f"Competition files preserved at competitions/{competition}/")


if __name__ == "__main__":
    main()
