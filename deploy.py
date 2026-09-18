"""Orchestrator: seven-phase deploy and CLI."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from beacon_ops import plant_team_beacons
from clone_ops import clone_team_boxes
from config_ops import (
    _prompt_difficulty,
    collect_boxes,
    collect_teams,
    collect_users_config,
    confirm_deploy,
    destroy_bridge_if_exists,
    load_boxes,
    load_injects,
    load_previous_competitions,
    random_password,
    resolve_inject_times,
    update_env,
)
from constants import (
    MAX_TEAMS,
    PER_MACHINE_NAKON_BUDGET,
    SCORING_ENGINE_VMID,
    SNAP_BASE,
    SNAP_READY,
)
from domain_ops import deploy_domain_configs
from engine_ops import bootstrap_scoring_engine, ensure_nat_forwarding, push_event_conf
from hardening_ops import fix_dns_on_boxes, fix_services_on_boxes, setup_ubuntu_auth
from nakon_ops import build_nakon_bundle, generate_nakon_config, run_nakon
from quotient.setup import create_injects, seed_teams, unpause_engine
from range_ops import destroy_vm_if_exists, enumerate_targets, take_snapshot, vm_id_for
from ssh_ops import read_terraform_ctx, wait_for_boxes_ssh, wait_for_cloud_init, wait_for_http, wait_for_ssh
from utils import compfile_flag, load_compfile, load_users_config
from windows_ops import bootstrap_windows_box, is_windows_template

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1):
    """Main deployment pipeline.

    num_teams / assume_yes let the tool run non-interactively (argparse in main()): when they're
    None/False the function prompts exactly as it did before. from_phase (>1) resumes a partially
    built range: it SKIPS the destructive [1/7] cleanup and [2/7] terraform apply, and reloads the
    teams + per-run secrets from competitions/<id>/.deploy_state.json so a resume agrees with what
    was already deployed. After each numbered phase completes, the last-completed phase number is
    checkpointed to that state file; on failure the resume command is printed.
    """
    # Load competition configuration (Compfile is key=value format)
    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    # Themeable box login username + credlist account names (competitions/<id>/users.json,
    # optional — defaults to ubuntu/admin/user1/user2 when absent, matching every pre-existing
    # competition's behavior).
    box_username, credlist_usernames = load_users_config(comp_dir)
    comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {comp_name}")
    print(f"{'='*60}\n")

    boxes = load_boxes(comp_dir)
    if not boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)

    # Warn (don't block) if this competition's boxes.json still names a template known to be
    # broken (see docs/usage-people.md's "Adding a template VM" — 106/ubuntu24.04 and
    # 920/debian13-lite have bad cloud-init and clones never get a working network/SSH). A
    # reused competition replays its saved boxes.json exactly, so this can silently outlive the
    # interactive box-picker warning that would otherwise catch it — unconditional here (not
    # gated by --yes/confirm_deploy) since a non-interactive/CI deploy skips that prompt too.
    KNOWN_BROKEN_TEMPLATES = {"debian13-lite", "ubuntu24.04"}
    for b in boxes:
        if b.get("template") in KNOWN_BROKEN_TEMPLATES:
            print(f"  WARNING: box '{b['name']}' uses template '{b['template']}', which is "
                  f"known broken (bad cloud-init — clones won't get a working network/SSH). "
                  f"Use '{b['template']}-fix' instead. See docs/usage-people.md's "
                  f"Troubleshooting table.")

    # Resumable-phase state. Secrets (admin/inject/postgres/redis) and the team set are per-run;
    # a resume MUST reuse the originals or the engine's already-written .env / already-seeded
    # admin login won't match. Persist them here (gitignored, mode 0600) and reload on resume.
    state_path = comp_dir / ".deploy_state.json"
    if from_phase > 1 and not state_path.exists():
        # Without this guard the else-branch below ran as if fresh — minting NEW passwords and
        # overwriting teams.json — while from_phase still skipped the destructive phases 1-2,
        # leaving the deployed range and its credentials silently out of sync (e.g. nakon
        # authenticating with a password no box has).
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} but {state_path} doesn't exist — there are no "
            f"saved team passwords/secrets to resume with, and generating fresh ones while "
            f"skipping the destructive phases would leave the deployed range and its credentials "
            f"out of sync. Re-run without --from-phase for a clean redeploy."
        )
    resuming = from_phase > 1

    def _save_state():
        state_path.write_text(json.dumps(state, indent=2))
        try:
            os.chmod(state_path, 0o600)
        except OSError:
            pass

    def checkpoint(n):
        state["last_phase"] = n
        _save_state()

    injects = load_injects(comp_dir)

    if resuming:
        state = json.loads(state_path.read_text())
        teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in state["teams"].items()
        }
        number_of_teams = len(teams)
        admin_password = state.get("admin_password") or random_password()
        postgres_password = state.get("postgres_password") or random_password()
        redis_password = state.get("redis_password") or random_password()
        box_password = state.get("box_password") or random_password()
        box_creds = state.get("box_creds") or {
            name: random_password() for name in credlist_usernames
        }
        inject_password = state.get("inject_password")
        print(f"  Resuming from phase {from_phase} "
              f"({number_of_teams} team(s), last completed phase {state.get('last_phase')})")
    else:
        if num_teams is not None:
            if not (1 <= num_teams <= MAX_TEAMS):
                raise SystemExit(
                    f"--teams must be between 1 and {MAX_TEAMS} (team identifiers are "
                    f"192.168.<101-254>.x)"
                )
            number_of_teams = num_teams
        else:
            while True:
                raw = input("How many teams? ").strip()
                try:
                    number_of_teams = int(raw)
                except ValueError:
                    print("  Enter a whole number.")
                    continue
                if 1 <= number_of_teams <= MAX_TEAMS:
                    break
                print(f"  Enter a number from 1 to {MAX_TEAMS} "
                      f"(team identifiers are 192.168.<101-254>.x).")
        teams = collect_teams(number_of_teams)
        # Per-competition Quotient web-admin password (scoreboard/admin login only). Postgres and
        # Redis passwords for the Quotient stack are generated once here and passed to both
        # bootstrap_scoring_engine() and push_event_conf() so the two .env writes agree.
        admin_password = random_password()
        postgres_password = random_password()
        redis_password = random_password()
        # Box login (box_username's cloud-init account every target box gets, themeable via
        # users.json — see load_users_config() above) and the credlist accounts Quotient's
        # Ssh/Smtp/Imap/Sql/Ftp checks authenticate WITH (credlist_usernames, also themeable)
        # — passwords generated fresh per competition, same as admin/postgres/redis above,
        # instead of the fixed ubuntu/ubuntu + admin/changeme123 literals this used to ship
        # with. A fixed value across every deployment is guessable from this open-source repo,
        # or from fingerprinting a past deploy — see utils.py's BOX_USERNAME_DEFAULT comment.
        box_password = random_password()
        box_creds = {name: random_password() for name in credlist_usernames}
        inject_password = random_password() if injects else None
        state = {
            "last_phase": 0,
            "teams": teams,
            "admin_password": admin_password,
            "inject_password": inject_password,
            "postgres_password": postgres_password,
            "redis_password": redis_password,
            "box_password": box_password,
            "box_creds": box_creds,
        }
        _save_state()

    # Generate Nakon config
    nakon_config_path = generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password,
                                               box_username=box_username)

    # Build the Nakon bundle here, on the operator machine, from the FULL machine list. This
    # is the only step that needs the vulndb (MySQL + vulndb-ui/MinIO), and
    # generate_nakon_config() above already proves it's reachable from here. Phases 5 and 6
    # then deploy from this one bundle, so the scoring engine never sees vulndb credentials.
    nakon_bundle = build_nakon_bundle(nakon_config_path)

    # Update environment variables for Terraform
    # Terraform reads TF_VAR_teams and TF_VAR_boxes_per_team as JSON strings
    teams_json = json.dumps({
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in teams.items()
    })
    boxes_json = json.dumps(boxes)

    update_env({
        "TF_VAR_teams": teams_json,
        "TF_VAR_boxes_per_team": boxes_json,
        "TF_VAR_box_password": box_password,
        "TF_VAR_box_username": box_username,
    })

    # Persist team credentials so destroy-competition.py can find and tear down this
    # competition later (it requires teams.json to exist). Also lets us look up team
    # logins for verification without re-deriving them.
    (comp_dir / "teams.json").write_text(teams_json)

    # Confirm deployment (skipped by --yes and on resume — resuming implies prior confirmation)
    if not assume_yes and not resuming:
        if not confirm_deploy(name, scenario, difficulty, teams, boxes):
            print("  Deployment cancelled.")
            return

    node = os.environ["TF_VAR_proxmox_node"]
    # Every (team, box) pair this deploy touches, with the vmid/IP/nakon-machine-name already
    # derived. Built once from the FULL box list — see enumerate_targets()'s docstring for why
    # nothing downstream may re-derive a vmid from a filtered `boxes`.
    all_targets = enumerate_targets(teams, boxes)
    team1_targets = [t for t in all_targets if t["team_key"] == "team1"]

    # Tracks the phase currently executing so the failure handler can tell the operator exactly
    # where to resume from.
    current_phase = max(from_phase, 1)
    try:
        # [1/7] Clean up previous deployment (DESTRUCTIVE — skipped on resume)
        if from_phase <= 1:
            current_phase = 1
            print("[1/7] Cleaning up previous deployment...")
            for t in all_targets:
                destroy_vm_if_exists(node, t["vmid"])
            # Destroy scoring engine (vmid hardcoded in main.tf)
            destroy_vm_if_exists(node, SCORING_ENGINE_VMID)
            # Destroy bridges
            for team in teams.values():
                destroy_bridge_if_exists(node, f"vmbr{team['identifier']}")
            # A fresh deploy must never inherit a previous deploy's sweep marker.
            (comp_dir / ".phase6-swept").unlink(missing_ok=True)
            time.sleep(5)
            checkpoint(1)
        else:
            print("[1/7] Skipped (resume) — leaving existing VMs/bridges in place.")

        # [2/7] Terraform init & apply (DESTRUCTIVE rebuild — skipped on resume)
        if from_phase <= 2:
            current_phase = 2
            print("[2/7] Running Terraform init & apply...")
            subprocess.run(["terraform", "init"], cwd="terraform", check=True, timeout=120)
            # -parallelism=1: concurrent full clones saturate datastore/API (HTTP 596).
            # Scale timeout per Windows box (60GB vs 15GB Linux).
            apply_timeout = 2400 + 1800 * sum(1 for b in boxes if is_windows_template(b["template"]))
            subprocess.run(["terraform", "apply", "-auto-approve", "-parallelism=1"], cwd="terraform", check=True, timeout=apply_timeout)

            # Poll the engine's SSH reachability instead of a blind post-apply sleep.
            apply_ctx = read_terraform_ctx()
            wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                         apply_ctx["scoring_engine_ip"], timeout=300)
            checkpoint(2)
        else:
            print("[2/7] Skipped (resume) — not re-running terraform apply.")

        # Shared setup needed by every phase from [3/7] on. Runs even when phase 3 itself is
        # skipped, because phases 4–7 all reference key/ctx/scoring_ip/scoring_user.
        ctx = read_terraform_ctx()
        key = Path(ctx["ssh_key_path"])
        scoring_user = os.environ["TF_VAR_vm_username"]
        scoring_ip = ctx["scoring_engine_ip"]

        # [3/7] (was: copy SSH key to scoring engine — removed, nothing ever read the copy back;
        # all SSH/SCP to team/scoring boxes uses the local key via ctx, and nakon authenticates
        # to team boxes by password.)
        if from_phase <= 3:
            current_phase = 3
            print("[3/7] Skipped — remote key copy removed (was unused).")
            checkpoint(3)
        else:
            print("[3/7] Skipped (resume).")

        # [4/7] Bootstrap scoring engine
        if from_phase <= 4:
            current_phase = 4
            print("[4/7] Bootstrapping scoring engine (packages, Docker, Quotient)...")
            bootstrap_scoring_engine(ctx, postgres_password, redis_password)

            # Push event.conf before nakon: prevents Quotient crash loop that wipes NAT.
            print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
            push_event_conf(comp_dir, teams, boxes, ctx, name,
                            inject_password=inject_password, admin_password=admin_password,
                            postgres_password=postgres_password, redis_password=redis_password,
                            box_creds=box_creds)
            ensure_nat_forwarding(ctx)

            # [4.5/7] Bootstrap team1's Windows boxes (IP/DNS/credentials — no cloud-init to do
            # this for them), then enable password auth + NOPASSWD sudo for box_username on the
            # Linux ones.
            print(f"[4.5/7] Bootstrapping Windows boxes, enabling password auth + NOPASSWD "
                  f"sudo for {box_username} on Linux boxes (team1)...")
            # Only team1 exists at this point (team2+ are cloned later)
            windows_team1_targets = [t for t in team1_targets if is_windows_template(t["box"]["template"])]
            linux_team1_targets = [t for t in team1_targets if not is_windows_template(t["box"]["template"])]
            for t in windows_team1_targets:
                print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
                gw = f"192.168.{t['identifier']}.1"
                bootstrap_windows_box(node, t["vmid"], t["ip"], gw, "8.8.8.8", box_password)
            setup_ubuntu_auth(linux_team1_targets, ctx)
            checkpoint(4)
        else:
            print("[4/7] Skipped (resume).")

        # [5/7] Fix DNS on team1 boxes (needed for apt-get in Nakon) + run Nakon deployment
        if from_phase <= 5:
            current_phase = 5
            print("[5/7] Fixing DNS on team1 boxes, then running Nakon deployment...")
            fix_dns_on_boxes([t for t in team1_targets if not is_windows_template(t["box"]["template"])], ctx)

            print(f"  Snapshotting team1 boxes as '{SNAP_BASE}' (pre-Nakon restore point)...")
            for t in team1_targets:
                take_snapshot(node, t["vmid"], SNAP_BASE,
                              description="tezcatlipoca: booted, networked, pre-Nakon")

            # Scope deploy to team1 only (--only); bundle still full.
            team1_identifier = teams["team1"]["identifier"]
            team1_machines = [
                m["name"] for m in json.loads(nakon_config_path.read_text())["machines"]
                if m["ip"].split(".")[2] == str(team1_identifier)
            ]

            ensure_nat_forwarding(ctx)

            run_nakon(key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
                      only=team1_machines,
                      timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(team1_machines)))
            print("  Nakon deployment complete")
            checkpoint(5)
        else:
            print("[5/7] Skipped (resume).")

        # [6/7] Clone team1 boxes to other teams, fix DNS, harden services
        if from_phase <= 6:
            current_phase = 6
            swept_marker = comp_dir / ".phase6-swept"
            if swept_marker.exists():
                print("[6/7] Resume marker present — clones + Nakon sweep already done; "
                      "skipping straight to domains/beacons/hardening")
            else:
                print("[6/7] Cloning team1 boxes to other teams, fixing DNS, hardening services...")
                clone_team_boxes(teams, boxes, ctx, comp_dir, box_creds=box_creds,
                                  box_password=box_password)

                # Deploy Nakon on team2+ boxes (team1 already done at [5/7])
                if len(teams) > 1:
                    print("  Deploying Nakon on team2+ boxes...")
                    ensure_nat_forwarding(ctx)
                    all_machines = json.loads(nakon_config_path.read_text())["machines"]
                    run_nakon(key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
                              timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(all_machines)))
                    print("  Nakon deployment on team2+ complete")
                else:
                    print("  Single team — hardening services on team1 boxes...")
                    fix_services_on_boxes(
                        comp_dir, [t for t in all_targets if not is_windows_template(t["box"]["template"])],
                        ctx, box_creds=box_creds,
                    )
                # Written only after a clean sweep so mid-phase-6 resumes don't re-run it.
                swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))

            print("  Configuring Windows AD domains (if any)...")
            deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path,
                                           key, scoring_user, scoring_ip, box_password)

            # Optional hunt artifacts (Compfile: team_beacons 1) — planted before
            # the tz-ready snapshot so clones and restore points carry them.
            if compfile_flag(comp_dir / "Compfile", "team_beacons"):
                print("  Planting team beacons (hunt artifacts)...")
                plant_team_beacons(teams, boxes, ctx, box_username=box_username,
                                   box_password=box_password)

            print(f"  Snapshotting all boxes as '{SNAP_READY}' (as-delivered restore point)...")
            for t in all_targets:
                take_snapshot(node, t["vmid"], SNAP_READY,
                              description="tezcatlipoca: as delivered, post-Nakon + hardening")
            checkpoint(6)
        else:
            print("[6/7] Skipped (resume).")

        # [7/7] Seed competition and create injects (event.conf was pushed at [4/7])
        if from_phase <= 7:
            current_phase = 7
            print("[7/7] Seeding competition and creating injects...")

            # Poll Quotient's HTTP endpoint instead of a blind sleep before seeding.
            wait_for_http(f"http://{scoring_ip}/api/login", timeout=120)

            quotient_ctx = {
                "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
                "quotient_admin_password": admin_password,
            }

            # Gate each sub-step on state flags (unpause_engine is not idempotent).
            if not state.get("seeded"):
                print("  Seeding teams and starting the competition clock...")
                seed_teams(scoring_ip, quotient_ctx)
                state["seeded"] = True
                _save_state()
            else:
                print("  Teams already seeded (resume) — skipping.")

            if not state.get("engine_unpaused"):
                unpause_engine(scoring_ip, quotient_ctx)
                state["engine_unpaused"] = True
                _save_state()
            else:
                print("  Engine already unpaused (resume) — skipping.")

            if injects and not state.get("injects_created"):
                print(f"  Creating {len(injects)} inject(s)...")
                resolve_inject_times(injects)  # anchor offsets to actual competition start
                create_injects(scoring_ip, admin_password, injects)
                state["injects_created"] = True
                _save_state()
            elif injects:
                print("  Injects already created (resume) — skipping.")
            checkpoint(7)
        else:
            print("[7/7] Skipped (resume).")
    except BaseException as e:
        print(f"\n  [!] Deploy failed during phase {current_phase} of '{comp_name}'.")
        resume_phase = current_phase
        # "already exists" suggests Proxmox/Terraform state mismatch; resume from phase 1.
        if current_phase >= 2 and "already exists" in str(e).lower():
            resume_phase = 1
            print("      This looks like a Proxmox/Terraform state mismatch (something the "
                  "prior attempt created still exists, but Terraform's state doesn't know about "
                  "it) — resuming from the failed phase would just hit the same error again.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {comp_name} --from-phase {resume_phase} --yes")
        raise

    # Persist all credentials to a mode-0600 file so operators have a durable, non-log
    # record (the summary below still prints them for convenience, but the file is the
    # authoritative copy and is chmod 600 so it isn't world-readable).
    cred_lines = [
        f"# Credentials for {name} — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scoreboard:  http://{scoring_ip}",
        f"admin  {admin_password}",
    ]
    if inject_password:
        cred_lines.append(f"inject  {inject_password}")
    for team_name, team_data in teams.items():
        cred_lines.append(f"{team_name}  {team_data['password']}  (192.168.{team_data['identifier']}.0/24)")
    # Box login (box_username's cloud-init account, themeable — see users.json) and the
    # credlist accounts Quotient's Ssh/Smtp/Imap/Sql/Ftp checks authenticate WITH — generated
    # fresh per competition (see box_password/box_creds above), recorded here since this file
    # is the one durable, non-log record of every secret this run generated.
    cred_lines.append(f"box-login ({box_username})  {box_password}")
    for user, pw in box_creds.items():
        cred_lines.append(f"box-credlist-{user}  {pw}")
    cred_path = comp_dir / "credentials.txt"
    cred_path.write_text("\n".join(cred_lines) + "\n")
    os.chmod(cred_path, 0o600)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  {name} is live")
    print(f"{'='*60}")
    print(f"Scenario: {scenario}")
    print(f"Saved to: competitions/{comp_name}/  (credentials.txt, mode 0600)")
    print(f"\nScoreboard:    http://{scoring_ip}")
    print(f"Admin login:   admin / {admin_password}")
    if inject_password:
        print(f"Inject login:  inject / {inject_password}   ({len(injects)} inject(s) loaded)")
    print(f"\nTeam logins:")
    for team_name, team_data in teams.items():
        print(f"  {team_name} / {team_data['password']}  (subnet 192.168.{team_data['identifier']}.0/24)")
    print(f"\nBox login:     {box_username} / {box_password}  (every team box)")
    print(f"Box credlist:  " + ", ".join(f"{u}/{p}" for u, p in box_creds.items()))
    print(f"\nScoring engine SSH: ssh -i {key} {scoring_user}@{scoring_ip}")
    print(f"{'='*60}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Tezcatlipoca — CTF Range Deployment. With no flags it runs fully "
                    "interactively (as before); flags let it run non-interactively.",
    )
    parser.add_argument("--competition", help="Competition name. If it already has a Compfile + "
                                              "boxes.json, deploy it straight away; otherwise it "
                                              "is created (needs --scenario/--difficulty or falls "
                                              "back to prompting).")
    parser.add_argument("--teams", type=int, help="Number of teams (skips the 'How many teams?' prompt).")
    parser.add_argument("--yes", action="store_true", help="Skip the confirm-deploy prompt.")
    parser.add_argument("--scenario", help="Scenario description (only when creating a new competition).")
    parser.add_argument("--difficulty", type=int, help="Difficulty 1-10 (only when creating a new competition).")
    parser.add_argument("--box-username", dest="box_username",
                        help="Themeable box login username (only when creating a new competition; "
                             "default 'ubuntu'). Written to competitions/<id>/users.json.")
    parser.add_argument("--credlist-usernames", dest="credlist_usernames",
                        help="Comma-separated, exactly 3 themeable credlist account names (only "
                             "when creating a new competition; default 'admin,user1,user2'). "
                             "Written to competitions/<id>/users.json.")
    parser.add_argument("--from-phase", type=int, default=1, dest="from_phase",
                        help="Resume from this phase (>1 skips the destructive cleanup + terraform "
                             "apply). See the resume hint printed on a failed deploy.")
    parser.add_argument("--plan-only", action="store_true", dest="plan_only",
                        help="Collect/generate the competition's config (Compfile, boxes.json) "
                             "and print a summary, then exit WITHOUT touching any infrastructure "
                             "— no teardown, no `terraform apply`. There is no confirmation "
                             "checkpoint between the box picker and a real deploy otherwise "
                             "(--yes skips it outright, and piped/scripted stdin that happens to "
                             "satisfy every remaining prompt walks straight into one) — use this "
                             "to review the plan first, then re-run without the flag to deploy it "
                             "for real.")
    args = parser.parse_args()

    def _print_plan(comp_name, comp_dir):
        boxes = json.loads((comp_dir / "boxes.json").read_text())
        print(f"\n  ── PLAN for '{comp_name}' — nothing has been deployed " + "─" * 20)
        for b in boxes:
            disk = f"{b['disk_gb']} GB disk" if b.get("disk_gb") else "template's own disk"
            print(f"    {b['name']:<12} {b['template']:<20} {b['cpu']} CPU, {b['memory_mb']} MB, {disk}")
        print(f"\n  Team count is decided at deploy time (--teams N, or the prompt).")
        print(f"  Nothing was deployed — no teardown, no terraform apply.")
        print(f"  Deploy for real with: python3 create-competition.py --competition {comp_name} "
              f"--teams <N> --yes")

    print("Tezcatlipoca - CTF Range Deployment")
    print("=" * 40)

    # Non-interactive path: --competition names the competition to deploy or create.
    if args.competition:
        comp_name = args.competition.strip().lower().replace(" ", "-")
        comp_dir = Path("competitions") / comp_name
        has_compfile = (comp_dir / "Compfile").exists()
        has_boxes = (comp_dir / "boxes.json").exists()

        if comp_dir.is_dir() and has_compfile and has_boxes:
            # Reuse existing competition — straight to deploy(), no stdin needed.
            print(f"Reusing existing competition '{comp_name}'.")
        else:
            # Create the competition. scenario/difficulty come from flags when given, otherwise
            # we fall back to prompting for just the missing pieces. boxes are still collected
            # interactively (there's no non-interactive box spec yet).
            print(f"Creating new competition '{comp_name}'.")
            comp_dir.mkdir(parents=True, exist_ok=True)
            scenario = args.scenario if args.scenario is not None else input("Scenario description: ").strip()
            difficulty = args.difficulty if args.difficulty is not None else _prompt_difficulty()
            (comp_dir / "Compfile").write_text(
                f"name {comp_name}\n"
                f"scenario {scenario}\n"
                f"difficulty {difficulty}\n"
            )
            boxes = collect_boxes()
            (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))
            box_username, credlist_usernames = collect_users_config(
                box_username_flag=args.box_username, credlist_flag=args.credlist_usernames
            )
            (comp_dir / "users.json").write_text(json.dumps(
                {"box_username": box_username, "credlist_usernames": credlist_usernames}, indent=2
            ))

        if args.plan_only:
            _print_plan(comp_name, comp_dir)
            return

        deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase)
        return

    # Interactive path (unchanged behavior when no --competition flag is given).
    previous = load_previous_competitions()
    if previous:
        print("\nPrevious competitions:")
        for i, name in enumerate(previous, 1):
            print(f"  [{i}] {name}")
        print()

    choice = input("Create new (n) or reuse existing (number)? ").strip()

    if choice.lower() == "n":
        comp_name = input("Competition name: ").strip().lower().replace(" ", "-")
        comp_dir = Path("competitions") / comp_name
        comp_dir.mkdir(parents=True, exist_ok=True)

        scenario = input("Scenario description: ").strip()
        difficulty = _prompt_difficulty()

        # Write Compfile in key=value format (matching utils.load_compfile)
        (comp_dir / "Compfile").write_text(
            f"name {comp_name}\n"
            f"scenario {scenario}\n"
            f"difficulty {difficulty}\n"
        )

        boxes = collect_boxes()
        (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))

        box_username, credlist_usernames = collect_users_config()
        (comp_dir / "users.json").write_text(json.dumps(
            {"box_username": box_username, "credlist_usernames": credlist_usernames}, indent=2
        ))
    else:
        try:
            idx = int(choice) - 1
        except ValueError:
            print("Invalid choice.")
            sys.exit(1)
        if 0 <= idx < len(previous):
            comp_name = previous[idx]
        else:
            print("Invalid choice.")
            sys.exit(1)
        comp_dir = Path("competitions") / comp_name

    if args.plan_only:
        _print_plan(comp_name, comp_dir)
        return

    # Pass through --teams/--yes/--from-phase so they still work in interactive mode; they're
    # None/False/1 by default, giving exactly the prior interactive behavior.
    deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase)


if __name__ == "__main__":
    main()
