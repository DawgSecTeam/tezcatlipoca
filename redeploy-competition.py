# Author: Hamza

"""Redeploy a SUBSET of a live competition's boxes — one team, one box type, or any
combination — without tearing down the range.

create-competition.py is all-or-nothing: phase 1 destroys every team's boxes, the scoring
engine and every bridge, and even a --from-phase resume operates on the whole competition. That
is the wrong tool when a single team's box breaks an hour into an event.

This one resolves a filtered set of (team, box) targets and puts just those back, cheapest
option first:

  rollback-ready   roll back to the `tz-ready` snapshot — the exact disk the competition
  (default)        started on. Seconds to a couple of minutes. Nothing is rebuilt.
  rollback-base    roll back to `tz-base` (booted + networked, pre-Nakon), then re-run Nakon
                   and service hardening for those machines. Use when tz-ready is also bad.
  reconfigure      no rollback at all: re-run DNS/auth/Nakon/hardening against the live boxes.
                   Use when a service died but the box is otherwise the team's to keep.
  rebuild          the VM is gone or won't boot: recreate it from its Packer template, then do
                   everything rollback-base does, and re-take both snapshots.

Both snapshots are taken by create-competition.py during the normal deploy (phases 5 and 6) —
a range deployed before snapshotting existed, or on a datastore that can't snapshot, only has
`reconfigure` and `rebuild` available. The tool says so rather than failing obscurely.

WARNING: rolling a box back mid-competition discards everything the defending team did to it.
It is a reset to a known-good point, not a repair. Every destructive mode prints the full
target list and asks for confirmation unless --yes.
"""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from range_ops import (
    SNAP_BASE,
    SNAP_READY,
    describe_target,
    destroy_vm_if_exists,
    enumerate_targets,
    list_snapshots,
    proxmox_api,
    rollback_snapshot,
    snapshot_support_hint,
    start_vm,
    take_snapshot,
    wait_for_proxmox_task,
)
from utils import load_compfile, pick_competition

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _load_driver():
    """Import create-competition.py as a module.

    Its filename has a hyphen, so it can't be imported with a plain `import` — hence the
    importlib shim. Doing this rather than duplicating the SSH/Nakon chain is deliberate: the
    DNS fix, `ubuntu` auth setup, Nakon invocation and service hardening must stay bit-identical
    to what the full deploy does, or a redeployed box drifts from its neighbours and starts
    scoring differently. Safe to import: that module's top level only calls load_dotenv() and
    silences urllib3 warnings, and its main() is behind `if __name__ == "__main__"`.
    """
    path = Path(__file__).parent / "create-competition.py"
    spec = importlib.util.spec_from_file_location("create_competition", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["create_competition"] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()


### Selection

def parse_team_selector(raw, teams):
    """Resolve a comma-separated team selector against the competition's teams.json.

    Accepts whatever an operator is most likely to have in front of them mid-event: the team key
    (`team2`), the bare number (`2`), or the subnet identifier they can read off a box's IP
    (`102`, from 192.168.102.x). Raises SystemExit on anything that matches no team — silently
    redeploying nothing, or worse the wrong team, is the failure mode to avoid here.
    """
    by_key = {k.lower(): k for k in teams}
    by_ident = {str(v["identifier"]): k for k, v in teams.items()}
    by_number = {k.lower().removeprefix("team"): k for k in teams}

    selected = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        low = token.lower()
        match = by_key.get(low) or by_ident.get(low) or by_number.get(low)
        if match is None:
            raise SystemExit(
                f"  ERROR: no team matches '{token}'. This competition has: "
                + ", ".join(f"{k} (identifier {v['identifier']})" for k, v in teams.items())
            )
        if match not in selected:
            selected.append(match)
    return selected


def parse_box_selector(raw, boxes):
    known = {b["name"].lower(): b["name"] for b in boxes}
    selected = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        match = known.get(token.lower())
        if match is None:
            raise SystemExit(
                f"  ERROR: no box named '{token}'. This competition has: "
                + ", ".join(b["name"] for b in boxes)
            )
        if match not in selected:
            selected.append(match)
    return selected


def box_platform(box):
    """'linux' / 'windows' for a box, from its template name.

    Uses the driver's os_to_platform() — the same function generate_nakon_config() classifies
    with — so --platform can never disagree with what Nakon thinks it is deploying to.
    """
    return driver.os_to_platform(box.get("template", ""))


def select_targets(teams, boxes, args):
    """Full target list, narrowed by whichever filters were given (AND-combined)."""
    targets = enumerate_targets(teams, boxes)

    if args.teams:
        keep = set(parse_team_selector(args.teams, teams))
        targets = [t for t in targets if t["team_key"] in keep]
    if args.boxes:
        keep = set(parse_box_selector(args.boxes, boxes))
        targets = [t for t in targets if t["box_name"] in keep]
    if args.platform:
        want = args.platform.lower()
        targets = [t for t in targets if box_platform(t["box"]) == want]

    return targets


### Modes

def run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """The configure half of a redeploy: DNS, auth, Nakon (scoped), service hardening.

    Identical to what phases 5/6 do, except `nakon deploy --only` is scoped to exactly these
    machines. What gets applied to each one is fixed by the bundle, which was built from the
    full machine list — narrowing --only changes which boxes are connected to, never what is
    deployed to them.
    """
    key = Path(ctx["ssh_key_path"])
    scoring_user = os.environ["TF_VAR_vm_username"]
    scoring_ip = ctx["scoring_engine_ip"]

    driver.fix_dns_on_boxes(targets, ctx)
    driver.setup_ubuntu_auth(targets, ctx)

    # The boxes NAT out through the engine; a container restart there drops the rule, and
    # Nakon's first action on every machine is an apt-get.
    driver.ensure_nat_forwarding(ctx)

    machines = [t["machine"] for t in targets]
    print(f"  Running Nakon on {len(machines)} machine(s): {', '.join(machines)}")
    driver.run_nakon(
        key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
        only=machines,
        # Same per-machine budget the full deploy uses, scaled to this selection rather than
        # the whole range — see PER_MACHINE_NAKON_BUDGET in create-competition.py.
        timeout=max(2400, driver.PER_MACHINE_NAKON_BUDGET * len(machines)),
    )

    driver.fix_services_on_boxes(comp_dir, targets, ctx, box_creds=state.get("box_creds"))


def mode_rollback(targets, ctx, node, snapshot, comp_dir, state, nakon_config_path,
                  nakon_bundle, reconfigure):
    """Roll each target back to `snapshot`, then optionally re-run the configure chain."""
    restored = []
    for t in targets:
        print(f"  Rolling back {describe_target(t)} to '{snapshot}'...")
        try:
            rollback_snapshot(node, t["vmid"], snapshot)
            restored.append(t)
            print(f"    {t['vm_name']} restored")
        except Exception as e:
            # One bad box must not abandon the rest — mid-event, recovering three of four boxes
            # beats recovering none.
            print(f"  WARNING: rollback failed for {describe_target(t)}: {e}")

    if not restored:
        raise SystemExit("  ERROR: no box was rolled back successfully — nothing to do.")

    driver.wait_for_boxes_ssh(ctx, restored, timeout=300)

    if reconfigure:
        driver.wait_for_cloud_init(ctx, restored, timeout=240)
        run_nakon_and_harden(restored, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
        print(f"  Re-taking '{SNAP_READY}' for the recovered boxes...")
        for t in restored:
            take_snapshot(node, t["vmid"], SNAP_READY,
                          description="tezcatlipoca: as delivered (re-taken by redeploy)")

    return restored


def mode_reconfigure(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """No rollback — re-run the configure chain against the boxes as they are right now."""
    driver.wait_for_boxes_ssh(ctx, targets, timeout=300)
    run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    return targets


def template_vmid_for(box):
    """Resolve a box's template name to a vmid, the way main.tf's templates data source does."""
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    data = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    for vm in data:
        if (vm.get("name") == box["template"]
                and vm.get("template") == 1
                and "template" in (vm.get("tags") or "").split(";")
                and vm.get("vmid") != scoring_template_id):
            return vm["vmid"]
    raise SystemExit(
        f"  ERROR: no Proxmox template named '{box['template']}' (tagged 'template'). "
        f"Available: " + ", ".join(driver.list_proxmox_templates())
    )


def mode_rebuild(targets, ctx, node, comp_dir, state, nakon_config_path, nakon_bundle):
    """Recreate each target VM from its Packer template, then configure it from scratch.

    The last rung: for a VM that has been deleted, or is so broken it won't boot far enough to
    roll back. Deliberately clones the box's own TEMPLATE rather than team1's live box the way
    phase 6 does — mid-competition team1's box carries whatever team1's defenders have done to
    it, and handing another team a copy of that is neither fair nor reproducible. Cloning the
    template reproduces what Terraform built at deploy time instead.

    Mirrors main.tf's `team_box` resource: full clone, cloud-init ipconfig0/net0, the `ubuntu`
    account with this competition's box_password and the range SSH key, and the box's cpu/memory
    from boxes.json.
    """
    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: .deploy_state.json has no box_password — can't recreate a box with the "
            "login the rest of the range uses. Rebuild is unavailable for this competition."
        )
    ssh_public_key = os.environ["TF_VAR_ssh_public_key"]

    rebuilt = []
    for t in targets:
        box = t["box"]
        src_vmid = template_vmid_for(box)
        print(f"  Rebuilding {describe_target(t)} from template "
              f"'{box['template']}' (vmid {src_vmid})...")

        destroy_vm_if_exists(node, t["vmid"])

        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src_vmid}/clone", data={
            "newid": t["vmid"],
            "name": t["vm_name"],
            "full": 1,
        })["data"]
        wait_for_proxmox_task(node, upid)

        config = {
            "ipconfig0": f"ip={t['ip']}/24,gw=192.168.{t['identifier']}.1",
            "net0": f"virtio,bridge=vmbr{t['identifier']}",
            "ciuser": "ubuntu",
            "cipassword": box_password,
            "sshkeys": quote_sshkeys(ssh_public_key),
            "cores": box["cpu"],
            "memory": box["memory_mb"],
        }
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data=config)

        # main.tf only emits a disk block when disk_gb is set; a null means "keep the
        # template's own disk", and forcing a size there would try to SHRINK a real template
        # disk, which Proxmox rejects. Same rule here.
        if box.get("disk_gb"):
            proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/resize", data={
                "disk": "scsi0", "size": f"{box['disk_gb']}G",
            })

        start_vm(node, t["vmid"])
        rebuilt.append(t)

        if t["team_key"] == "team1":
            print(f"    NOTE: {t['vm_name']} is a Terraform-managed resource. It was recreated "
                  f"outside Terraform, so the next `terraform apply` will see drift and want to "
                  f"replace it. Fine mid-event; re-import or accept the replacement afterwards.")

    driver.wait_for_boxes_ssh(ctx, rebuilt, timeout=600)
    driver.wait_for_cloud_init(ctx, rebuilt, timeout=300)
    driver.fix_dns_on_boxes(rebuilt, ctx)
    driver.setup_ubuntu_auth(rebuilt, ctx)

    print(f"  Snapshotting rebuilt boxes as '{SNAP_BASE}'...")
    for t in rebuilt:
        take_snapshot(node, t["vmid"], SNAP_BASE,
                      description="tezcatlipoca: rebuilt from template, pre-Nakon")

    run_nakon_and_harden(rebuilt, ctx, comp_dir, state, nakon_config_path, nakon_bundle)

    print(f"  Snapshotting rebuilt boxes as '{SNAP_READY}'...")
    for t in rebuilt:
        take_snapshot(node, t["vmid"], SNAP_READY,
                      description="tezcatlipoca: as delivered (rebuilt by redeploy)")
    return rebuilt


