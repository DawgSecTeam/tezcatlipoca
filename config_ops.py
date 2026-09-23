"""Competition configuration, team/box prompts, and inject handling."""

import json
import os
import re
import secrets
import string
from pathlib import Path

import requests

from constants import MAX_BOXES_PER_TEAM, SCORING_ENGINE_VMID
from range_ops import proxmox_api
from utils import BOX_USERNAME_DEFAULT, CREDLIST_USERNAMES_DEFAULT, valid_unix_username

ENV_PATH = Path(".env")


def load_previous_competitions():
   return [
      p.name
      for p in Path("competitions").iterdir()
      if p.is_dir() and (p / "Compfile").exists()
   ]


def random_password():
    """14 chars, guaranteed upper+lower+digit+symbol, cmd/PS- and URL-safe charset.

    Windows guest boxes set this via `net user` and AD enforces complexity:
    the old letters+digits pool produced digit-free passwords ~13% of runs
    (scrim-extreme-2026-09-20) and the policy rejection killed every Windows
    login downstream. Characters are cmd/PS-quoting safe (no &|<>^%$`"' or
    whitespace) and URL-grammar free (no #/?@): these secrets land in
    postgres DSNs in /opt/quotient/.env, and `#` silently truncates the DSN
    at the password (scrim-extreme-cyberfield-2026-09-22 crash loop)."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!*_-+="]
    alphabet = "".join(pools)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if all(any(c in p for c in pw) for p in pools):
            return pw


def collect_teams(number_of_teams):
    teams = {}
    import os as _os
    override = (_os.environ.get("TF_VAR_team_identifiers") or "").strip()
    ids = [s.strip() for s in override.split(",")] if override else [
        str(100 + i) for i in range(1, number_of_teams + 1)
    ]
    if override and len(ids) < number_of_teams:
        ids += [str(100 + i) for i in range(len(ids) + 1, number_of_teams + 1)]
    seen = set()
    for i in range(1, number_of_teams + 1):
        key = f"team{i}"
        identifier = ids[i - 1]
        if not (identifier.isdigit() and 1 <= int(identifier) <= 254):
            raise SystemExit(
                f"  ERROR: team identifier {identifier!r} must be an integer in 1..254 "
                f"(it becomes the 192.168.<id>.x subnet)."
            )
        if identifier in seen:
            raise SystemExit(f"  ERROR: duplicate team identifier {identifier} — subnets/vmids would collide.")
        seen.add(identifier)
        base = 200 + int(identifier) * 10
        if base <= SCORING_ENGINE_VMID <= base + MAX_BOXES_PER_TEAM - 1:
            raise SystemExit(
                f"  ERROR: team identifier {identifier} maps to vmids "
                f"{base}..{base + MAX_BOXES_PER_TEAM - 1}, colliding with the scoring engine "
                f"(vmid {SCORING_ENGINE_VMID})."
            )
        password = random_password()
        teams[key] = {"identifier": identifier, "password": password}
    return teams


def update_env(updates: dict):
    text = ENV_PATH.read_text()
    for key, value in updates.items():
        line = f"{key}={value}"
        new_text, count = re.subn(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.MULTILINE)
        text = new_text if count else text + f"\n{line}\n"
        os.environ[key] = value
    ENV_PATH.write_text(text)


def list_proxmox_templates():
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    try:
        r = requests.get(
            f"{endpoint}/api2/json/cluster/resources",
            params={"type": "vm"},
            headers={"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"},
            verify=False, timeout=10,
        )
        r.raise_for_status()
        vms = r.json()["data"]
    except Exception as e:
        print(f"  (couldn't list Proxmox templates: {e})")
        return []
    return sorted(
        vm["name"] for vm in vms
        if vm.get("template") == 1
        and "template" in (vm.get("tags") or "").split(";")
        and vm.get("vmid") != scoring_template_id
    )


def destroy_bridge_if_exists(node, bridge_name):
    """Delete a Proxmox Linux bridge if it exists."""
    try:
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
    except Exception as e:
        print(f"    WARNING: could not delete bridge {bridge_name}: {e}")


def _prompt_int(prompt, default):
    """int(input()) with a default on blank and a re-prompt (not a crash) on non-numeric input."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"  Enter a whole number (or leave blank for {default}).")


def _prompt_difficulty():
    """Difficulty prompt that re-asks on non-numeric/out-of-range input instead of crashing."""
    while True:
        raw = input("Difficulty (1-10): ").strip()
        try:
            value = int(raw)
        except ValueError:
            print("  Enter a whole number from 1 to 10.")
            continue
        if 1 <= value <= 10:
            return value
        print("  Enter a number from 1 to 10.")


