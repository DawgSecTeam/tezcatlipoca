"""The competition's secrets: carried on a resume, minted on a fresh deploy."""

import json
from pathlib import Path

from config.config_ops import collect_teams, random_password, write_state
from common.constants import MAX_TEAMS, PIPELINE_VERSION
from common.utils import mint_run_id


def _tracked_team_identifier_defaults(comp_dir):
# Read a packet's non-secret team identifier suggestions for fresh-worktree defaults.
    compfile = Path(comp_dir) / "Compfile"
    source = None
    if compfile.is_file():
        for line in compfile.read_text().splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0] == "packet_source":
                source = parts[1].strip()
                break
    if not source:
        return []
    profile_path = Path(source)
    if not profile_path.is_absolute():
        profile_path = Path(__file__).resolve().parents[1] / profile_path
    if not profile_path.is_file():
        return []
    from config.packet_ops import load_profile
    profile = load_profile(profile_path)
    return (profile.get("event") or {}).get("team_identifiers") or []


def _fresh_identifier_defaults(comp_dir, previous_state):
# Prefer identifiers from local history, then tracked packet metadata.
    saved = (previous_state or {}).get("teams") or {}
    identifiers = ([team.get("identifier") for team in saved.values()
                    if isinstance(team, dict) and team.get("identifier") is not None]
                   if isinstance(saved, dict) else [])
    path = Path(comp_dir) / "teams.json"
    if path.is_file():
        try:
            teams = json.loads(path.read_text())
            identifiers.extend(team.get("identifier") for team in teams.values()
                               if isinstance(team, dict)
                               and team.get("identifier") is not None)
        except (OSError, ValueError):
            pass
    identifiers.extend(_tracked_team_identifier_defaults(comp_dir))
    defaults, seen = [], set()
    for value in identifiers:
        normalized = str(value)
        if normalized not in seen:
            defaults.append(value)
            seen.add(normalized)
    return defaults


def carry_box_password(previous_state):
# box_password is a golden-hash INPUT (baked into /etc/shadow + cloud-init on the
#     golden disk): re-minting it on a fresh deploy rebuilds every golden. Reuse the
#     competition's existing one; mint only for a truly new competition.
    return (previous_state or {}).get("box_password") or random_password()


def resolve_competition_teams(ctx, num_teams, from_phase, team_identifiers=None):
# Resolve the run id and the team roster (carried on a resume), into `ctx`.
#
#     Deliberately split from the credential mint: preflight must be able to abort a
#     fresh deploy before any secret exists to desync (see prepare()'s order note). The
#     run id comes first so BOTH paths persist the same id (utils.mint_run_id): reused
#     from prior state when the competition directory has one — a resume, a crash-loop
#     re-run and a redeploy must reclaim and re-tag the SAME lineage, and a fresh mint
#     would orphan every kept template's ownership.
    ctx.run_id = ctx.previous_state.get("run_id") or mint_run_id()
    if ctx.resuming:
        ctx.state = json.loads(ctx.state_path.read_text())
        ctx.teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in ctx.state["teams"].items()
        }
        ctx.number_of_teams = len(ctx.teams)
        # Carried, not minted; fill gaps only (the mint step never runs on a resume).
        ctx.admin_password = ctx.state.get("admin_password") or random_password()
        ctx.scoring_password = ctx.state.get("scoring_password") or random_password()
        ctx.postgres_password = ctx.state.get("postgres_password") or random_password()
        ctx.redis_password = ctx.state.get("redis_password") or random_password()
        ctx.box_password = ctx.state.get("box_password") or random_password()
        ctx.box_creds = ctx.state.get("box_creds") or {
            name: random_password() for name in ctx.credlist_usernames
        }
        ctx.domain_creds = ctx.state.get("domain_creds")
        ctx.inject_password = ctx.state.get("inject_password")
        ctx.state.update({
            "admin_password": ctx.admin_password,
            "scoring_password": ctx.scoring_password,
            "postgres_password": ctx.postgres_password,
            "redis_password": ctx.redis_password,
            "box_password": ctx.box_password,
            "box_creds": ctx.box_creds,
            "domain_creds": ctx.domain_creds,
            "inject_password": ctx.inject_password,
            "scoring_vm_id": ctx.engine_vmid,
            "run_id": ctx.run_id,
        })
        write_state(ctx.state_path, ctx.state)
        print(f"  Resuming from phase {from_phase} "
              f"({ctx.number_of_teams} team(s), last completed phase {ctx.state.get('last_phase')})")
    else:
        if num_teams is not None:
            if not (1 <= num_teams <= MAX_TEAMS):
                raise SystemExit(
                    f"--teams must be between 1 and {MAX_TEAMS} (team identifiers are "
                    f"192.168.<101-254>.x)"
                )
            ctx.number_of_teams = num_teams
        else:
            while True:
                raw = input("How many teams? ").strip()
                try:
                    ctx.number_of_teams = int(raw)
                except ValueError:
                    print("  Enter a whole number.")
                    continue
                if 1 <= ctx.number_of_teams <= MAX_TEAMS:
                    break
                print(f"  Enter a number from 1 to {MAX_TEAMS} "
                      f"(team identifiers are 192.168.<101-254>.x).")
        ctx.teams = collect_teams(
            ctx.number_of_teams, ctx.engine_vmid,
            identifier_defaults=_fresh_identifier_defaults(
                ctx.state_path.parent, ctx.previous_state),
            team_identifiers=team_identifiers)
        # Skeleton only — the mint step adds the secrets after preflight. An abort at
        # the gates leaves a state with the lineage (run id) but no credentials.
        ctx.state = {
            "last_phase": 0,
            "pipeline_version": PIPELINE_VERSION,
            "teams": ctx.teams,
            "scoring_vm_id": ctx.engine_vmid,
            "run_id": ctx.run_id,
        }


def mint_competition_secrets(ctx):
# Mint the fresh deploy's secrets into `ctx` and persist them (preflight has
#     already passed — see prepare()'s order note).
    ctx.admin_password = random_password()
    ctx.scoring_password = random_password()
    ctx.postgres_password = random_password()
    ctx.redis_password = random_password()
    # M4: box_password is a golden-hash INPUT (baked into /etc/shadow +
    # cloud-init on the golden disk) — a fresh deploy that re-minted it would
    # rebuild every golden and break the lifecycle's "2-team test run → 8-team
    # competition must not rebuild anything". Reuse the competition's existing
    # box password when prior state carries one; mint fresh only on a truly
    # new competition. passwords.json (packet profile) outranks both: the
    # packet's default credentials ARE the competition, and an operator edit
    # to passwords.json is a deliberate re-key (goldens rebuild — correct).
    ctx.box_password = ((ctx.packet_pw or {}).get("box_password")
                            or carry_box_password(ctx.previous_state))
    if ctx.packet_pw:
        print("  Box credentials come from passwords.json (packet profile) — "
              "not re-minted")
    ctx.box_creds = (dict(ctx.packet_credlists.get("linux") or {})
                         or {name: random_password() for name in ctx.credlist_usernames})
    ctx.domain_creds = (dict(ctx.packet_credlists.get("domain") or {}) or None)
    ctx.inject_password = random_password() if ctx.injects else None
    ctx.state.update({
        "admin_password": ctx.admin_password,
        "scoring_password": ctx.scoring_password,
        "inject_password": ctx.inject_password,
        "postgres_password": ctx.postgres_password,
        "redis_password": ctx.redis_password,
        "box_password": ctx.box_password,
        "box_creds": ctx.box_creds,
        "domain_creds": ctx.domain_creds,
    })
    write_state(ctx.state_path, ctx.state)
