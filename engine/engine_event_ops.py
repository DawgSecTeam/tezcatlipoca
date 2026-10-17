"""Read and push Quotient's event.conf (engine-authoritative secrets)."""

import base64
import json
import subprocess
import toml

from quotient.setup import build_event_conf
from common.ssh_ops import engine_ssh_opts


def read_event_conf(ctx):
# The engine's files are the source of truth after a re-bootstrap or a partially applied
# seed — the state document can drift. box_password is baked into the boxes and is not
# recoverable here.
    import tomllib

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    def _read(path):
        r = subprocess.run(
            ["ssh", "-i", key, *engine_ssh_opts(ctx),
             f"{scoring_user}@{scoring_ip}", f"sudo cat {path}"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return r.stdout

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
    listing = subprocess.run(
        ["ssh", "-i", key, *engine_ssh_opts(ctx),
         f"{scoring_user}@{scoring_ip}",
         "ls /opt/quotient/config/credlists/ 2>/dev/null || true"],
        capture_output=True, text=True, check=False, timeout=30).stdout
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


def push_event_conf(comp_dir, teams, boxes, ctx, event_name, admin_password,
                    postgres_password, redis_password, box_creds, inject_password=None,
                    extra_credlists=None, scoring_password=None):
# Each extra credlist is pushed as <name>.credlist and must be named by some check's credlist
# override so build_event_conf declares it in CredlistSettings.

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    box_services = json.loads((comp_dir / "box_services.json").read_text())

    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
        "boxes_per_team": boxes,
        "team_passwords": {team_key: team_data["password"] for team_key, team_data in teams.items()},
        "event_name": event_name,
        "quotient_admin_password": admin_password,
        "quotient_scoring_password": scoring_password or admin_password,
        "inject_password": inject_password,
    }

    event_conf = build_event_conf(quotient_ctx, box_services)
    event_conf_toml = toml.dumps(event_conf)

    event_conf_b64 = base64.b64encode(event_conf_toml.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            f"sudo mkdir -p /opt/quotient/config && "
            f"echo '{event_conf_b64}' | base64 -d | sudo tee /opt/quotient/config/event.conf > /dev/null && "
            "sudo chmod 600 /opt/quotient/config/event.conf",
        ],
        check=True, timeout=30,
    )

    credlist = "".join(f"{user},{pw}\n" for user, pw in box_creds.items())
    credlist_b64 = base64.b64encode(credlist.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            f"sudo mkdir -p /opt/quotient/config/credlists && echo '{credlist_b64}' | base64 -d | sudo tee /opt/quotient/config/credlists/linux.credlist > /dev/null && "
            "sudo chmod 600 /opt/quotient/config/credlists/linux.credlist",
        ],
        check=True, timeout=30,
    )

    for list_name, pairs in (extra_credlists or {}).items():
        content = "".join(f"{user},{pw}\n" for user, pw in pairs.items())
        content_b64 = base64.b64encode(content.encode()).decode()
        subprocess.run(
            [
                "ssh", "-i", key,
                *engine_ssh_opts(ctx),
                f"{scoring_user}@{scoring_ip}",
                f"sudo mkdir -p /opt/quotient/config/credlists && echo '{content_b64}' | base64 -d | sudo tee /opt/quotient/config/credlists/{list_name}.credlist > /dev/null && "
                f"sudo chmod 600 /opt/quotient/config/credlists/{list_name}.credlist",
            ],
            check=True, timeout=30,
        )
        print(f"  Pushed credlist {list_name}.credlist ({len(pairs)} account(s))")

    env_content = (
        f"POSTGRES_PASSWORD={postgres_password}\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_b64 = base64.b64encode(env_content.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env > /dev/null && "
            "sudo chmod 600 /opt/quotient/.env",
        ],
        check=True, timeout=30,
    )

    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose restart",
        ],
        # The postgres stop+start alone can run past a minute on a loaded node, outliving the 60s default.
        check=True, timeout=300,
    )

    print("  Event configuration pushed to scoring engine")
