# Author: Hamza

import json
import math
import os
import random
import re
import shlex
import string
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote as url_quote

import requests
import toml
import urllib3
from dotenv import load_dotenv

from constants import (
    DISRUPTIVE_CONFIGS,
    NAKON_DIR,
    PER_MACHINE_NAKON_BUDGET,
    REBOOTS_BOX_CONFIGS,
    SLOW_SERVICES,
    WINDOWS_ADMIN_USER,
)
from quotient.setup import build_event_conf, create_injects, seed_teams, unpause_engine
from range_ops import (
    MAX_BOXES_PER_TEAM,
    MAX_TEAMS,
    SCORING_ENGINE_VMID,
    SNAP_BASE,
    SNAP_READY,
    destroy_vm_if_exists,
    diagnose_unreachable_box,
    enumerate_targets,
    guest_agent_exec_root,
    guest_agent_exec_windows,
    proxmox_api,
    stop_vm,
    take_snapshot,
    vm_id_for,
    wait_for_guest_agent,
    wait_for_proxmox_task,
)
from ssh_ops import (
    read_terraform_ctx,
    ssh_on_gateway,
    ssh_to_engine,
    ssh_via_gateway,
    wait_for_boxes_ssh,
    wait_for_cloud_init,
    wait_for_http,
    wait_for_ssh,
)
from utils import BOX_USERNAME_DEFAULT, DNS_FIX_CMD, load_compfile, load_users_config, pick_competition

ENV_PATH = Path(".env")
# NAKON_DIR imported from constants — keep alias for backwards compatibility is automatic via import

# Re-export for redeploy-competition.py which imports this file as `driver`.
# (will be cleaned up in Phase 3 when redeploy imports ssh_ops directly)
# read_terraform_ctx, ssh_via_gateway, ssh_on_gateway, wait_for_ssh,
# wait_for_boxes_ssh, wait_for_cloud_init, wait_for_http are already
# available as module globals via the ssh_ops import above.

# TF_VAR_* must be in env for `terraform` subprocesses (inherited from parent).
load_dotenv(ENV_PATH)

# Proxmox uses self-signed cert; matches main.tf insecure=true.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

### Helper functions

def load_previous_competitions():
   return [
      p.name
      for p in Path("competitions").iterdir()
      if p.is_dir() and (p / "Compfile").exists()
   ]


def random_password():
    return "".join(random.choices(string.ascii_letters + string.digits, k=12))


### Windows support
# Windows templates have no cloud-init; bootstrap via QEMU guest agent (virtio-serial)
# for IP/gateway/DNS, admin password, sshd/guest-agent. "win" substring is the
# single convention for Windows detection (nakon randomize + terraform initialization).
# WINDOWS_ADMIN_USER and REBOOTS_BOX_CONFIGS live in constants.py.


def is_windows_template(template_name):
    return "win" in template_name.lower()


def bootstrap_windows_box(node, vmid, ip, gateway, dns_server, admin_password, timeout=600):
    """Post-clone Windows setup: static IP/gateway/DNS, admin password, sshd + guest agent
    via QEMU guest agent (only channel before network/password exists). Waits for agent;
    raises on timeout since downstream steps depend on it.
    """
    if not wait_for_guest_agent(node, vmid, timeout=timeout):
        raise RuntimeError(f"vmid {vmid}: guest agent never became responsive within {timeout}s")

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$adapter = Get-NetAdapter | Where-Object {{ $_.Status -eq 'Up' }} | Select-Object -First 1
if (-not $adapter) {{ throw "no up NetAdapter found" }}
Remove-NetIPAddress -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress '{ip}' -PrefixLength 24 -DefaultGateway '{gateway}'
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses '{dns_server}'

# nakon's paramiko connection authenticates with this password (see nakon/deploy/ssh.py) —
# without this the account still has whatever the template baked in, which nothing downstream
# knows.
net user {WINDOWS_ADMIN_USER} "{admin_password}"

Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name sshd -ErrorAction SilentlyContinue
Set-Service -Name QEMU-GA -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name QEMU-GA -ErrorAction SilentlyContinue
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}}
"""
    rc, out, err = guest_agent_exec_windows(node, vmid, ps_script, timeout=90)
    if rc != 0:
        raise RuntimeError(f"vmid {vmid}: bootstrap script failed (rc={rc}): {err or out}")


def dns_repoint_windows_box(node, vmid, dns_server, timeout=60):
    """Point Windows DNS at the team's DC before domain join (Add-Computer needs SRV records
    from an authoritative server). Guest-agent equivalent of Linux dns* repoint.
    """
    ps_script = f"""
