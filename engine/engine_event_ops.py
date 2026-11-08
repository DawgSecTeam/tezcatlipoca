"""Read and push Quotient's event.conf (engine-authoritative secrets)."""

import base64
import json
import toml

from quotient.setup import build_event_conf
from engine.engine_cmd_ops import _run_engine_cmd


def read_event_conf(ctx):
# The engine's files are the source of truth after a re-bootstrap or a partially applied
# seed — the state document can drift. box_password is baked into the boxes and is not
# recoverable here.
    import tomllib

    def _read(path):
        return _run_engine_cmd(ctx, f"sudo cat {path}", step=f"read {path}",
                               capture=True, timeout=30).stdout

    secrets = {}
    event = tomllib.loads(_read("/opt/quotient/config/event.conf"))
    admins = event.get("admin") or []
    if admins:
        secrets["admin_password"] = admins[0].get("pw")
    team_pws = {t.get("name"): t.get("pw") for t in event.get("team") or []}
    if team_pws:
        secrets["team_passwords"] = team_pws
    injects = event.get("inject") or []
    if injects:
        secrets["inject_password"] = injects[0].get("pw")

    box_creds = {}
    for line in _read("/opt/quotient/config/credlists/linux.credlist").splitlines():
        line = line.strip()
        if line and "," in line:
            user, pw = line.split(",", 1)
            box_creds[user] = pw
    if box_creds:
        secrets["box_creds"] = box_creds

    extra = {}
    listing = _run_engine_cmd(ctx, "ls /opt/quotient/config/credlists/ 2>/dev/null || true",
                              step="list credlists", check=False, capture=True,
                              timeout=30).stdout
    for fname in listing.split():
        if fname == "linux.credlist" or not fname.endswith(".credlist"):
            continue
        pairs = {}
        for line in _read(f"/opt/quotient/config/credlists/{fname}").splitlines():
            line = line.strip()
            if line and "," in line:
                user, pw = line.split(",", 1)
                pairs[user] = pw
        if pairs:
            extra[fname[:-len(".credlist")]] = pairs
    if extra:
        secrets["extra_credlists"] = extra

    for line in _read("/opt/quotient/.env").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            secrets["postgres_password"] = line.split("=", 1)[1]
        elif line.startswith("REDIS_PASSWORD="):
            secrets["redis_password"] = line.split("=", 1)[1]
    return secrets


def _quotient_ctx(teams, boxes, event_name, admin_password, scoring_password,
                  inject_password):
    return {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
        "boxes_per_team": boxes,
        "team_passwords": {team_key: team_data["password"] for team_key, team_data in teams.items()},
        "event_name": event_name,
        "quotient_admin_password": admin_password,
        "quotient_scoring_password": scoring_password or admin_password,
        "inject_password": inject_password,
    }


def _write_event_conf(ctx, event_conf_toml):
    event_conf_b64 = base64.b64encode(event_conf_toml.encode()).decode()
    _run_engine_cmd(
        ctx,
        f"sudo mkdir -p /opt/quotient/config && "
        f"echo '{event_conf_b64}' | base64 -d | "
        f"sudo tee /opt/quotient/config/event.conf > /dev/null && "
        "sudo chmod 600 /opt/quotient/config/event.conf",
        timeout=30, step="push event.conf")


