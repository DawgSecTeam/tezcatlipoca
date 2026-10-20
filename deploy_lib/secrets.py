"""The competition's secrets: carried on a resume, minted on a fresh deploy."""

import json
import re
from pathlib import Path

from config.config_ops import collect_teams, random_password
from common.constants import MAX_TEAMS, PIPELINE_VERSION
from common.utils import mint_run_id
from estate import state as estate_state


def _tracked_team_identifier_defaults(comp_dir):
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
    from config.packet_ops import load_profile, resolve_packet_source
    profile_path = resolve_packet_source(source, comp_dir=comp_dir)
    if profile_path is None:
        return []
    profile = load_profile(profile_path)
    return (profile.get("event") or {}).get("team_identifiers") or []


def _fresh_identifier_defaults(comp_dir, previous_state):
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


# box_password is a golden-hash INPUT: re-minting it rebuilds every golden. The workspace
# state is per-RUN (the dashboard seeds it empty), so a fresh run of a competition forgot it
# and rebuilt every golden the templates were kept to avoid — hold it per COMPETITION on the
# run host instead.
COMP_SECRETS_DIR = Path.home() / ".tezcatlipoca" / "comp-secrets"


def _comp_secret_path(comp_name):
    slug = re.sub(r"[^a-z0-9-]+", "-", str(comp_name).lower()).strip("-")
    return COMP_SECRETS_DIR / f"{slug}.json"


def carry_box_password(previous_state, comp_name=None):
    carried = (previous_state or {}).get("box_password")
    if carried:
        return carried
    path = _comp_secret_path(comp_name) if comp_name else None
    if path is not None:
        try:
            stored = json.loads(path.read_text(encoding="utf-8")).get("box_password")
        except (OSError, ValueError):
            stored = None
        if stored:
            return stored
    password = random_password()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"box_password": password}) + "\n", encoding="utf-8")
        path.chmod(0o600)
    return password


# the run id reuses prior state so resume, crash-loop re-run and redeploy reclaim the SAME
# lineage — a fresh mint orphans kept templates' ownership. Split from the mint so preflight can abort before secrets exist.
def resolve_competition_teams(ctx, num_teams, from_phase, team_identifiers=None):
    ctx.run_id = ctx.previous_state.get("run_id") or mint_run_id()
    if ctx.resuming:
        # re-read from disk: the state document holds the only copy of the team passwords
        ctx.state = estate_state.load_flat(ctx.state_path.parent if ctx.state_path
                                           else ctx.comp_dir)
        ctx.teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in ctx.state["teams"].items()
        }
        ctx.number_of_teams = len(ctx.teams)
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
        ctx.save_state()
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
        # skeleton only: an abort at preflight leaves the lineage (run id) with no credentials
        ctx.state = {
            "last_phase": 0,
            "pipeline_version": PIPELINE_VERSION,
            "teams": ctx.teams,
            "scoring_vm_id": ctx.engine_vmid,
            "run_id": ctx.run_id,
        }


def mint_competition_secrets(ctx):
    ctx.admin_password = random_password()
    ctx.scoring_password = random_password()
    ctx.postgres_password = random_password()
    ctx.redis_password = random_password()
    # passwords.json outranks carried state: the packet's credentials ARE the competition, and
    # an operator edit to it is a deliberate re-key (goldens rebuild — correct)
    ctx.box_password = ((ctx.packet_pw or {}).get("box_password")
                            or carry_box_password(ctx.previous_state, ctx.comp_name))
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
    ctx.save_state()