$ErrorActionPreference = 'Stop'
$adapter = Get-NetAdapter | Where-Object {{ $_.Status -eq 'Up' }} | Select-Object -First 1
if (-not $adapter) {{ throw "no up NetAdapter found" }}
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses '{dns_server}'
"""
    rc, out, err = guest_agent_exec_windows(node, vmid, ps_script, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"vmid {vmid}: DNS repoint failed (rc={rc}): {err or out}")


def collect_teams(number_of_teams):
    teams = {}
    for i in range(1, number_of_teams + 1):
        key = f"team{i}"
        identifier = str(100 + i)
        password = random_password()
        teams[key] = {"identifier": identifier, "password": password}
    return teams


def update_env(updates: dict):
    # Rewrites TF_VAR_* lines in .env in place (preserving comments/order) and mirrors the
    # change into os.environ so the `terraform` subprocess calls below see it immediately —
    # they were already loaded once at import time, before these values were known.
    text = ENV_PATH.read_text()
    for key, value in updates.items():
        line = f"{key}={value}"
        # Pass `line` as a function replacement, not a string: re.sub interprets
        # backslashes/group refs (\1, \g<0>) in a string replacement, which would crash
        # or corrupt .env for values like a free-form event_name containing a backslash.
        new_text, count = re.subn(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.MULTILINE)
        text = new_text if count else text + f"\n{line}\n"
        os.environ[key] = value
    ENV_PATH.write_text(text)


def list_proxmox_templates():
    # Mirrors main.tf's own lookup (data.proxmox_virtual_environment_vms.templates filters on
    # tag "template" alone) so the picker only ever offers names Terraform will actually
    # resolve. The scoring engine's own template is excluded — it's looked up by
    # TF_VAR_template_vm_id instead, even though it may carry the same tag.
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
        # Only 404 means "no such bridge"; other errors must warn.
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
    # boxes_per_team is per-competition, not global — different events get different boxes.
    # Asked fresh every time a competition is created; reusing one replays its saved boxes.json.
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
                # Also accept a template name string that matches list_proxmox_templates()
                # exactly — removes the fragile index-only selection. Index still works.
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
        # Blank keeps the template's own disk. Terraform only emits a disk block when this is
        # set (main.tf), because Proxmox cannot shrink a disk — a value below the template's
        # own size fails the clone.
        disk_gb = _prompt_optional_int("  Disk (GB)    [keep template's]: ")

        box = {
            "name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb,
            "disk_gb": disk_gb, "template": template,
        }
        boxes.append(box)
    return boxes


def collect_users_config(box_username_flag=None, credlist_flag=None):
    """Collect themeable box login + 3 credlist usernames (see utils.load_users_config).
    Returns defaults when blank; always writes to users.json for pinning.
    """
    from utils import CREDLIST_USERNAMES_DEFAULT

    if box_username_flag is not None:
        box_username = box_username_flag.strip() or BOX_USERNAME_DEFAULT
    else:
        box_username = input(f"  Box login username [{BOX_USERNAME_DEFAULT}]: ").strip() or BOX_USERNAME_DEFAULT

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
    else:
        credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)

    return box_username, credlist_usernames


def load_injects(comp_dir):
    """Load per-competition injects from competitions/<id>/injects/.
    Each subdirectory with inject.json defines title/description/offsets (minutes
    relative to competition start) and attachments. Offsets are resolved to
    RFC3339 at creation time (phase 7) to avoid anchoring to deploy start.
    Returns [] when no injects/ dir exists.
    """
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

        # Attachments: every file in the folder except the manifest / description source.
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
    # Reusing a competition replays its saved boxes.json, not current .env.
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


def os_to_platform(template):
    """Classify a free-text template name the way nakon does: 'windows' if it has 'win'."""
    return "windows" if "win" in template.lower() else "linux"


# SLOW_SERVICES lives in constants.py (excluded from auto-pick for speed).


def _nakon_randomize(platform, services_budget, vulns_budget):
    """Pick services+vulns via `nakon randomize --json` (cwd=NAKON_DIR for catalog access)."""
    cmd = [
        sys.executable, "-m", "nakon", "randomize",
        "--platform", platform,
        "--services", str(services_budget),
        "--vulns", str(vulns_budget),
        "--exclude", *SLOW_SERVICES,
        "--source", "auto", "--json",
    ]
    result = subprocess.run(cmd, cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon randomize failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine, or VULNDB_UI_URL set. Check vendor/nakon/.env."
        )
    selection = json.loads(result.stdout.strip().splitlines()[-1])
    return selection["services"], selection["vulns"]


def generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password, box_username="ubuntu"):
    services_path = comp_dir / "box_services.json"
    vulns_path = comp_dir / "box_vulns.json"

    # Deterministic re-runs: honour pinned configs if either file exists.
    if services_path.exists() or vulns_path.exists():
        pinned = json.loads(services_path.read_text()) if services_path.exists() else {}
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes
        }
        pinned_from = ", ".join(
            p.name for p in (services_path, vulns_path) if p.exists()
        )
        print(f"  Using pinned configurations from {pinned_from}")
        # Always write both files so later unconditional reads don't fail.
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        vulns_path.write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )
    else:
        # One randomization per box type so every team gets identical services (Quotient wildcard IP).
        box_configs = {}
        for box in boxes:
            platform = os_to_platform(box["template"])
            services, vulns = _nakon_randomize(
                platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            box_configs[box["name"]] = (services, vulns)

        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )

    # Disruptive vulns break DNS/apt; sort them last so package installs still have network.
    # DISRUPTIVE_CONFIGS imported from constants.py

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        services, vulns = box_configs[box["name"]]
        configurations = services + vulns
        # Entries may be string or {"name": ..., "vars": {...}}; normalize for sorting.
        configurations.sort(
            key=lambda c: (c if isinstance(c, str) else c["name"]) in DISRUPTIVE_CONFIGS
        )
        windows = is_windows_template(box["template"])
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": WINDOWS_ADMIN_USER if windows else box_username,
            "password": box_password,
            "configurations": configurations,
        })

    # Machine list is per-competition (not shared nakon/config.json state).
    config_path = comp_dir / "nakon-config.json"
    config_path.write_text(json.dumps({"machines": machines}, indent=2))
    return config_path


def build_nakon_bundle(config_path):
    """Build (or reuse) the Nakon bundle for this competition. Content-addressed;
    cached when catalog unchanged. Runs with cwd=NAKON_DIR for vulndb creds.
    """
    result = subprocess.run(
        [sys.executable, "-m", "nakon", "build",
         "--config", str(Path(config_path).resolve()),
         "--out", "bundles",
         "--json"],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon build failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine. Check vendor/nakon/.env."
        )

    info = json.loads(result.stdout.strip().splitlines()[-1])
    state = "cached" if info["cached"] else "fresh"
    print(f"  Nakon bundle {info['bundle_id'][:12]} ({state}, {info['plans']} plan(s), "
          f"{info['machines']} machine(s))")
    return NAKON_DIR / info["path"]


# PER_MACHINE_NAKON_BUDGET lives in constants.py


def run_nakon(key, scoring_user, scoring_ip, bundle, config_path, only=None, timeout=2400,
              strict=True):
    """Push bundle to scoring engine and run `nakon deploy` there.
    `only` scopes to machine names without changing bundle content.
    strict=True passes --strict so failures abort instead of reporting live.
    """
    ssh_base = [
        "ssh", "-i", str(key),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{scoring_user}@{scoring_ip}",
    ]

    subprocess.run(ssh_base + ["rm -rf /tmp/nakon && mkdir -p /tmp/nakon"],
                   check=True, timeout=60)

    subprocess.run(
        [
            "scp", "-i", str(key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-r",
            str(NAKON_DIR / "nakon"),           # the package itself
            str(bundle),                        # bundles/<bundle_id>/
            str(Path(config_path).resolve()),   # addresses + credentials only
            f"{scoring_user}@{scoring_ip}:/tmp/nakon/",
        ],
        check=True, timeout=600,
    )

    remote_config = f"/opt/nakon/{Path(config_path).name}"
    only_args = ""
    if only:
        only_args = " --only " + " ".join(shlex.quote(name) for name in only)
    strict_arg = " --strict" if strict else ""

    try:
        subprocess.run(
            ssh_base + [
                "sudo mkdir -p /opt/nakon && sudo rm -rf /opt/nakon/* && "
                "sudo cp -r /tmp/nakon/. /opt/nakon/ && "
                "sudo pip3 install --break-system-packages paramiko 2>/dev/null; "
                "cd /opt/nakon && sudo python3 -m nakon deploy "
                f"--bundle /opt/nakon/{bundle.name} --config {remote_config}{only_args}{strict_arg}"
            ],
            check=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Client timeout doesn't reliably kill remote `nakon deploy`; pkill to avoid racing second deploy.
        try:
            subprocess.run(ssh_base + ["sudo pkill -9 -f 'nakon deploy' || true"],
                           timeout=30)
        except Exception:
            pass
        raise


# SSH helpers live in ssh_ops.py — imported at top (see ssh_ops import).
# Kept as module globals for redeploy-competition.py which does `import create_competition as driver`.
# (Phase 3 will make redeploy import ssh_ops directly.)


def fix_dns_on_boxes(targets, ctx):
    """Fix DNS on every target box. All-fail aborts (systemic vs transient)."""

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    box_username = ctx.get("box_username", "ubuntu")
    node = os.environ["TF_VAR_proxmox_node"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    print("  Fixing DNS on all team boxes...")
    total = 0
    failed = 0
    for t in targets:
        total += 1
        ip = t["ip"]
        for attempt in range(1, 9):
            try:
                subprocess.run(
                    [
                        "ssh", "-i", key,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10",
                        "-o", f"ProxyCommand={proxy}",
                        f"{box_username}@{ip}", DNS_FIX_CMD,
                    ],
                    check=True, timeout=40,
                )
                print(f"    DNS fixed on {ip}")
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 8:
                    print(f"    DNS fix attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
                else:
                    print(f"  WARNING: DNS fix failed for {ip} after 8 attempts — proceeding anyway")
                    print(diagnose_unreachable_box(node, t["vmid"]))
                    failed += 1

    if total > 0 and failed == total:
        raise RuntimeError(
            f"DNS fix failed on all {total} team box(es) after 8 attempts each — this looks "
            f"systemic (see the guest-agent diagnosis above for each box), not a one-off timing "
            f"fluke. Aborting rather than proceeding into Nakon against boxes that are already "
            f"known unreachable."
        )


def fix_services_on_boxes(comp_dir, targets, ctx, box_creds=None):
    """Post-nakon service hardening (bind address, mail, ftp, dns, etc).
    Credlist accounts must match push_event_conf's linux.credlist.
    """
    import base64

    creds = box_creds or {"admin": "changeme123", "user1": "password1", "user2": "password2"}
    cred_items = list(creds.items())

    node = os.environ["TF_VAR_proxmox_node"]
    print("  Hardening services on all team boxes...")
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    for t in targets:
        ip = t["ip"]
        services = box_services.get(t["box_name"], [])

        # Build a per-box hardening script
        script_lines = ["#!/bin/bash", "set -e", ""]

        # Always create credlist OS accounts (all auth checks need them).
        script_lines.append(f"# Credlist OS accounts ({'/'.join(creds)}) for all auth-based service checks")
        for username, password in cred_items:
            script_lines.append(f"sudo useradd -m -s /bin/bash {username} 2>/dev/null || true")
            script_lines.append(f"echo '{username}:{password}' | sudo chpasswd")
        script_lines.append("")

        if "mysql" in services or "mariadb" in services:
            script_lines.extend([
                "# MySQL/MariaDB: bind to 0.0.0.0",
                "# Try all possible config file locations",
                "for cnf in /etc/mysql/mysql.conf.d/50-server.cnf /etc/mysql/mariadb.conf.d/50-server.cnf /etc/mysql/my.cnf; do",
                '  if [ -f "$cnf" ]; then',
                "    sudo sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' \"$cnf\"",
                "  fi",
                "done",
                "sudo systemctl restart mysql 2>/dev/null || sudo systemctl restart mariadb 2>/dev/null || true",
                "sleep 2",
                "# Create MySQL users from credlist",
                "cat > /tmp/setup_mysql.sql << 'SQLEOF'",
            ])
            for idx, (username, password) in enumerate(cred_items):
                script_lines.append(f"CREATE USER IF NOT EXISTS '{username}'@'%' IDENTIFIED BY '{password}';")
                grant = "GRANT ALL PRIVILEGES ON *.* TO '{}'@'%' WITH GRANT OPTION;" if idx == 0 \
                    else "GRANT ALL PRIVILEGES ON *.* TO '{}'@'%';"
                script_lines.append(grant.format(username))
            script_lines.extend([
                "FLUSH PRIVILEGES;",
                "SQLEOF",
                "sudo mysql < /tmp/setup_mysql.sql || true",
                "",
            ])

        if "postfix" in services or "smtp" in services:
            script_lines.extend([
                "# Postfix: ensure it listens on all interfaces",
                "sudo postconf -e 'inet_interfaces = all' 2>/dev/null || true",
                "sudo postconf -e 'inet_protocols = ipv4' 2>/dev/null || true",
                "# Ensure smtpd listener exists (non-interactive install may leave master.cf empty).",
                "sudo postconf -M 'smtp/inet=smtp inet n - y - - smtpd' 2>/dev/null || true",
                "sudo systemctl restart postfix 2>/dev/null || true",
                "sleep 1",
                "# Create mail users matching credlist for SMTP checks",
            ])
            for username, password in cred_items:
                script_lines.append(f"sudo useradd -m -s /bin/bash {username} 2>/dev/null || true")
                script_lines.append(f"echo '{username}:{password}' | sudo chpasswd")
            script_lines.append("")

        if "nginx" in services or "http" in services or "web" in services:
            script_lines.extend([
                "# Nginx: ensure it starts and listens on port 80",
                "sudo systemctl restart nginx 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "vsftpd" in services or "ftp" in services:
            script_lines.extend([
                "# Vsftpd: ensure anonymous is off, local users can login",
                "sudo sed -i 's/^#*anonymous_enable.*/anonymous_enable=NO/' /etc/vsftpd.conf 2>/dev/null || true",
                "sudo sed -i 's/^#*local_enable.*/local_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
                "sudo sed -i 's/^#*write_enable.*/write_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
                "sudo systemctl restart vsftpd 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "dovecot" in services or "imap" in services:
            script_lines.extend([
                "# Dovecot: enable plaintext auth, create mail dirs",
                "sudo sed -i 's/^#*disable_plaintext_auth.*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf 2>/dev/null || true",
                # Dovecot 2.4 uses different key; add only on 2.4+.
                "dovecot --version 2>/dev/null | grep -qE '^(2\\.[4-9]|[3-9]\\.)' && "
                "sudo bash -c \"echo 'auth_allow_cleartext = yes' > "
                "/etc/dovecot/conf.d/99-allow-plaintext.conf\" || true",
            ])
            # Mail dir for every credlist account.
            for username, _ in cred_items:
                script_lines.append(f"sudo mkdir -p /home/{username}/mail")
                script_lines.append(f"sudo chmod 700 /home/{username}/mail")
                script_lines.append(f"sudo chown {username}:{username} /home/{username}/mail 2>/dev/null || true")
            script_lines.extend([
                "sudo systemctl restart dovecot 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "bind" in services or "named" in services or "dns" in services:
            script_lines.extend([
                "# Bind9: allow queries from anywhere",
                "cat > /tmp/named.conf.options << 'BIND9EOF'",
                "options {",
                '  directory "/var/cache/bind";',
                "  recursion yes;",
                "  allow-query { any; };",
                "  forwarders { 8.8.8.8; 1.1.1.1; };",
                "};",
                "BIND9EOF",
                "sudo cp /tmp/named.conf.options /etc/bind/named.conf.options",
                # Fix missing named.conf.default-zones
                "if [ ! -f /etc/bind/named.conf.default-zones ]; then",
                "  cat > /tmp/named.conf.default-zones << 'BZEOF'",
                'zone "." {',
                "  type hint;",
                '  file "/usr/share/dns/root.hints";',
                "};",
                'zone "localhost" {',
                "  type master;",
                '  file "/etc/bind/db.local";',
                "};",
                'zone "127.in-addr.arpa" {',
                "  type master;",
                '  file "/etc/bind/db.127";',
                "};",
                "BZEOF",
                "  sudo cp /tmp/named.conf.default-zones /etc/bind/named.conf.default-zones",
                "fi",
                # Ensure db.local exists (some templates strip it)
                "if [ ! -f /etc/bind/db.local ]; then",
                "  cat > /tmp/db.local << 'DLEOF'",
                '$TTL 86400',
                '@   IN  SOA ns1.localhost. root.localhost. (',
                '        2026071201',
                '        3600',
                '        1800',
                '        604800',
                '        86400 )',
                '    IN  NS  ns1.localhost.',
                'ns1 IN  A   127.0.0.1',
                '@   IN  A   127.0.0.1',
                "DLEOF",
                "  sudo cp /tmp/db.local /etc/bind/db.local",
                "fi",
                "sudo systemctl restart bind9 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "telnet-service" in services or "telnet" in services:
            script_lines.extend([
                "# Telnet: enable disabled inetd entry and restart.",
                "sudo update-inetd --enable telnet 2>/dev/null || true",
                "sudo systemctl restart inetutils-inetd 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "splunk" in services:
            script_lines.extend([
                "# Splunk: Nakon only creates user, need something on port 8000",
                "sudo apt-get install -y lighttpd 2>/dev/null || true",
                "sudo sed -i 's/server.port.*/server.port = 8000/' /etc/lighttpd/lighttpd.conf 2>/dev/null || true",
                "sudo systemctl enable lighttpd 2>/dev/null || true",
                "sudo systemctl start lighttpd 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        # Always ensure all relevant services are started
        script_lines.extend([
            "# Ensure all installed services are running",
            "for svc in mysql mariadb postfix nginx vsftpd dovecot bind9 lighttpd apache2; do",
            "  if systemctl list-unit-files \"$svc.service\" &>/dev/null; then",
            "    sudo systemctl start $svc 2>/dev/null || true",
            "  fi",
            "done",
        ])

        # Encode script as base64 to avoid ALL quoting issues
        script_content = "\n".join(script_lines)
        script_b64 = base64.b64encode(script_content.encode()).decode()

        # Deploy and execute via gateway
        deploy_cmd = (
            f"echo '{script_b64}' | base64 -d > /tmp/harden.sh && "
            "chmod +x /tmp/harden.sh && "
            "bash /tmp/harden.sh"
        )

        try:
            result = ssh_via_gateway(ctx, ip, deploy_cmd, timeout=60,
                                      user=ctx.get("box_username", "ubuntu"))
            if result.returncode != 0:
                # Writable-sudoers breaks sudo; fall back to guest agent (root, no sudo needed).
                print(f"    Service hardening warning on {ip}: {result.stderr.strip()[:200]}")
                print(f"    Retrying {ip} via guest agent (as root, no sudo needed)...")
                vmid = t["vmid"]
                # \b (not ^\s*) so this also catches mid-pipeline uses like
                # "echo ... | sudo chpasswd", not just line-leading ones.
                root_script = re.sub(r"\bsudo ", "", script_content)
                try:
                    rc, out, err = guest_agent_exec_root(node, vmid, root_script, timeout=120)
                    if rc == 0:
                        print(f"    Services hardened on {ip} (via guest agent)")
                    else:
                        print(f"    Service hardening still failing on {ip} via guest agent: "
                              f"rc={rc} {err.strip()[:200]}")
                except Exception as e:
                    print(f"    Guest-agent fallback failed for {ip} (vmid {vmid}): {e}")
            else:
                print(f"    Services hardened on {ip}")
        except Exception as e:
            print(f"    Service hardening error on {ip}: {e}")


def setup_ubuntu_auth(targets, ctx):
    """Enable password auth + NOPASSWD sudo for box_username (nakon uses password auth + sudo)."""

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    box_username = ctx.get("box_username", "ubuntu")
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )

    print(f"  Enabling password auth + NOPASSWD sudo for {box_username} on team boxes...")
    auth_cmd = (
        "sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo systemctl restart sshd 2>/dev/null || true; "
        f"echo '{box_username} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/{box_username}; "
        f"sudo chmod 440 /etc/sudoers.d/{box_username}"
    )

    for t in targets:
        ip = t["ip"]
        for attempt in range(1, 9):
            try:
                subprocess.run(
                    [
                        "ssh", "-i", key,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10",
                        "-o", f"ProxyCommand={proxy}",
                        f"{box_username}@{ip}", auth_cmd,
                    ],
                    check=True, timeout=40,
                )
                print(f"    Auth configured on {ip}")
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 8:
                    print(f"    Auth attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
                else:
                    print(f"  WARNING: Auth setup failed for {ip} after 8 attempts — proceeding anyway")


def clone_team_boxes(teams, boxes, ctx, comp_dir, box_creds=None, box_password=None):
    """Clone team1 boxes to other teams and configure networking.

    After cloning, reconfigures IP addresses, fixes DNS, and hardens services
    on ALL team boxes (team1 + cloned teams).
    """
    node = os.environ["TF_VAR_proxmox_node"]
    team_ids = list(teams.values())
    if len(team_ids) < 2:
        print("  Only one team — skipping box cloning.")
        return

    team1 = team_ids[0]

    # Step 1: cloud-init clean on team1 (Linux only; Windows uses sysprep).
    print("  Running cloud-init clean on team1 boxes...")
    for box in boxes:
        if is_windows_template(box["template"]):
            continue
        ip = f"192.168.{team1['identifier']}.{box['last_octet']}"
        try:
            result = ssh_via_gateway(ctx, ip, "sudo cloud-init clean --logs --machine-id",
                                      timeout=30, user=ctx.get("box_username", "ubuntu"))
            if result.returncode != 0:
                print(f"  WARNING: cloud-init clean FAILED for {box['name']} "
                      f"(rc={result.returncode}): {(result.stderr or '').strip()[:150]}")
                print(f"           Clones of {box['name']} may inherit its machine-id/"
                      f"cloud-init state (duplicate-identity bugs).")
            else:
                print(f"    {box['name']}: cloud-init clean done")
        except Exception as e:
            print(f"  WARNING: cloud-init clean failed for {box['name']}: {e}")

    # Step 2: Stop team1 boxes
    # Use 0-based box index for vm_id_for (Terraform creates VMs with 0-based index)
    # while last_octet is used for IP addresses
    print("  Shutting down team1 boxes...")
    for box_idx, box in enumerate(boxes):
        vmid = vm_id_for(team1["identifier"], box_idx)
        vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        vm = next((v for v in vms if v["vmid"] == vmid), None)
        if vm and vm.get("status") == "running":
            stop_vm(node, vmid)
        print(f"    team1-{box['name']} (vmid {vmid}) stopped")

    # Step 3: Clone team1 boxes for each subsequent team
    print("  Cloning team1 boxes to other teams...")
    # Track cloned VMIDs for destroy + resume (not in Terraform state).
    cloned_vms_path = comp_dir / "cloned_vms.json"
    cloned_vms = json.loads(cloned_vms_path.read_text()) if cloned_vms_path.exists() else {}
    existing_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
    for team in team_ids[1:]:
        for box_idx, box in enumerate(boxes):
            src_vmid = vm_id_for(team1["identifier"], box_idx)
            dst_vmid = vm_id_for(team["identifier"], box_idx)
            clone_name = f"{team['identifier']}-{box['name']}"

            if dst_vmid in existing_vmids:
                print(f"    {clone_name} (vmid {dst_vmid}) already exists — skipping clone (resume)")
            else:
                upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src_vmid}/clone", data={
                    "newid": dst_vmid,
                    "name": clone_name,
                    "full": 1,
                })["data"]
                print(f"    team1-{box['name']} (vmid {src_vmid}) -> {clone_name} (vmid {dst_vmid})...")
                wait_for_proxmox_task(node, upid)

            cloned_vms[clone_name] = dst_vmid
            cloned_vms_path.write_text(json.dumps(cloned_vms, indent=2))

            team_subnet = team["identifier"]
            box_octet = box["last_octet"]
            bridge = f"vmbr{team_subnet}"

            if is_windows_template(box["template"]):
                proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                    "net0": f"virtio,bridge={bridge}",
                })
            else:
                ipconfig = f"ip=192.168.{team_subnet}.{box_octet}/24,gw=192.168.{team_subnet}.1"
                proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                    "ipconfig0": ipconfig,
                    "net0": f"virtio,bridge={bridge}",
                })

    # Step 4: Start ALL team boxes (team1 + cloned)
    print("  Starting all team boxes...")
    for team in team_ids:
        for box_idx, box in enumerate(boxes):
            vmid = vm_id_for(team["identifier"], box_idx)
            try:
                vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                vm = next((v for v in vms if v["vmid"] == vmid), None)
                if vm and vm.get("status") != "running":
                    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
                    wait_for_proxmox_task(node, upid, timeout=120)
                print(f"    vmid {vmid} ({team['identifier']}-{box['name']}) started")
            except Exception as e:
                print(f"  WARNING: Failed to start vmid {vmid}: {e}")

    all_targets = enumerate_targets(teams, boxes)

    # Bootstrap cloned team2+ Windows boxes (team1 done in phase 4.5).
    for t in all_targets:
        if t["team_key"] == "team1" or not is_windows_template(t["box"]["template"]):
            continue
        print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
        gw = f"192.168.{t['identifier']}.1"
        bootstrap_windows_box(node, t["vmid"], t["ip"], gw, "8.8.8.8", box_password)

    wait_for_boxes_ssh(ctx, all_targets, timeout=300)
    wait_for_cloud_init(ctx, all_targets, timeout=240)

    fix_dns_on_boxes([t for t in all_targets if not is_windows_template(t["box"]["template"])], ctx)
    setup_ubuntu_auth([t for t in all_targets if not is_windows_template(t["box"]["template"])], ctx)

    # Snapshot cloned team2+ boxes before phase-6 nakon (team1 already has tz-base).
    print(f"  Snapshotting cloned boxes as '{SNAP_BASE}' (pre-Nakon restore point)...")
    for t in all_targets:
        if t["team_key"] == "team1":
            continue  # already snapshotted in phase [5/7]
        take_snapshot(node, t["vmid"], SNAP_BASE,
                      description="tezcatlipoca: cloned, networked, pre-Nakon")

    fix_services_on_boxes(
        comp_dir, [t for t in all_targets if not is_windows_template(t["box"]["template"])],
        ctx, box_creds=box_creds,
    )


def _run_single_nakon_config(machine, configurations, key, scoring_user, scoring_ip, comp_dir,
                              tag, timeout=1800, strict=True):
    """Deploy one machine with overridden configs in isolation (reboot-safe)."""
    tmp_machine = {**machine, "configurations": configurations}
    tmp_config_path = comp_dir / f".nakon-domain-{tag}.json"
    tmp_config_path.write_text(json.dumps({"machines": [tmp_machine]}, indent=2))
    bundle = build_nakon_bundle(tmp_config_path)
    run_nakon(key, scoring_user, scoring_ip, bundle, tmp_config_path,
              only=[machine["name"]], timeout=timeout, strict=strict)


# AD-flavored misconfigs must run after ADDS promotion to affect AD objects.


def wait_for_windows_sshd(node, vmid, timeout=180):
    """Wait for sshd Running via guest agent (agent up doesn't guarantee sshd up after ADDS reboot). Never raises."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, vmid, "(Get-Service sshd -ErrorAction SilentlyContinue).Status", timeout=20
            )
            if rc == 0 and out.strip() == "Running":
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


