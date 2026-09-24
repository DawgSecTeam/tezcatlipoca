"""Orchestrator: seven-phase deploy and CLI."""

import fcntl
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
    preflight_gates,
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
from hardening_ops import fix_dns_on_boxes, fix_services_on_boxes, prep_apt_on_boxes, setup_ubuntu_auth
from nakon_ops import acquire_engine_lock, build_nakon_bundle, generate_nakon_config, run_nakon
from quotient.setup import create_injects, engine_paused, seed_teams, unpause_engine
from range_ops import (delete_snapshot, destroy_vm_if_exists, ensure_terraform_workdir,
                       enumerate_targets, list_snapshots, persist_targets, rollback_snapshot,
                       take_snapshot, terraform_dir, terraform_plugin_cache_dir, vm_id_for)
from ssh_ops import (forget_engine_host_key, read_terraform_ctx,
                     wait_for_boxes_ssh,
                     wait_for_cloud_init, wait_for_http, wait_for_ssh)
from utils import compfile_flag, load_compfile, load_users_config, valid_comp_name
from windows_ops import bootstrap_windows_box, is_windows_template

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_DEPLOY_LOCKS = {}  # path -> open fh (keep referenced so flock survives)


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None):
    """Run the seven-phase deploy for one competition; from_phase > 1 resumes from .deploy_state.json.

    scoring_vmid overrides the scoring-engine VMID (default 1000) so several
    competitions can run concurrently on one node; it is persisted to
    .deploy_state.json and reused on resume."""
    # One driver per competition. Two concurrent deploys share the engine's
    # /opt/nakon staging dir and each one's 'rm -rf /opt/nakon/*' wipes the
    # other's plan archives mid-plant (scrim-extreme-2026-09-20: a pkill'd
    # wrapper left run-2's python alive while run-3 started; both died with
    # 'bundle is missing its plan archive').
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

    # Resolve this competition's scoring-engine VMID before taking the engine lock
    # (the lock is keyed on it) and before phase-1 cleanup destroys it. On resume it
    # is authoritative from .deploy_state.json; on a fresh deploy it comes from the
    # flag/arg (default SCORING_ENGINE_VMID).
    _early_state = comp_dir / ".deploy_state.json"
    if from_phase > 1 and _early_state.exists():
        try:
            engine_vmid = int(json.loads(_early_state.read_text()).get("scoring_vm_id", SCORING_ENGINE_VMID))
        except (ValueError, json.JSONDecodeError):
            engine_vmid = SCORING_ENGINE_VMID
    else:
        engine_vmid = int(scoring_vmid) if scoring_vmid is not None else SCORING_ENGINE_VMID
    acquire_engine_lock(engine_vmid)

    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    box_username, credlist_usernames = load_users_config(comp_dir)
    comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {comp_name}")
    print(f"{'='*60}\n")

    boxes = load_boxes(comp_dir)
    if not boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)

    KNOWN_BROKEN_TEMPLATES = {"debian13-lite", "ubuntu24.04"}
    for b in boxes:
        if b.get("template") in KNOWN_BROKEN_TEMPLATES:
            print(f"  WARNING: box '{b['name']}' uses template '{b['template']}', which is "
                  f"known broken (bad cloud-init — clones won't get a working network/SSH). "
                  f"Use '{b['template']}-fix' instead. See docs/usage-people.md's "
                  f"Troubleshooting table.")

    state_path = comp_dir / ".deploy_state.json"
    if from_phase > 1 and not state_path.exists():
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} but {state_path} doesn't exist — there are no "
            f"saved team passwords/secrets to resume with, and generating fresh ones while "
            f"skipping the destructive phases would leave the deployed range and its credentials "
            f"out of sync. Re-run without --from-phase for a clean redeploy."
        )
    resuming = from_phase > 1

    def _save_state():
        # Atomic rename: .deploy_state.json holds the only copy of the box
        # passwords — a torn write here bricks both resume and redeploy.
        tmp_path = state_path.with_name(state_path.name + ".tmp")
        tmp_path.write_text(json.dumps(state, indent=2))
        os.replace(tmp_path, state_path)
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
        state.update({
            "admin_password": admin_password,
            "postgres_password": postgres_password,
            "redis_password": redis_password,
            "box_password": box_password,
            "box_creds": box_creds,
            "inject_password": inject_password,
            "scoring_vm_id": engine_vmid,
        })
        _save_state()
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
        teams = collect_teams(number_of_teams, engine_vmid)
        admin_password = random_password()
        postgres_password = random_password()
        redis_password = random_password()
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
            "scoring_vm_id": engine_vmid,
        }
        _save_state()

    nakon_config_path = generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password,
                                               box_username=box_username)

    nakon_bundle = build_nakon_bundle(nakon_config_path)

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
        "TF_VAR_scoring_vm_id": str(engine_vmid),
    })

    (comp_dir / "teams.json").write_text(teams_json)
    os.chmod(comp_dir / "teams.json", 0o600)

    # Per-competition Terraform working dir + tfvars so concurrent competitions on
    # one node don't share the single terraform/terraform.tfstate or clobber each
    # other via the shared .env. terraform.tfvars.json outranks TF_VAR_* env, so it
    # is authoritative for this comp's teams/boxes/engine-vmid regardless of what
    # another concurrent deploy wrote to .env. The ssh key path is made absolute
    # because it was ../proxmox-relative to the (now deeper) working dir.
    tf_dir = ensure_terraform_workdir(comp_dir)
    raw_key = os.environ["TF_VAR_ssh_private_key_path"]
    ssh_key_abs = raw_key if os.path.isabs(raw_key) else str((Path("terraform") / raw_key).resolve())
    tfvars = {
        "teams": {k: {"identifier": v["identifier"], "password": v["password"]}
                  for k, v in teams.items()},
        "boxes_per_team": boxes,
        "box_password": box_password,
        "box_username": box_username,
        "event_name": name,
        "scoring_vm_id": engine_vmid,
        "ssh_private_key_path": ssh_key_abs,
    }
    tfvars_path = tf_dir / "terraform.tfvars.json"
    tfvars_path.write_text(json.dumps(tfvars, indent=2))
    os.chmod(tfvars_path, 0o600)

    if from_phase <= 2:
        preflight_gates(comp_dir, boxes, number_of_teams, teams=teams,
                        engine_vmid=engine_vmid, check_free=not resuming)

    if not assume_yes and not resuming:
        if not confirm_deploy(name, scenario, difficulty, teams, boxes):
            print("  Deployment cancelled.")
            return

    node = os.environ["TF_VAR_proxmox_node"]
    all_targets = enumerate_targets(teams, boxes)
    persist_targets(comp_dir, all_targets, boxes)
    team1_targets = [t for t in all_targets if t["team_key"] == "team1"]

    current_phase = max(from_phase, 1)
    try:
        if from_phase <= 1:
            current_phase = 1
            print("[1/7] Cleaning up previous deployment...")
            for t in all_targets:
                destroy_vm_if_exists(node, t["vmid"])
            destroy_vm_if_exists(node, engine_vmid)
            for team in teams.values():
                destroy_bridge_if_exists(node, f"vmbr{team['identifier']}")
            (comp_dir / ".phase6-swept").unlink(missing_ok=True)
            time.sleep(5)
            checkpoint(1)
        else:
            print("[1/7] Skipped (resume) — leaving existing VMs/bridges in place.")

        if from_phase <= 2:
            current_phase = 2
            print("[2/7] Running Terraform init & apply...")
            tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
            tf_cwd = str(terraform_dir(comp_dir))
            subprocess.run(["terraform", "init"], cwd=tf_cwd, env=tf_env, check=True, timeout=300)
            apply_timeout = 2400 + 1800 * sum(1 for b in boxes if is_windows_template(b["template"]))
            subprocess.run(["terraform", "apply", "-auto-approve", "-parallelism=1"], cwd=tf_cwd, env=tf_env, check=True, timeout=apply_timeout)

            apply_ctx = read_terraform_ctx(comp_dir)
            # Fresh engine VM => new host key; drop any stale pin so accept-new re-pins it.
            forget_engine_host_key(apply_ctx["scoring_engine_ip"])
            wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                         apply_ctx["scoring_engine_ip"], timeout=300)
            checkpoint(2)
        else:
            print("[2/7] Skipped (resume) — not re-running terraform apply.")

        ctx = read_terraform_ctx(comp_dir)
        key = Path(ctx["ssh_key_path"])
        scoring_user = os.environ["TF_VAR_vm_username"]
        scoring_ip = ctx["scoring_engine_ip"]

        if from_phase <= 3:
            current_phase = 3
            print("[3/7] Skipped — remote key copy removed (was unused).")
            checkpoint(3)
        else:
            print("[3/7] Skipped (resume).")

        if from_phase <= 4:
            current_phase = 4
            print("[4/7] Bootstrapping scoring engine (packages, Docker, Quotient)...")
            bootstrap_scoring_engine(ctx, postgres_password, redis_password)

            print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
            push_event_conf(comp_dir, teams, boxes, ctx, name,
                            inject_password=inject_password, admin_password=admin_password,
                            postgres_password=postgres_password, redis_password=redis_password,
                            box_creds=box_creds)
            ensure_nat_forwarding(ctx)

            print(f"[4.5/7] Bootstrapping Windows boxes, enabling password auth + NOPASSWD "
                  f"sudo for {box_username} on Linux boxes (team1)...")
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

        if from_phase <= 5:
            current_phase = 5
            # A --from-phase 5 repair resume lands on boxes the previous attempt may
            # have half-planted, and the plant is NOT idempotent over a planted box
            # (scrim-extreme-cyberfield-2026-09-22 attempt 3 burned 11 configs this
            # way). tz-base only exists once phase 5 got past its snapshot step, so
            # its presence marks an earlier attempt — restore it before re-planting.
            if from_phase == 5:
                planted = [t for t in team1_targets
                           if SNAP_BASE in list_snapshots(node, t["vmid"])]
                for t in planted:
                    # ZFS rollback requires the most-recent snapshot: tz-ready (taken
                    # at phase-6 end or by a redeploy rebuild) blocks the tz-base
                    # rollback — delete it first, phase 6 re-takes it.
                    if SNAP_READY in list_snapshots(node, t["vmid"]):
                        print(f"  Phase-5 re-entry: deleting '{SNAP_READY}' on "
                              f"{t['vm_name']} (blocks the tz-base rollback; re-taken in phase 6)")
                        delete_snapshot(node, t["vmid"], SNAP_READY)
                    print(f"  Phase-5 re-entry: rolling {t['vm_name']} back to "
                          f"'{SNAP_BASE}' before re-planting...")
                    rollback_snapshot(node, t["vmid"], SNAP_BASE)
                if planted:
                    # 900s: cold post-rollback boot of a heavily-planted disk
                    # exceeds the 300s budget (resume-d/e aborted on all 5 while
                    # every box was up minutes later)
                    wait_for_boxes_ssh(ctx, planted, timeout=900)

            print("[5/7] Fixing DNS on team1 boxes, then running Nakon deployment...")
            team1_linux = [t for t in team1_targets if not is_windows_template(t["box"]["template"])]
            fix_dns_on_boxes(team1_linux, ctx)
            # Prep apt BEFORE the tz-base snapshot: the boxes are reachable here (fix_dns just
            # succeeded over SSH), whereas right after the snapshot the guest agent/SSH are
            # briefly unresponsive. Running it here also bakes the disabled apt-daily + fresh
            # index into tz-base, so it survives a phase-5 rollback. NAT is ensured first so
            # the apt-get update has internet.
            ensure_nat_forwarding(ctx)
            prep_apt_on_boxes(team1_linux, ctx)

            print(f"  Snapshotting team1 boxes as '{SNAP_BASE}' (pre-Nakon restore point)...")
            for t in team1_targets:
                take_snapshot(node, t["vmid"], SNAP_BASE,
                              description="tezcatlipoca: booted, networked, pre-Nakon")

            team1_identifier = teams["team1"]["identifier"]
            team1_machines = [
                m["name"] for m in json.loads(nakon_config_path.read_text())["machines"]
                if m["ip"].split(".")[2] == str(team1_identifier)
            ]

            ensure_nat_forwarding(ctx)

            failed = run_nakon(key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
                               only=team1_machines,
                               timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(team1_machines)))
            state["nakon_failed_steps"] = failed[:20]
            _save_state()
            print("  Nakon deployment complete")
            checkpoint(5)
        else:
            print("[5/7] Skipped (resume).")

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

                if len(teams) > 1:
                    print("  Deploying Nakon on team2+ boxes...")
                    ensure_nat_forwarding(ctx)
                    prep_apt_on_boxes(
                        [t for t in all_targets if not is_windows_template(t["box"]["template"])], ctx)
                    all_machines = json.loads(nakon_config_path.read_text())["machines"]
                    # strict=False: the phase-6 sweep re-runs every machine on every resume, and
                    # one flaky plant (apt rotation, IIS Chocolatey state) must not kill a 2-hour
                    # sweep after 98% of it landed. Phase 5 keeps strict — that's the first, and
                    # authoritative, plant.
                    failed = run_nakon(key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
                                       timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(all_machines)),
                                       strict=False)
                    state["nakon_failed_steps"] = failed[:20]
                    _save_state()
                    print("  Nakon deployment on team2+ complete")
                else:
                    print("  Single team — hardening services on team1 boxes...")
                    fix_services_on_boxes(
                        comp_dir, [t for t in all_targets if not is_windows_template(t["box"]["template"])],
                        ctx, box_creds=box_creds,
                    )
                swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))

            print("  Configuring Windows AD domains (if any)...")
            deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path,
                                           key, scoring_user, scoring_ip, box_password)

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

        if from_phase <= 7:
            current_phase = 7
            print("[7/7] Seeding competition and creating injects...")

            wait_for_http(f"http://{scoring_ip}/api/login", timeout=120)

            quotient_ctx = {
                "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
                "quotient_admin_password": admin_password,
            }

            if not state.get("seeded"):
                print("  Seeding teams and starting the competition clock...")
                seed_teams(scoring_ip, quotient_ctx)
                state["seeded"] = True
                _save_state()
            else:
                print("  Teams already seeded (resume) — skipping.")

            if not state.get("engine_unpaused"):
                # The unpause POST isn't idempotent, so a resume in the crash
                # window between POST and flag-save asks the engine first and
                # re-POSTs only when it really is still paused.
                paused = engine_paused(scoring_ip, quotient_ctx)
                if paused is False:
                    print("  Engine reports itself unpaused — recording and skipping.")
                else:
                    unpause_engine(scoring_ip, quotient_ctx)
                state["engine_unpaused"] = True
                _save_state()
            else:
                print("  Engine already unpaused (resume) — skipping.")

            if injects and not state.get("injects_created"):
                print(f"  Creating {len(injects)} inject(s)...")
                resolve_inject_times(injects)
                _created, failed_titles = create_injects(scoring_ip, admin_password, injects)
                if failed_titles:
                    print(f"  WARNING: {len(failed_titles)} inject(s) failed to create: "
                          f"{', '.join(failed_titles)} — re-run --from-phase 7 to retry "
                          f"(existing injects are deduped)")
                else:
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
        if current_phase >= 2 and "already exists" in str(e).lower():
            resume_phase = 1
            print("      This looks like a Proxmox/Terraform state mismatch (something the "
                  "prior attempt created still exists, but Terraform's state doesn't know about "
                  "it) — resuming from the failed phase would just hit the same error again.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {comp_name} --from-phase {resume_phase} --yes")
        raise

    cred_lines = [
        f"# Credentials for {name} — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scoreboard:  http://{scoring_ip}",
        f"admin  {admin_password}",
    ]
    if inject_password:
        cred_lines.append(f"inject  {inject_password}")
    for team_name, team_data in teams.items():
        cred_lines.append(f"{team_name}  {team_data['password']}  (192.168.{team_data['identifier']}.0/24)")
    cred_lines.append(f"box-login ({box_username})  {box_password}")
    for user, pw in box_creds.items():
        cred_lines.append(f"box-credlist-{user}  {pw}")
    cred_path = comp_dir / "credentials.txt"
    cred_path.write_text("\n".join(cred_lines) + "\n")
    os.chmod(cred_path, 0o600)

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
    parser.add_argument("--scoring-vmid", type=int, default=None, dest="scoring_vmid",
                        help="VMID for this competition's scoring engine (default 1000). Give each "
                             "concurrent competition on a shared node a distinct free VMID so their "
                             "engines don't collide. Persisted to .deploy_state.json and reused on "
                             "resume (ignored on --from-phase, which reads it back from state).")
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

    if args.competition:
        comp_name = args.competition.strip().lower().replace(" ", "-")
        if not valid_comp_name(comp_name):
            sys.exit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
        comp_dir = Path("competitions") / comp_name
        has_compfile = (comp_dir / "Compfile").exists()
        has_boxes = (comp_dir / "boxes.json").exists()

        if comp_dir.is_dir() and has_compfile and has_boxes:
            print(f"Reusing existing competition '{comp_name}'.")
        else:
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

        deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase, scoring_vmid=args.scoring_vmid)
        return

    previous = load_previous_competitions()
    if previous:
        print("\nPrevious competitions:")
        for i, name in enumerate(previous, 1):
            print(f"  [{i}] {name}")
        print()

    choice = input("Create new (n) or reuse existing (number)? ").strip()

    if choice.lower() == "n":
        comp_name = input("Competition name: ").strip().lower().replace(" ", "-")
        if not valid_comp_name(comp_name):
            sys.exit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
        comp_dir = Path("competitions") / comp_name
        comp_dir.mkdir(parents=True, exist_ok=True)

        scenario = input("Scenario description: ").strip()
        difficulty = _prompt_difficulty()

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

    deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase, scoring_vmid=args.scoring_vmid)


if __name__ == "__main__":
    main()