# A resume that fixes box_services.json AFTER phase 3 pushed it leaves the engine scoring
# the OLD pin set forever: run-14ac7f14 fixed IIS→SMB at its phase-4 failure boundary, and
# run-bd70a985 opened with every team chasing a phantom `win01-iis` pin. Compare PARSED
# TOML (formatting is not drift). On drift rewrite event.conf + credlists and restart only
# the app service — never /opt/quotient/.env, whose postgres/redis secrets a resumed ctx
# may not even carry. Returns True (current), False (re-pushed), or None (undecidable:
# the engine file couldn't be read/parsed — the downstream verify gates still stand).
def ensure_event_conf_current(comp_dir, teams, boxes, ctx, event_name, admin_password,
                              scoring_password=None, inject_password=None, box_creds=None,
                              extra_credlists=None):
    try:
        box_services = json.loads((comp_dir / "box_services.json").read_text())
    except (OSError, ValueError) as e:
        print(f"  [event.conf] box_services.json unreadable ({e}) — drift check skipped")
        return None
    desired_toml = toml.dumps(build_event_conf(
        _quotient_ctx(teams, boxes, event_name, admin_password, scoring_password,
                      inject_password), box_services))
    try:
        import tomllib
        live = tomllib.loads(_run_engine_cmd(
            ctx, "sudo cat /opt/quotient/config/event.conf", step="read event.conf",
            capture=True, timeout=30).stdout)
        same = (json.dumps(live, sort_keys=True)
                == json.dumps(tomllib.loads(desired_toml), sort_keys=True))
    except Exception as e:                                       # noqa: BLE001
        print(f"  [event.conf] engine copy unreadable ({e}) — drift check skipped")
        return None
    if same:
        return True
    print("  [event.conf] DRIFT — engine pins differ from box_services.json; rewriting "
          "and restarting the scoring app (phase-3 bake predates a resumed fix)")
    _write_event_conf(ctx, desired_toml)
    for list_name, pairs in ([("linux", box_creds or {})]
                             + sorted((extra_credlists or {}).items())):
        if not pairs:
            continue
        content = "".join(f"{user},{pw}\n" for user, pw in pairs.items())
        content_b64 = base64.b64encode(content.encode()).decode()
        _run_engine_cmd(
            ctx,
            f"sudo mkdir -p /opt/quotient/config/credlists && "
            f"echo '{content_b64}' | base64 -d | "
            f"sudo tee /opt/quotient/config/credlists/{list_name}.credlist > /dev/null && "
            f"sudo chmod 600 /opt/quotient/config/credlists/{list_name}.credlist",
            timeout=30, step=f"push {list_name}.credlist")
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose restart server",
                    timeout=300, step="restart quotient server")
    return False


def push_event_conf(comp_dir, teams, boxes, ctx, event_name, admin_password,
                    postgres_password, redis_password, box_creds, inject_password=None,
                    extra_credlists=None, scoring_password=None):
# Each extra credlist is pushed as <name>.credlist and must be named by some check's credlist
# override so build_event_conf declares it in CredlistSettings.

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    box_services = json.loads((comp_dir / "box_services.json").read_text())

    event_conf = build_event_conf(_quotient_ctx(teams, boxes, event_name, admin_password,
                                                scoring_password, inject_password),
                                  box_services)
    _write_event_conf(ctx, toml.dumps(event_conf))

    credlist = "".join(f"{user},{pw}\n" for user, pw in box_creds.items())
    credlist_b64 = base64.b64encode(credlist.encode()).decode()
    _run_engine_cmd(
        ctx,
        f"sudo mkdir -p /opt/quotient/config/credlists && "
        f"echo '{credlist_b64}' | base64 -d | "
        f"sudo tee /opt/quotient/config/credlists/linux.credlist > /dev/null && "
        "sudo chmod 600 /opt/quotient/config/credlists/linux.credlist",
        timeout=30, step="push linux.credlist")

    for list_name, pairs in (extra_credlists or {}).items():
        content = "".join(f"{user},{pw}\n" for user, pw in pairs.items())
        content_b64 = base64.b64encode(content.encode()).decode()
        _run_engine_cmd(
            ctx,
            f"sudo mkdir -p /opt/quotient/config/credlists && "
            f"echo '{content_b64}' | base64 -d | "
            f"sudo tee /opt/quotient/config/credlists/{list_name}.credlist > /dev/null && "
            f"sudo chmod 600 /opt/quotient/config/credlists/{list_name}.credlist",
            timeout=30, step=f"push {list_name}.credlist")
        print(f"  Pushed credlist {list_name}.credlist ({len(pairs)} account(s))")

    env_content = (
        f"POSTGRES_PASSWORD={postgres_password}\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=db\n"
        "POSTGRES_DB=engine\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_b64 = base64.b64encode(env_content.encode()).decode()
    _run_engine_cmd(
        ctx,
        f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env > /dev/null && "
        "sudo chmod 600 /opt/quotient/.env",
        timeout=30, step="push quotient .env")

    # Restart only the app service: restarting the postgres/redis services too makes Quotient
    # come up while Postgres is still in recovery, which is the boot race the template path
    # retries. The service is `server` in quotient's compose file; `quotient` is only the
    # container-name prefix, so naming it here made compose exit 1 with "no such service".
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose restart server",
                    # A container restart can run past a minute on a loaded node.
                    timeout=300, step="restart quotient server")

    print("  Event configuration pushed to scoring engine")