def wait_for_dc_dns(node, dc_vmid, domain, dc_ip, timeout=300):
    """Poll DC DNS until it serves domain SRV records (DNS may lag guest-agent). Never raises."""

    record = f"_ldap._tcp.dc._msdcs.{domain}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, dc_vmid,
                f"@(Resolve-DnsName -Name {record} -Server {dc_ip} -Type SRV -ErrorAction "
                f"SilentlyContinue).Count -gt 0",
                timeout=20,
            )
            if rc == 0 and out.strip().lower() == "true":
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


def deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path, key, scoring_user,
                          scoring_ip, box_password, promote_dc=True):
    """Per-team AD forest promotion + member joins. Runs after clone to avoid DC clone
    duplication; reboot configs run isolated. Reads domain_roles.json; no-op if absent.
    promote_dc=False only rejoins members (redeploy case).
    """
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        return
    roles = json.loads(roles_path.read_text())
    if not roles:
        return

    node = os.environ["TF_VAR_proxmox_node"]
    all_machines = {m["name"]: m for m in json.loads(nakon_config_path.read_text())["machines"]}
    dc_box = next((b for b in boxes if roles.get(b["name"]) == "dc"), None)
    member_boxes = [b for b in boxes if roles.get(b["name"]) == "member"]
    if dc_box is None:
        print("  WARNING: domain_roles.json has no 'dc' box — skipping domain configuration.")
        return

    for team_key, team in teams.items():
        identifier = team["identifier"]
        domain = f"team{identifier}.local"
        dc_machine_name = f"{dc_box['name']}-team{identifier}"
        dc_machine = all_machines.get(dc_machine_name)
        if dc_machine is None:
            print(f"  WARNING: {team_key}: machine {dc_machine_name} not in nakon config — "
                  f"skipping domain setup for this team")
            continue
        dc_vmid = vm_id_for(identifier, boxes.index(dc_box))
        dc_ip = dc_machine["ip"]

        if not promote_dc:
            print(f"  [{team_key}] DC {dc_box['name']} left as-is — (re)joining member "
                  f"box(es) to existing {domain}...")
        else:
            print(f"  [{team_key}] Promoting {dc_box['name']} ({dc_ip}) to a new AD forest "
                  f"({domain})...")
            _run_single_nakon_config(
                dc_machine,
                [{"name": "ADDS", "vars": {"domain": domain, "dsrm_password": box_password}}],
                key, scoring_user, scoring_ip, comp_dir, tag=f"{team_key}-adds",
            )
            print(f"    Waiting for {dc_box['name']} to reboot and come back (AD DS promotion "
                  f"is slow — budgeting up to 20 min)...")
            if not wait_for_guest_agent(node, dc_vmid, timeout=1200):
                print(f"  WARNING: {dc_box['name']} guest agent never came back after ADDS — "
                      f"skipping the rest of {team_key}'s domain setup")
                continue
            wait_for_windows_sshd(node, dc_vmid, timeout=180)

            print(f"  [{team_key}] Planting AD-flavored misconfigs on {dc_box['name']}...")
            # strict=False: AD misconfig pass lands misconfigs before inevitable non-fatal failures.
            _run_single_nakon_config(
                dc_machine,
                [
                    {"name": "Add User Account", "vars": {
                        "username": "svc-support", "password": box_password,
                        "full_name": "IT Support", "domain_address": domain,
                    }},
                    {"name": "Elevate User Account", "vars": {"username": "svc-support"}},
                    {"name": "Disable System Firewall"},
                    {"name": "Removing all auditing"},
                ],
                key, scoring_user, scoring_ip, comp_dir, tag=f"{team_key}-ad-misconfigs",
                strict=False,
            )

        # Wait for DC DNS SRV records before any member join.
        if member_boxes:
            print(f"  [{team_key}] Waiting for {dc_box['name']}'s DNS to serve {domain}...")
            if not wait_for_dc_dns(node, dc_vmid, domain, dc_ip):
                print(f"  WARNING: {dc_box['name']} DNS never served SRV records for {domain} "
                      f"— member joins may fail")

        for member_box in member_boxes:
            member_machine_name = f"{member_box['name']}-team{identifier}"
            member_machine = all_machines.get(member_machine_name)
            if member_machine is None:
                print(f"  WARNING: {team_key}: machine {member_machine_name} not in nakon "
                      f"config — skipping")
                continue
            member_vmid = vm_id_for(identifier, boxes.index(member_box))
            member_ip = member_machine["ip"]

            if is_windows_template(member_box["template"]):
                print(f"  [{team_key}] Repointing {member_box['name']}'s DNS at "
                      f"{dc_box['name']} ({dc_ip}) so it can find the domain...")
                dns_repoint_windows_box(node, member_vmid, dc_ip)

                print(f"  [{team_key}] Joining {member_box['name']} to {domain}...")
                _run_single_nakon_config(
                    member_machine,
                    [{"name": "Domain Join", "vars": {
                        "domain": domain, "admin_user": WINDOWS_ADMIN_USER,
                        "admin_pass": box_password,
                    }}],
                    key, scoring_user, scoring_ip, comp_dir,
                    tag=f"{team_key}-{member_box['name']}-join",
                )
                print(f"    Waiting for {member_box['name']} to reboot and come back "
                      f"(domain join)...")
                if not wait_for_guest_agent(node, member_vmid, timeout=900):
                    print(f"  WARNING: {member_box['name']} guest agent never came back after "
                          f"Domain Join")
                    continue
                wait_for_windows_sshd(node, member_vmid, timeout=180)
            else:
                # Linux member via realmd/sssd (no reboot, no separate DNS repoint).
                print(f"  [{team_key}] Joining Linux member {member_box['name']} ({member_ip}) "
                      f"to {domain} via realmd/sssd...")
                _run_single_nakon_config(
                    member_machine,
                    [{"name": "domain-join", "vars": {
                        "DOMAIN": domain,
                        "DC_IP": dc_ip,
                        "DOMAIN_ADMIN_USER": WINDOWS_ADMIN_USER,
                        "DOMAIN_ADMIN_PASS": box_password,
                        "BOX_HOSTNAME": member_box["name"],
                    }}],
                    key, scoring_user, scoring_ip, comp_dir,
                    tag=f"{team_key}-{member_box['name']}-join",
                )