def quote_sshkeys(public_key):
    """Proxmox's `sshkeys` config param wants the key URL-encoded."""
    from urllib.parse import quote
    return quote(public_key.strip(), safe="")


### CLI

def main():
    parser = argparse.ArgumentParser(
        description="Redeploy a subset of a live competition's boxes without tearing down the "
                    "range. Selection flags combine with AND; with none given, every box in "
                    "the competition is selected.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # one team's boxes, back to the state the competition started in
  redeploy-competition.py --competition cde-2026 --teams 3

  # one box for one team
  redeploy-competition.py --competition cde-2026 --teams 3 --boxes web01

  # one team's linux boxes, rebuilt from tz-base and re-planted by Nakon
  redeploy-competition.py --competition cde-2026 --teams 3 --platform linux \\
      --mode rollback-base

  # see what would be touched, change nothing
  redeploy-competition.py --competition cde-2026 --teams 3 --dry-run
""",
    )
    parser.add_argument("--competition", help="Competition ID (competitions/<id>). Omit for a "
                                              "menu of deployed competitions.")
    parser.add_argument("--teams", help="Comma-separated teams: 'team2', '2' or the subnet "
                                        "identifier '102' all select the same team.")
    parser.add_argument("--boxes", help="Comma-separated box names from boxes.json, e.g. "
                                        "'web01,db01'.")
    parser.add_argument("--platform", choices=["linux", "windows"],
                        help="Only boxes whose template is this platform.")
    parser.add_argument("--mode", default="rollback-ready",
                        choices=["rollback-ready", "rollback-base", "reconfigure", "rebuild"],
                        help="What to do to the selected boxes (default: rollback-ready).")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Print the resolved targets and their snapshots, then exit.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    # Competition selection
    comp_name = args.competition
    if not comp_name:
        candidates = [
            p.name for p in sorted(Path("competitions").iterdir())
            if p.is_dir() and (p / "Compfile").exists() and (p / "teams.json").exists()
        ]
        if not candidates:
            raise SystemExit("No deployed competitions found (need Compfile + teams.json).")
        comp_name = pick_competition(candidates, label="deployed",
                                     action="Select a competition to redeploy from")
        if comp_name is None:
            raise SystemExit("Quitting.")

    comp_dir = Path("competitions") / comp_name
    if not comp_dir.is_dir():
        raise SystemExit(f"  ERROR: no such competition: {comp_dir}")

    name, _scenario, difficulty = load_compfile(comp_dir / "Compfile")
    teams_path = comp_dir / "teams.json"
    boxes_path = comp_dir / "boxes.json"
    state_path = comp_dir / ".deploy_state.json"
    for p in (teams_path, boxes_path):
        if not p.exists():
            raise SystemExit(f"  ERROR: {p} is missing — this competition was never deployed.")

    teams = json.loads(teams_path.read_text())
    boxes = json.loads(boxes_path.read_text())
    # Secrets (box_password, box_creds) have to be the originals: the credlist accounts this
    # recreates must match what push_event_conf() wrote to Quotient's linux.credlist, or the
    # recovered box scores down on every auth-based check even though it is healthy.
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    targets = select_targets(teams, boxes, args)
    if not targets:
        raise SystemExit("  No boxes matched the given filters — nothing to do.")

    node = os.environ["TF_VAR_proxmox_node"]

    print(f"\n{'='*64}")
    print(f"  Redeploy — {name} ({comp_name})")
    print(f"{'='*64}")
    print(f"  Mode: {args.mode}")
    print(f"  {len(targets)} of {len(teams) * len(boxes)} box(es) selected:\n")
    for t in targets:
        snaps = list_snapshots(node, t["vmid"])
        have = ", ".join(sorted(snaps)) if snaps else "none"
        print(f"    {describe_target(t)}  [snapshots: {have}]")
    print()

    if args.dry_run:
        print("  --dry-run: nothing was changed.")
        return

    # Snapshot availability gate — fail before touching anything, with the fallback spelled out.
    needed = {"rollback-ready": SNAP_READY, "rollback-base": SNAP_BASE}.get(args.mode)
    if needed:
        missing = [t for t in targets if needed not in list_snapshots(node, t["vmid"])]
        if missing:
            print(f"  ERROR: {len(missing)} selected box(es) have no '{needed}' snapshot:")
            for t in missing:
                print(f"    {describe_target(t)}")
            print("\n  " + snapshot_support_hint(node, missing[0]["vmid"]))
            raise SystemExit(1)

    if not args.yes:
        if args.mode != "reconfigure":
            print("  This DISCARDS everything the defending team(s) have done to these boxes.")
        answer = input(f"  Redeploy {len(targets)} box(es) in mode '{args.mode}'? [y/N] ").strip()
        if answer.lower() not in ("y", "yes"):
            print("  Cancelled — nothing was changed.")
            return

    ctx = driver.read_terraform_ctx()

    # The Nakon config + bundle are only needed by the modes that actually re-run Nakon.
    # nakon-config.json is regenerated deterministically if absent: box_services.json and
    # box_vulns.json are pinned by the time a competition is deployed, so generate_nakon_config()
    # takes its "pinned" branch and reproduces the same machine list rather than re-randomising.
    nakon_config_path = nakon_bundle = None
    if args.mode in ("rollback-base", "reconfigure", "rebuild"):
        nakon_config_path = comp_dir / "nakon-config.json"
        if not nakon_config_path.exists():
            print("  nakon-config.json missing — regenerating from the pinned service/vuln sets...")
            nakon_config_path = driver.generate_nakon_config(
                teams, boxes, difficulty, comp_dir, state["box_password"]
            )
        nakon_bundle = driver.build_nakon_bundle(nakon_config_path)

    if args.mode == "rollback-ready":
        done = mode_rollback(targets, ctx, node, SNAP_READY, comp_dir, state,
                             nakon_config_path, nakon_bundle, reconfigure=False)
    elif args.mode == "rollback-base":
        done = mode_rollback(targets, ctx, node, SNAP_BASE, comp_dir, state,
                             nakon_config_path, nakon_bundle, reconfigure=True)
    elif args.mode == "reconfigure":
        done = mode_reconfigure(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    else:
        done = mode_rebuild(targets, ctx, node, comp_dir, state, nakon_config_path, nakon_bundle)

    print(f"\n{'='*64}")
    print(f"  Redeployed {len(done)} box(es) in mode '{args.mode}'")
    print(f"{'='*64}")
    for t in done:
        print(f"    {describe_target(t)}")
    print(f"\n  Verify with: python3 verify-competition.py competitions/{comp_name}")
    print(f"  Scoreboard:  http://{ctx['scoring_engine_ip']}")


if __name__ == "__main__":
    main()
