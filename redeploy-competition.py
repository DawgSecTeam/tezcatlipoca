
"""Redeploy a subset of a live competition's boxes (rollback-ready/base, reconfigure, rebuild)."""

import argparse
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from constants import WINDOWS_ADMIN_USER
from engine_ops import read_event_conf
from range_ops import (
    SNAP_BASE,
    SNAP_READY,
    describe_target,
    destroy_vm_if_exists,
    enumerate_targets,
    guest_agent_exec_root,
    guest_agent_exec_windows,
    list_snapshots,
    proxmox_api,
    rollback_snapshot,
    snapshot_support_hint,
    start_vm,
    take_snapshot,
    wait_for_proxmox_task,
)
from utils import load_compfile, load_users_config, pick_competition, valid_comp_name

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _load_driver():
    """Import create-competition.py as module (hyphen requires importlib)."""
    path = Path(__file__).parent / "create-competition.py"
    spec = importlib.util.spec_from_file_location("create_competition", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["create_competition"] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()



def parse_team_selector(raw, teams):
    """Parse team selector (team key, number, or subnet identifier)."""
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
    """Platform via driver's os_to_platform (must match nakon)."""
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



def run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """Configure half: DNS, auth, scoped nakon (--only), service hardening."""
    key = Path(ctx["ssh_key_path"])
    scoring_user = os.environ["TF_VAR_vm_username"]
    scoring_ip = ctx["scoring_engine_ip"]

    linux_targets = [t for t in targets if box_platform(t["box"]) == "linux"]
    driver.fix_dns_on_boxes(linux_targets, ctx)
    driver.setup_ubuntu_auth(linux_targets, ctx)

    driver.ensure_nat_forwarding(ctx)

    machines = [t["machine"] for t in targets]
    print(f"  Running Nakon on {len(machines)} machine(s): {', '.join(machines)}")
    # strict=False, mirroring deploy.py's phase-6 stance: these re-plants hit live
    # boxes mid-event, and one flaky/broken pin must not abort a repair sweep.
    failed = driver.run_nakon(
        key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
        only=machines,
        timeout=max(2400, driver.PER_MACHINE_NAKON_BUDGET * len(machines)),
        strict=False,
    )
    state["nakon_failed_steps"] = failed[:20]
    state_path = comp_dir / ".deploy_state.json"
    if state_path.exists():
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, state_path)
        os.chmod(state_path, 0o600)

    driver.fix_services_on_boxes(comp_dir, targets, ctx, box_creds=state.get("box_creds"))