def bootstrap_scoring_engine(ctx, postgres_password, redis_password):
    """Bootstrap the scoring engine: install packages, Docker, Quotient.

    postgres_password / redis_password are generated once per deploy() run and passed in so the
    Quotient stack .env written here agrees with the same values push_event_conf() writes later.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    print("  Installing packages on scoring engine...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "sudo killall apt-get apt dpkg 2>/dev/null; sleep 2; "
            "sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>/dev/null; "
            "sudo dpkg --configure -a 2>/dev/null; "
            "sudo apt-get update && sudo apt-get install -y docker.io git curl python3-pip",
        ],
        check=True, timeout=180,
    )

    print("  Installing Docker Compose v2 plugin...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                "install -m 0755 -d /etc/apt/keyrings && "
                "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc > /dev/null && "
                "sudo chmod a+r /etc/apt/keyrings/docker.asc && "
                "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] "
                "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable\" "
                "| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && "
                "sudo apt-get update && sudo apt-get install -y docker-compose-plugin"
            ),
        ],
        check=True, timeout=180,
    )

    print("  Starting Docker (already installed by Terraform)...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "sudo systemctl start docker && sudo systemctl enable docker && sleep 2 && sudo docker version",
        ],
        check=True, timeout=30,
    )

    print("  Cloning Quotient...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "sudo mkdir -p /opt/quotient && sudo git clone --depth 1 --recurse-submodules https://github.com/dbaseqp/Quotient.git /opt/quotient 2>/dev/null || (cd /opt/quotient && sudo git submodule update --init --recursive)",
        ],
        check=True, timeout=60,
    )

    # Write .env for Quotient (required before docker compose build/up)
    print("  Writing Quotient .env...")
    import base64
    quotient_env = (
        f"POSTGRES_PASSWORD={postgres_password}\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_b64 = base64.b64encode(quotient_env.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env",
        ],
        check=True, timeout=10,
    )

    print("  Building Quotient Docker images...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose build --no-cache",
        ],
        check=True, timeout=1800,
    )

    print("  Starting Quotient Docker containers...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose up -d",
        ],
        check=True, timeout=60,
    )

    # Docker sets FORWARD policy to DROP; restore forwarding + isolation.
    print("  Restoring network forwarding rules after Docker start...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                    "sudo iptables -P FORWARD ACCEPT && "
                    "sudo iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
                    "sudo iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP && "
                "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE && "
                # Enable TCP forwarding for ProxyCommand tunnels
                "sudo sed -i 's/^#*AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config && "
                "sudo systemctl reload sshd 2>/dev/null || true"
            ),
        ],
        check=True, timeout=15,
    )
    print("  Forwarding rules restored")

    # Make NAT + isolation durable (Docker wipes iptables on restart).
    print("  Installing range-firewall systemd unit + timer (keeps team NAT + isolation durable)...")
    firewall_script = (
        "#!/bin/bash\n"
        "iptables -P FORWARD ACCEPT\n"
        "iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
        "iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP\n"
        "iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null || "
        "iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE\n"
    )
    firewall_service = (
        "[Unit]\n"
        "Description=Re-assert team NAT/forwarding + team-to-team isolation (Docker wipes it on restart)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/sbin/range-firewall.sh\n"
    )
    firewall_timer = (
        "[Unit]\n"
        "Description=Periodically re-assert team NAT/forwarding + team-to-team isolation\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=30\n"
        "OnUnitActiveSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    firewall_script_b64 = base64.b64encode(firewall_script.encode()).decode()
    firewall_service_b64 = base64.b64encode(firewall_service.encode()).decode()
    firewall_timer_b64 = base64.b64encode(firewall_timer.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                f"echo '{firewall_script_b64}' | base64 -d | sudo tee /usr/local/sbin/range-firewall.sh > /dev/null && "
                "sudo chmod +x /usr/local/sbin/range-firewall.sh && "
                f"echo '{firewall_service_b64}' | base64 -d | sudo tee /etc/systemd/system/range-firewall.service > /dev/null && "
                f"echo '{firewall_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/range-firewall.timer > /dev/null && "
                "sudo systemctl daemon-reload && sudo systemctl enable --now range-firewall.timer"
            ),
        ],
        check=True, timeout=30,
    )
    print("  range-firewall.timer enabled (re-asserts NAT + isolation every 30s)")

    install_range_healthcheck(ctx)

    # Team bridge NICs (ens19/ens20/...) are fully configured by Terraform's
    # null_resource.team_nics + null_resource.reboot_scoring_engine (see terraform/main.tf) by
    # the time this phase runs — no separate configuration needed here.


def install_range_healthcheck(ctx):
    """Install live-ops health check timer on scoring engine (Quotient + NAT/isolation)."""
    import base64

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    print("  Installing range-healthcheck systemd unit + timer...")
    healthcheck_script = (
        "#!/bin/bash\n"
        "LOG=/var/log/range-healthcheck.log\n"
        "ts() { date '+%Y-%m-%d %H:%M:%S'; }\n"
        "\n"
        "# 1. Quotient container(s) running\n"
        "if ! docker ps --filter 'name=quotient' --filter 'status=running' -q | grep -q .; then\n"
        "  echo \"$(ts) FAIL quotient-containers: no running container matching name=quotient\" >> \"$LOG\"\n"
        "fi\n"
        "\n"
        "# 2. Quotient's API is responding at all. Deliberately NOT curl -f: /api/login is a\n"
        "# POST-only route, so a plain GET correctly gets 405 -- that's still proof the server\n"
        "# is up. Only '000' (curl's code for no connection at all) counts as down.\n"
        "code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost/api/login 2>/dev/null)\n"
        "if [ -z \"$code\" ] || [ \"$code\" = '000' ]; then\n"
        "  echo \"$(ts) FAIL quotient-api: http://localhost/api/login did not respond (curl code: ${code:-none})\" >> \"$LOG\"\n"
        "fi\n"
        "\n"
        "# 3. Team-to-team isolation rule present (see bootstrap_scoring_engine()/range-firewall.sh)\n"
        "if ! iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null; then\n"
        "  echo \"$(ts) FAIL isolation-rule: team-to-team DROP rule missing from FORWARD -- "
        "teams may be able to reach each other right now\" >> \"$LOG\"\n"
        "fi\n"
        "\n"
        "# 4. Team-subnet NAT rule present (nakon/team boxes need this for internet access)\n"
        "if ! iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null; then\n"
        "  echo \"$(ts) FAIL nat-rule: team-subnet MASQUERADE missing from POSTROUTING\" >> \"$LOG\"\n"
        "fi\n"
    )
    healthcheck_service = (
        "[Unit]\n"
        "Description=Range live-ops health check (Quotient + NAT + isolation)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/sbin/range-healthcheck.sh\n"
    )
    healthcheck_timer = (
        "[Unit]\n"
        "Description=Periodically run the range live-ops health check\n"
        "\n"
        "[Timer]\n"
        # Offset from range-firewall.timer's :00/:30-second cadence so the two don't fire in
        # the same tick under systemd's default randomization-free scheduling.
        "OnBootSec=60\n"
        "OnUnitActiveSec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    healthcheck_script_b64 = base64.b64encode(healthcheck_script.encode()).decode()
    healthcheck_service_b64 = base64.b64encode(healthcheck_service.encode()).decode()
    healthcheck_timer_b64 = base64.b64encode(healthcheck_timer.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                f"echo '{healthcheck_script_b64}' | base64 -d | sudo tee /usr/local/sbin/range-healthcheck.sh > /dev/null && "
                "sudo chmod +x /usr/local/sbin/range-healthcheck.sh && "
                f"echo '{healthcheck_service_b64}' | base64 -d | sudo tee /etc/systemd/system/range-healthcheck.service > /dev/null && "
                f"echo '{healthcheck_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/range-healthcheck.timer > /dev/null && "
                "sudo touch /var/log/range-healthcheck.log && "
                "sudo systemctl daemon-reload && sudo systemctl enable --now range-healthcheck.timer"
            ),
        ],
        check=True, timeout=30,
    )
    print("  range-healthcheck.timer enabled (checks every 60s, logs failures only)")


def ensure_nat_forwarding(ctx):
    """Idempotently (re)assert the engine's team-subnet NAT + forwarding + team isolation.

    The scoring engine is every team's NAT gateway to the internet, which nakon needs for
    apt-get. But Docker re-syncs iptables on any container start/restart and drops the custom
    team-subnet MASQUERADE, leaving boxes offline — and nakon swallows the resulting apt
    failures, so services silently don't install. The same resync also drops the team-to-team
    DROP rule (range-firewall.timer re-asserts both every 30s once the range is live, but that
    timer isn't installed until later in bootstrap_scoring_engine() — this call is what covers
    the gap during phases 4-6, before nakon runs). Call this right before any step that needs
    the team boxes online (i.e. before each nakon run). Only the boxes→internet path needs
    NAT; engine→box scoring is direct routing on the team bridge, so this is only about nakon.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    cmd = (
        "sudo iptables -P FORWARD ACCEPT; "
        "sudo iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
        "sudo iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP; "
        "sudo iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null || "
        "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE"
    )
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}", cmd,
        ],
        check=False, timeout=30,
    )
    print("  NAT/forwarding ensured on scoring engine")


