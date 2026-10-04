"""The competition's secrets: carried on a resume, minted on a fresh deploy."""

import json

from config_ops import collect_teams, random_password, write_state
from constants import MAX_TEAMS, PIPELINE_VERSION
from utils import mint_run_id


def carry_box_password(previous_state):
    """box_password is a golden-hash INPUT (baked into /etc/shadow + cloud-init on the
    golden disk): re-minting it on a fresh deploy rebuilds every golden. Reuse the
    competition's existing one; mint only for a truly new competition."""
    return (previous_state or {}).get("box_password") or random_password()


def resolve_competition_teams(secrets, prior, spec, num_teams, identity, from_phase):
    """Resolve the run id and the team roster (carried on a resume), into `secrets`.

    Deliberately split from the credential mint: preflight must be able to abort a
    fresh deploy before any secret exists to desync (see prepare()'s order note). The
    run id comes first so BOTH paths persist the same id (utils.mint_run_id): reused
    from prior state when the competition directory has one — a resume, a crash-loop
    re-run and a redeploy must reclaim and re-tag the SAME lineage, and a fresh mint
    would orphan every kept template's ownership."""
    secrets.run_id = prior.previous_state.get("run_id") or mint_run_id()
    if prior.resuming:
        secrets.state = json.loads(prior.state_path.read_text())
        secrets.teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in secrets.state["teams"].items()
        }
        secrets.number_of_teams = len(secrets.teams)
        # Carried, not minted; fill gaps only (the mint step never runs on a resume).
        secrets.admin_password = secrets.state.get("admin_password") or random_password()
        secrets.scoring_password = secrets.state.get("scoring_password") or random_password()
        secrets.postgres_password = secrets.state.get("postgres_password") or random_password()
        secrets.redis_password = secrets.state.get("redis_password") or random_password()
        secrets.box_password = secrets.state.get("box_password") or random_password()
        secrets.box_creds = secrets.state.get("box_creds") or {
            name: random_password() for name in spec.credlist_usernames
        }
        secrets.domain_creds = secrets.state.get("domain_creds")
        secrets.inject_password = secrets.state.get("inject_password")
        secrets.state.update({
            "admin_password": secrets.admin_password,
            "scoring_password": secrets.scoring_password,
            "postgres_password": secrets.postgres_password,
            "redis_password": secrets.redis_password,
            "box_password": secrets.box_password,
            "box_creds": secrets.box_creds,
            "domain_creds": secrets.domain_creds,
            "inject_password": secrets.inject_password,
            "scoring_vm_id": identity.engine_vmid,
            "run_id": secrets.run_id,
        })
        write_state(prior.state_path, secrets.state)
        print(f"  Resuming from phase {from_phase} "
              f"({secrets.number_of_teams} team(s), last completed phase {secrets.state.get('last_phase')})")
    else:
        if num_teams is not None:
            if not (1 <= num_teams <= MAX_TEAMS):
                raise SystemExit(
                    f"--teams must be between 1 and {MAX_TEAMS} (team identifiers are "
                    f"192.168.<101-254>.x)"
                )
            secrets.number_of_teams = num_teams
        else:
            while True:
                raw = input("How many teams? ").strip()
                try:
                    secrets.number_of_teams = int(raw)
                except ValueError:
                    print("  Enter a whole number.")
                    continue
                if 1 <= secrets.number_of_teams <= MAX_TEAMS:
                    break
                print(f"  Enter a number from 1 to {MAX_TEAMS} "
                      f"(team identifiers are 192.168.<101-254>.x).")
        secrets.teams = collect_teams(secrets.number_of_teams, identity.engine_vmid)
        # Skeleton only — the mint step adds the secrets after preflight. An abort at
        # the gates leaves a state with the lineage (run id) but no credentials.
        secrets.state = {
            "last_phase": 0,
            "pipeline_version": PIPELINE_VERSION,
            "teams": secrets.teams,
            "scoring_vm_id": identity.engine_vmid,
            "run_id": secrets.run_id,
        }


def mint_competition_secrets(secrets, prior, spec, inputs, identity):
    """Mint the fresh deploy's secrets into `secrets` and persist them (preflight has
    already passed — see prepare()'s order note)."""
    secrets.admin_password = random_password()
    secrets.scoring_password = random_password()
    secrets.postgres_password = random_password()
    secrets.redis_password = random_password()
    # M4: box_password is a golden-hash INPUT (baked into /etc/shadow +
    # cloud-init on the golden disk) — a fresh deploy that re-minted it would
    # rebuild every golden and break the lifecycle's "2-team test run → 8-team
    # competition must not rebuild anything". Reuse the competition's existing
    # box password when prior state carries one; mint fresh only on a truly
    # new competition. passwords.json (packet profile) outranks both: the
    # packet's default credentials ARE the competition, and an operator edit
    # to passwords.json is a deliberate re-key (goldens rebuild — correct).
    secrets.box_password = ((inputs.packet_pw or {}).get("box_password")
                            or carry_box_password(prior.previous_state))
    if inputs.packet_pw:
        print("  Box credentials come from passwords.json (packet profile) — "
              "not re-minted")
    secrets.box_creds = (dict(inputs.packet_credlists.get("linux") or {})
                         or {name: random_password() for name in spec.credlist_usernames})
    secrets.domain_creds = (dict(inputs.packet_credlists.get("domain") or {}) or None)
    secrets.inject_password = random_password() if inputs.injects else None
    secrets.state.update({
        "admin_password": secrets.admin_password,
        "scoring_password": secrets.scoring_password,
        "inject_password": secrets.inject_password,
        "postgres_password": secrets.postgres_password,
        "redis_password": secrets.redis_password,
        "box_password": secrets.box_password,
        "box_creds": secrets.box_creds,
        "domain_creds": secrets.domain_creds,
    })
    write_state(prior.state_path, secrets.state)