def _prompt_optional_int(prompt):
    """Like _prompt_int, but blank means None (no default) instead of a fallback value."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print("  Enter a whole number, or leave blank.")


def collect_boxes():
    templates = list_proxmox_templates()
    if templates:
        print("Available box templates (tagged 'template' in Proxmox):")
        for i, t in enumerate(templates, 1):
            print(f"  [{i}] {t}")
        print()
    else:
        print("  (no templates found in Proxmox — you'll need to type template names manually)\n")

    while True:
        raw = input("How many box types for this competition? ").strip()
        try:
            number_of_boxes = int(raw)
        except ValueError:
            print("  Enter a whole number.")
            continue
        if 1 <= number_of_boxes <= MAX_BOXES_PER_TEAM:
            break
        print(f"  Enter a number from 1 to {MAX_BOXES_PER_TEAM} (see MAX_BOXES_PER_TEAM).")

    boxes = []
    for i in range(1, number_of_boxes + 1):
        print(f"\n─── Box {i} of {number_of_boxes} " + "─" * 40)

        existing_names = {b["name"] for b in boxes}
        name = input("  Name (e.g. web01, db, mail): ").strip()
        while not name or name in existing_names:
            if name in existing_names:
                name = input(f"  '{name}' is already used by another box in this competition"
                              " — pick a different name: ").strip()
            else:
                name = input("  Name can't be blank: ").strip()

        if templates:
            while True:
                raw = input(f"  Template [{1}–{len(templates)}, or name]: ").strip()
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(templates):
                        template = templates[idx - 1]
                        break
                except ValueError:
                    pass
                if raw in templates:
                    template = raw
                    break
                print(f"  Enter a number from 1 to {len(templates)}, or a template name.")
        else:
            template = input("  Template name: ").strip()
            while not template:
                template = input("  Template can't be blank — must match a tagged Proxmox VM exactly: ").strip()

        cpu = _prompt_int("  CPU cores    [1]: ", 1)
        memory_mb = _prompt_int("  Memory (MB)  [2048]: ", 2048)
        disk_gb = _prompt_optional_int("  Disk (GB)    [keep template's]: ")

        box = {
            "name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb,
            "disk_gb": disk_gb, "template": template,
        }
        boxes.append(box)
    return boxes


def collect_users_config(box_username_flag=None, credlist_flag=None):
    """Collect themeable box login + 3 credlist usernames; defaults when blank, always writes users.json."""

    if box_username_flag is not None:
        box_username = box_username_flag.strip() or BOX_USERNAME_DEFAULT
    else:
        box_username = input(f"  Box login username [{BOX_USERNAME_DEFAULT}]: ").strip() or BOX_USERNAME_DEFAULT
    if not valid_unix_username(box_username):
        print(f"  '{box_username}' is not a valid box username — using {BOX_USERNAME_DEFAULT}.")
        box_username = BOX_USERNAME_DEFAULT

    if credlist_flag is not None:
        raw = credlist_flag
    else:
        raw = input(
            f"  Credlist usernames, comma-separated (exactly 3) "
            f"[{','.join(CREDLIST_USERNAMES_DEFAULT)}]: "
        ).strip()

    if raw:
        credlist_usernames = [n.strip() for n in raw.split(",") if n.strip()]
        if len(credlist_usernames) != 3:
            print(f"  Need exactly 3 credlist usernames — got {len(credlist_usernames)}, "
                  f"falling back to the default {CREDLIST_USERNAMES_DEFAULT}.")
            credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
        elif not all(valid_unix_username(n) for n in credlist_usernames):
            print(f"  Credlist usernames must be lowercase [a-z_][a-z0-9_-]* — "
                  f"falling back to the default {CREDLIST_USERNAMES_DEFAULT}.")
            credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
    else:
        credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)

    return box_username, credlist_usernames


def load_injects(comp_dir):
    """Load per-competition injects (title/description/offsets/attachments); [] when no injects/ dir."""
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return []

    injects = []
    for sub in sorted(injects_dir.iterdir()):
        manifest = sub / "inject.json"
        if not sub.is_dir() or not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())

        description = meta.get("description", "")
        if meta.get("description_file"):
            desc_path = sub / meta["description_file"]
            if desc_path.exists():
                description = desc_path.read_text()

        skip = {"inject.json", meta.get("description_file")}
        files = [str(f) for f in sorted(sub.iterdir()) if f.is_file() and f.name not in skip]

        injects.append({
            "title":       meta["title"],
            "description": description,
            "open_offset_min":  meta.get("open_offset_min", 0),
            "due_offset_min":  meta.get("due_offset_min", 60),
            "close_offset_min": meta.get("close_offset_min", 90),
            "files":       files,
        })
    return injects


def resolve_inject_times(injects):
    """Resolve inject offsets to RFC3339 timestamps anchored at now (phase 7). Mutates in place."""

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def rfc3339(minutes):
        return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for inj in injects:
        inj["open_time"] = rfc3339(inj.pop("open_offset_min", 0))
        inj["due_time"] = rfc3339(inj.pop("due_offset_min", 60))
        inj["close_time"] = rfc3339(inj.pop("close_offset_min", 90))
    return injects


def load_boxes(comp_dir):
    path = comp_dir / "boxes.json"
    return json.loads(path.read_text()) if path.exists() else None


def confirm_deploy(name, scenario, difficulty, teams, boxes):
    n = len(teams)
    team_range = f"team1–team{n}" if n > 1 else "team1"
    short_scenario = scenario[:72] + ("..." if len(scenario) > 72 else "")

    print("\n─── Ready to deploy " + "─" * 44)
    print(f"  Competition : {name}")
    print(f"  Scenario    : {short_scenario}")
    print(f"  Difficulty  : {difficulty} / 10")
    print(f"  Teams       : {n}  ({team_range}, passwords auto-generated)")
    print(f"  Boxes       :")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}  ({b['cpu']} CPU, {b['memory_mb']} MB)")
    print()
    print("  Terraform will now run; this takes several minutes.")
    answer = input("  Continue? (y/n): ").strip().lower()
    return answer == "y"