def push_event_conf(comp_dir, teams, boxes, ctx, event_name, inject_password=None,
                    admin_password="changeme123", postgres_password="postgres_password",
                    redis_password="redis_password", box_creds=None):
    """Build event.conf and push it to the scoring engine.

    postgres_password / redis_password come from deploy() so the .env rewritten here matches the
    one bootstrap_scoring_engine() wrote — Postgres and the app must agree on the same secret.

    box_creds ({"admin": ..., "user1": ..., "user2": ...}) is the credlist content Quotient's
    Ssh/Smtp/Imap/Sql/Ftp checks authenticate WITH — it has to name accounts that really exist
    on the boxes, i.e. exactly what fix_services_on_boxes() creates there. Falls back to the
    legacy fixed literals only if not supplied (keeps this callable standalone / from tests).
    """
    import base64

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    box_services = json.loads((comp_dir / "box_services.json").read_text())

    # Build the context dict that build_event_conf expects
    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
        "boxes_per_team": boxes,
        "team_passwords": {team_key: team_data["password"] for team_key, team_data in teams.items()},
        "event_name": event_name,
        "quotient_admin_password": admin_password,
        "inject_password": inject_password,
    }

    # Build event.conf from box_services
    event_conf = build_event_conf(quotient_ctx, box_services)
    event_conf_toml = toml.dumps(event_conf)

    # Write event.conf to scoring engine
    event_conf_b64 = base64.b64encode(event_conf_toml.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"echo '{event_conf_b64}' | base64 -d | sudo tee /opt/quotient/config/event.conf",
        ],
        check=True, timeout=30,
    )

    # Write credlist. box_creds names the SAME accounts fix_services_on_boxes() creates on every
    # box — the two have to agree or every credlist-based check (Ssh/Smtp/Imap/Sql/Ftp) scores a
    # healthy box as down.
    creds = box_creds or {"admin": "changeme123", "user1": "password1", "user2": "password2"}
    credlist = "".join(f"{user},{pw}\n" for user, pw in creds.items())
    credlist_b64 = base64.b64encode(credlist.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"mkdir -p /opt/quotient/config/credlists && echo '{credlist_b64}' | base64 -d | sudo tee /opt/quotient/config/credlists/linux.credlist",
        ],
        check=True, timeout=30,
    )

    # Write .env for Quotient (matches docker-compose.yml env_file expectations)
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
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env",
        ],
        check=True, timeout=30,
    )

    # Restart Quotient to pick up new config
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose restart",
        ],
        check=True, timeout=60,
    )

    print("  Event configuration pushed to scoring engine")


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
        # or from fingerprinting a past deploy — see utils.py's BOX_USERNAME_DEFAULT/BOX_PASSWORD
        # comment.
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

            print("  Configuring Windows AD domains (if any)...")
            deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path,
                                           key, scoring_user, scoring_ip, box_password)

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
