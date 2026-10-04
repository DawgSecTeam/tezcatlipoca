"""Interactive competition prompts: box lineup, login/credlist usernames, deploy confirmation."""

import os

from constants import MAX_BOXES_PER_TEAM
from pve_api import proxmox_request
from utils import (BOX_USERNAME_DEFAULT, CREDLIST_USERNAMES_DEFAULT, is_legacy_account_name,
                   valid_unix_username)


def list_proxmox_templates():
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    try:
        r = proxmox_request(
            "GET", f"{endpoint}/api2/json/cluster/resources",
            params={"type": "vm"},
            headers={"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"},
            timeout=10,
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
    elif is_legacy_account_name(box_username):
        print(f"  '{box_username}' collides with a legacy distro system account (cloud-init "
              f"would adopt it and brick auth) — using {BOX_USERNAME_DEFAULT} instead.")
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


def confirm_deploy(name, scenario, difficulty, teams, boxes):
    n = len(teams)
    team_range = f"team1–team{n}" if n > 1 else "team1"
    short_scenario = scenario[:72] + ("..." if len(scenario) > 72 else "")

    print("\n─── Ready to deploy " + "─" * 44)
    print(f"  Competition : {name}")
    print(f"  Scenario    : {short_scenario}")
    print(f"  Difficulty  : {difficulty} / 10")
    print(f"  Teams       : {n}  ({team_range}, passwords auto-generated)")
    print("  Boxes       :")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}  ({b['cpu']} CPU, {b['memory_mb']} MB)")
    print()
    print("  Terraform will now run; this takes several minutes.")
    answer = input("  Continue? (y/n): ").strip().lower()
    return answer == "y"