def rerun_domain_configs(targets, ctx, comp_dir, state, nakon_config_path):
    """Re-run AD domain chain for affected teams (promote DC only if reset).

    Returns True when the boxes' domain state may be treated as settled — no
    domain semantics at all, or the chain re-ran clean — and False when the
    chain was supposed to run but could not. Callers must NOT re-take tz-ready
    on False: snapshotting then would bake a broken state in as 'as delivered'."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        return True
    roles = json.loads(roles_path.read_text())
    domain_targets = [t for t in targets if roles.get(t["box_name"])]
    if not domain_targets:
        return True

    box_password = state.get("box_password")
    if not box_password:
        print("  WARNING: .deploy_state.json has no box_password — cannot re-run domain "
              "configuration for the reset domain-role box(es). Rejoin by hand, or re-run "
              "create-competition.py --from-phase 6.")
        return False

    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    affected_teams = {t["team_key"] for t in domain_targets}

    print("  Domain-role box(es) were reset to a pre-domain state — re-running domain "
          "configuration for " + ", ".join(sorted(affected_teams)) + "...")
    for team_key in sorted(affected_teams):
        dc_reset = any(
            roles.get(t["box_name"]) == "dc" and t["team_key"] == team_key
            for t in domain_targets
        )
        if dc_reset:
            stale_marker = comp_dir / f".nakon-domain-{team_key}-adds.json"
            if stale_marker.exists():
                print(f"  Deleting stale ADDS marker for {team_key} — the DC was reset, so "
                      "it must be re-promoted, not assumed promoted.")
                stale_marker.unlink()
        driver.deploy_domain_configs(
            {team_key: teams[team_key]}, boxes, comp_dir, nakon_config_path,
            Path(ctx["ssh_key_path"]), os.environ["TF_VAR_vm_username"],
            ctx["scoring_engine_ip"], box_password, promote_dc=dc_reset,
        )
    return True


def mode_resync(targets, ctx, node, comp_dir, state, state_path):
    """Engine-authoritative credential alignment — no rollback, no re-plant.

    1. Pull the secrets the engine actually holds (event.conf, credlist,
       /opt/quotient/.env) and align .deploy_state.json where they differ.
    2. Re-set the selected boxes' passwords to the state values via the guest
       agent, so drifted boxes come back in line without needing SSH.
    box_password (the box login) exists nowhere on the engine — it is baked
    into the boxes at bootstrap — so it is reported, not repaired."""
    print("  Reading engine-authoritative secrets (event.conf, credlist, /opt/quotient/.env)...")
    secrets = read_event_conf(ctx)

    changed = []
    for key in ("admin_password", "postgres_password", "redis_password", "inject_password"):
        new = secrets.get(key)
        if new and state.get(key) != new:
            print(f"    {key}: state {'drifted' if state.get(key) else 'missing'} -> aligned with engine")
            state[key] = new
            changed.append(key)
    if secrets.get("box_creds") and state.get("box_creds") != secrets["box_creds"]:
        print("    box_creds: drifted -> aligned with engine credlist")
        state["box_creds"] = secrets["box_creds"]
        changed.append("box_creds")
    for team_key, pw in (secrets.get("team_passwords") or {}).items():
        entry = (state.get("teams") or {}).get(team_key)
        if pw and entry and entry.get("password") != pw:
            print(f"    {team_key} password: drifted -> aligned with engine")
            entry["password"] = pw
            changed.append(f"teams.{team_key}.password")
    if not changed:
        print("    .deploy_state.json already matches the engine.")
    if changed:
        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, state_path)
        os.chmod(state_path, 0o600)

    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: .deploy_state.json has no box_password — cannot re-set box logins. "
            "Only the state alignment above was applied.")
    box_username, _credlist = load_users_config(comp_dir)
    for t in targets:
        try:
            if box_platform(t["box"]) == "windows":
                rc, out, err = guest_agent_exec_windows(
                    node, t["vmid"],
                    f"net user {WINDOWS_ADMIN_USER} '{box_password}'", timeout=120)
            else:
                script = f"echo {shlex.quote(f'{box_username}:{box_password}')} | chpasswd"
                for user, pw in (state.get("box_creds") or {}).items():
                    script += f"; echo {shlex.quote(f'{user}:{pw}')} | chpasswd"
                rc, out, err = guest_agent_exec_root(node, t["vmid"], script, timeout=120)
            if rc == 0:
                print(f"    {describe_target(t)}: passwords re-set (via guest agent)")
            else:
                print(f"    WARNING: {describe_target(t)}: guest agent rc={rc}: {(err or '').strip()[:120]}")
        except Exception as e:
            print(f"    WARNING: {describe_target(t)}: password re-set failed ({e})")

    print("  NOTE: box_password itself cannot be recovered from the engine — if the box "
          "login (as opposed to credlist accounts) is what drifted, reset it by hand.")
    return targets


def mode_rollback(targets, ctx, node, snapshot, comp_dir, state, nakon_config_path,
                  nakon_bundle, reconfigure):
    """Rollback to snapshot, optionally reconfigure."""
    restored = []
    for t in targets:
        print(f"  Rolling back {describe_target(t)} to '{snapshot}'...")
        try:
            rollback_snapshot(node, t["vmid"], snapshot)
            restored.append(t)
            print(f"    {t['vm_name']} restored")
        except Exception as e:
            print(f"  WARNING: rollback failed for {describe_target(t)}: {e}")

    if not restored:
        raise SystemExit("  ERROR: no box was rolled back successfully — nothing to do.")

    driver.wait_for_boxes_ssh(ctx, restored, timeout=300)

    if reconfigure:
        driver.wait_for_cloud_init(ctx, restored, timeout=240)
        run_nakon_and_harden(restored, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
        try:
            domain_settled = rerun_domain_configs(restored, ctx, comp_dir, state, nakon_config_path)
        except Exception:
            print(f"  tz-ready NOT re-taken — the domain chain failed above, so the boxes are "
                  f"not in an 'as delivered' state.")
            raise
        if domain_settled:
            print(f"  Re-taking '{SNAP_READY}' for the recovered boxes...")
            for t in restored:
                take_snapshot(node, t["vmid"], SNAP_READY,
                              description="tezcatlipoca: as delivered (re-taken by redeploy)")
        else:
            print(f"  tz-ready NOT re-taken — domain configuration could not run (see above); "
                  f"snapshotting now would bake a broken state in as 'as delivered'.")

    return restored


def mode_reconfigure(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """No rollback — re-run the configure chain against the boxes as they are right now."""
    # 900s: the operator->engine->box jump path has banner-timeout flakiness
    # windows; a shared short budget aborts scoping runs on boxes the engine
    # reaches fine
    driver.wait_for_boxes_ssh(ctx, targets, timeout=900)
    run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    roles_path = comp_dir / "domain_roles.json"
    if roles_path.exists():
        roles = json.loads(roles_path.read_text())
        if any(roles.get(t["box_name"]) for t in targets):
            print("  NOTE: reconfigure never resets disks, so domain membership is assumed "
                  "intact. If the AD domain itself is what's broken, use --mode rollback-base "
                  "(or rebuild) — those re-promote/re-join after the reset.")
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
    """Recreate VM from template then configure (clones template, not team1 live box)."""
    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: .deploy_state.json has no box_password — can't recreate a box with the "
            "login the rest of the range uses. Rebuild is unavailable for this competition."
        )
    ssh_public_key = os.environ["TF_VAR_ssh_public_key"]
    box_username = ctx.get("box_username", "ubuntu")

    rebuilt = []
    for t in targets:
        box = t["box"]
        windows = box_platform(box) == "windows"
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
            "net0": f"virtio,bridge=vmbr{t['identifier']}",
            "cores": box["cpu"],
            "memory": box["memory_mb"],
        }
        if not windows:
            config.update({
                "ipconfig0": f"ip={t['ip']}/24,gw=192.168.{t['identifier']}.1",
                "ciuser": box_username,
                "cipassword": box_password,
                "sshkeys": quote_sshkeys(ssh_public_key),
            })
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data=config)

        if box.get("disk_gb"):
            proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/resize", data={
                "disk": "scsi0", "size": f"{box['disk_gb']}G",
            })

        start_vm(node, t["vmid"])

        if windows:
            print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
            gw = f"192.168.{t['identifier']}.1"
            driver.bootstrap_windows_box(node, t["vmid"], t["ip"], gw, "8.8.8.8", box_password)

        rebuilt.append(t)

        if t["team_key"] == "team1":
            print(f"    NOTE: {t['vm_name']} is a Terraform-managed resource. It was recreated "
                  f"outside Terraform, so the next `terraform apply` will see drift and want to "
                  f"replace it. Fine mid-event; re-import or accept the replacement afterwards.")

    driver.wait_for_boxes_ssh(ctx, rebuilt, timeout=600)
    driver.wait_for_cloud_init(ctx, rebuilt, timeout=300)

    print(f"  Snapshotting rebuilt boxes as '{SNAP_BASE}'...")
    for t in rebuilt:
        take_snapshot(node, t["vmid"], SNAP_BASE,
                      description="tezcatlipoca: rebuilt from template, pre-Nakon")

    run_nakon_and_harden(rebuilt, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    try:
        domain_settled = rerun_domain_configs(rebuilt, ctx, comp_dir, state, nakon_config_path)
    except Exception:
        print(f"  tz-ready NOT re-taken — the domain chain failed above, so the boxes are "
              f"not in an 'as delivered' state.")
        raise

    if domain_settled:
        print(f"  Snapshotting rebuilt boxes as '{SNAP_READY}'...")
        for t in rebuilt:
            take_snapshot(node, t["vmid"], SNAP_READY,
                          description="tezcatlipoca: as delivered (rebuilt by redeploy)")
    else:
        print(f"  tz-ready NOT re-taken — domain configuration could not run (see above).")
    return rebuilt


def quote_sshkeys(public_key):
    """Proxmox's `sshkeys` config param wants the key URL-encoded."""
    from urllib.parse import quote
    return quote(public_key.strip(), safe="")



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
                        choices=["rollback-ready", "rollback-base", "reconfigure", "rebuild",
                                 "resync"],
                        help="What to do to the selected boxes (default: rollback-ready). "
                             "resync = align credentials with the engine and re-set box "
                             "passwords via the guest agent, touching nothing else.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Print the resolved targets and their snapshots, then exit.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    comp_name = args.competition
    if comp_name is not None and not valid_comp_name(comp_name):
        raise SystemExit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
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
        if args.mode not in ("reconfigure", "resync"):
            print("  This DISCARDS everything the defending team(s) have done to these boxes.")
        answer = input(f"  Redeploy {len(targets)} box(es) in mode '{args.mode}'? [y/N] ").strip()
        if answer.lower() not in ("y", "yes"):
            print("  Cancelled — nothing was changed.")
            return

    ctx = driver.read_terraform_ctx()

    nakon_config_path = nakon_bundle = None
    if args.mode in ("rollback-base", "reconfigure", "rebuild"):
        nakon_config_path = comp_dir / "nakon-config.json"
        if not nakon_config_path.exists():
            box_password = state.get("box_password")
            if not box_password:
                raise SystemExit(
                    "  ERROR: nakon-config.json is missing and .deploy_state.json has no "
                    "box_password — can't regenerate the machine list (nakon authenticates to "
                    "every box with it). This mode is unavailable for this competition."
                )
            print("  nakon-config.json missing — regenerating from the pinned service/vuln sets...")
            box_username, _credlist = load_users_config(comp_dir)
            nakon_config_path = driver.generate_nakon_config(
                teams, boxes, difficulty, comp_dir, box_password, box_username=box_username
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
    elif args.mode == "resync":
        done = mode_resync(targets, ctx, node, comp_dir, state, state_path)
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
