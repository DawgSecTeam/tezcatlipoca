# Author: Hamza

import importlib.util
import json
import math
import os
import random
import re
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

from quotient.setup import build_event_conf, create_injects, seed_and_start
from utils import DNS_FIX_CMD, load_compfile, pick_competition

ENV_PATH = Path(".env")
NAKON_DIR = Path("nakon")

# Terraform reads these straight out of the process environment (TF_VAR_<name>) — loading
# .env here also makes them available to the `terraform` subprocess calls below, since
# subprocess inherits the parent's environment by default.
load_dotenv(ENV_PATH)

# Proxmox's API token auth (below) talks straight to the API over the same self-signed cert
# main.tf's provider block sets insecure=true for — same tradeoff, just from Python instead.
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


def proxmox_api(method, path, **kwargs):
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    url = f"{endpoint}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    r = requests.request(method, url, headers=headers, verify=False, timeout=60, **kwargs)
    r.raise_for_status()
    return r.json()


def wait_for_proxmox_task(node, upid, timeout=600):
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Proxmox task {upid} timed out after {timeout}s")
        data = proxmox_api("GET", f"/nodes/{node}/tasks/{upid}/status")["data"]
        if data["status"] == "stopped":
            if data.get("exitstatus") != "OK":
                raise RuntimeError(f"Proxmox task {upid} failed: {data.get('exitstatus')}")
            return
        time.sleep(3)


# VM IDs are 200 + identifier*10 + box_index (mirrored in main.tf's team_box.vm_id), which
# leaves each team a stride of exactly 10. An 11th box would land on the next team's first box
# and silently clobber it, so the scheme caps the box count rather than the picker.
MAX_BOXES_PER_TEAM = 10


def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


def stop_vm(node, vmid):
    # Graceful first so the box's filesystem is consistent for the clone, but never
    # indefinitely: a shutdown is an ACPI power-button event, and a guest without acpid (box
    # templates are only required to have working cloud-init) just ignores it. That used to
    # stall here for the full task timeout and then abort the deploy — after nakon had already
    # run — so fall back to pulling the plug instead.
    try:
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)
    except RuntimeError:
        print(f"    vmid {vmid} ignored ACPI shutdown — forcing stop")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)


def destroy_vm_if_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        return
    if vm.get("status") == "running":
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid)
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}")["data"]
    wait_for_proxmox_task(node, upid)


def destroy_bridge_if_exists(node, bridge_name):
    """Delete a Proxmox Linux bridge if it exists."""
    try:
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
    except Exception:
        pass  # Bridge doesn't exist or already deleted


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
        number_of_boxes = int(input("How many box types for this competition? "))
        if 1 <= number_of_boxes <= MAX_BOXES_PER_TEAM:
            break
        print(f"  Enter a number from 1 to {MAX_BOXES_PER_TEAM} (see MAX_BOXES_PER_TEAM).")

    boxes = []
    for i in range(1, number_of_boxes + 1):
        print(f"\n─── Box {i} of {number_of_boxes} " + "─" * 40)

        name = input("  Name (e.g. web01, db, mail): ").strip()
        while not name:
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

        cpu = int(input("  CPU cores    [1]: ").strip() or 1)
        memory_mb = int(input("  Memory (MB)  [2048]: ").strip() or 2048)
        # Blank keeps the template's own disk. Terraform only emits a disk block when this is
        # set (main.tf), because Proxmox cannot shrink a disk — a value below the template's
        # own size fails the clone.
        disk_raw = input("  Disk (GB)    [keep template's]: ").strip()

        box = {
            "name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb,
            "disk_gb": int(disk_raw) if disk_raw else None, "template": template,
        }
        boxes.append(box)
    return boxes


def load_injects(comp_dir):
    """Load per-competition injects from competitions/<id>/injects/.

    Each inject is a subdirectory containing `inject.json`:
        {
          "title": "...", "description": "...",   # description may instead live in a
          "description_file": "prompt.md",         # sibling file (markdown), optional
          "open_offset_min": 0, "due_offset_min": 60, "close_offset_min": 90
        }
    Any other files in the subdirectory (e.g. a template .docx, a PoC) are uploaded as the
    inject's attachments. Offsets are minutes relative to competition start (now); they're
    resolved to RFC3339 timestamps here so Quotient's CreateInject can parse them directly.
    Returns [] when there's no injects/ dir, so competitions without injects are unaffected.
    """
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return []

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

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

        def rfc3339(minutes):
            return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Attachments: every file in the folder except the manifest / description source.
        skip = {"inject.json", meta.get("description_file")}
        files = [str(f) for f in sorted(sub.iterdir()) if f.is_file() and f.name not in skip]

        injects.append({
            "title":       meta["title"],
            "description": description,
            "open_time":   rfc3339(meta.get("open_offset_min", 0)),
            "due_time":    rfc3339(meta.get("due_offset_min", 60)),
            "close_time":  rfc3339(meta.get("close_offset_min", 90)),
            "files":       files,
        })
    return injects


def load_boxes(comp_dir):
    # Saved by this script at competition-creation time (see boxes.json below) — reusing a
    # competition replays the exact boxes it was built with, not whatever's currently in .env.
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


def generate_nakon_config(teams, boxes, difficulty, comp_dir):
    # nakon/ is a symlinked sibling repo (no __init__.py), so load its script
    # directly to reuse the same DB-driven service/vuln picker it uses itself.
    spec = importlib.util.spec_from_file_location("nakon_randomize_config", NAKON_DIR / "randomize_config.py")
    nr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nr)

    load_dotenv(NAKON_DIR / ".env")
    mydb = nr.mysql.connector.connect(
        host=os.getenv("host"), user=os.getenv("user"),
        password=os.getenv("password"), database=os.getenv("database"),
    )
    cursor = mydb.cursor()
    name_to_row = nr.load_configurations(cursor)
    cursor.close()
    mydb.close()

    # Services that are legitimate in the catalog but too heavy/slow for an automated apply
    # (splunk pulls a ~500 MB installer per box; roundcube drags in apache+mariadb+php). They
    # can still be assigned by hand via box_services.json — they're only excluded from the
    # random auto-pick so a hands-off deploy stays fast and reliable.
    SLOW_SERVICES = {"splunk", "roundcube"}
    for svc in SLOW_SERVICES:
        name_to_row.pop(svc, None)

    services_path = comp_dir / "box_services.json"

    # Deterministic re-runs: if this competition already has a box_services.json, honour it
    # instead of re-randomising. Lets an operator pin an exact service set (and makes reusing a
    # competition reproduce the same boxes, which is the documented intent for boxes-per-event).
    if services_path.exists():
        pinned = json.loads(services_path.read_text())
        # Optional companion file pins the misconfigs/vulns nakon plants per box. box_services.json
        # only holds the scoreable services (that's all Quotient needs); vulns live here so a
        # pinned competition can still deploy misconfigurations rather than services alone.
        vulns_path = comp_dir / "box_vulns.json"
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes
        }
        print(f"  Using pinned services from {services_path}")
    else:
        # Randomize once per box type so every team defends the same service set,
        # which lets Quotient use its 192.168._.N wildcard IP pattern uniformly.
        box_configs = {}
        for box in boxes:
            platform = nr.os_to_platform(box["template"])
            services, vulns, _ = nr.pick_configurations(
                name_to_row, platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            box_configs[box["name"]] = (services, vulns)

        # Persist just the scoreable services so push_event_conf() can build
        # Quotient checks without re-querying the DB on subsequent runs.
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        services, vulns = box_configs[box["name"]]
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": "ubuntu",
            "password": "ubuntu",
            "configurations": services + vulns,
        })

    (NAKON_DIR / "config.json").write_text(json.dumps({"machines": machines}, indent=2))


def read_terraform_ctx():
    raw = subprocess.run(
        ["terraform", "output", "-json"], cwd="terraform", capture_output=True, text=True, check=True
    ).stdout
    ctx = json.loads(json.loads(raw)["agent_context"]["value"])
    # ssh_key_path is relative to terraform/ (where Terraform's file() resolves it) —
    # resolve it to an absolute path so subprocess calls from the project root work.
    key_path = ctx["ssh_key_path"]
    if not os.path.isabs(key_path):
        ctx = {**ctx, "ssh_key_path": str((Path("terraform") / key_path).resolve())}
    return ctx


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60):
    """SSH to a target box via the scoring engine gateway.

    The team boxes have 'ubuntu' user with the proxmox key authorized (set by cloud-init).
    We use ProxyCommand through the scoring engine with OpenSSH's built-in -W (direct-stream-local).
    Requires AllowTcpForwarding=yes on the gateway sshd (set in bootstrap_scoring_engine).
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", f"ProxyCommand={proxy}",
            f"ubuntu@{target_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def ssh_on_gateway(ctx, cmd, timeout=30):
    """Run a command directly on the scoring engine gateway."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            f"{scoring_user}@{scoring_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def wait_for_ssh(key, user, host, timeout=300):
    """Poll a host's SSH until it accepts a command, replacing a blind post-boot sleep.

    Returns True once `ssh user@host true` succeeds, False on timeout. Never raises — a
    timeout prints a warning and the caller proceeds (matching the surrounding "continue
    anyway" error posture), because the later steps have their own retry loops.
    """
    print(f"  Waiting for {host} to accept SSH (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["ssh", "-i", key,
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=10",
                 "-o", "BatchMode=yes",
                 f"{user}@{host}", "true"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                print(f"    {host} reachable via SSH (after {attempt} attempt(s))")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    print(f"  WARNING: {host} not reachable via SSH within {timeout}s — continuing anyway")
    return False


def wait_for_boxes_ssh(ctx, teams, boxes, timeout=300):
    """Poll every team box's SSH reachability THROUGH the gateway before the DNS/harden loops.

    Replaces a blind post-clone sleep. Uses one shared time budget across all boxes (they boot
    together, so once cloud-init finishes they come up nearly at once). Never raises — the
    fix_dns/setup_auth steps that follow already retry, so a timeout just warns and proceeds.
    """
    print("  Waiting for team boxes to accept SSH via gateway...")
    deadline = time.time() + timeout
    for team in teams.values():
        for box in boxes:
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            while True:
                try:
                    r = ssh_via_gateway(ctx, ip, "true", timeout=20)
                    if r.returncode == 0:
                        print(f"    {ip} reachable")
                        break
                except Exception:
                    pass
                if time.time() > deadline:
                    print(f"    WARNING: {ip} not reachable within timeout — continuing")
                    break
                time.sleep(10)


def wait_for_http(url, timeout=120):
    """Poll a URL until it returns ANY HTTP response (not connection-refused), replacing a
    blind sleep before seeding. requests.get returns for 4xx/5xx too (we don't raise_for_status),
    so any served response means the app is up. Never raises — timeout warns and proceeds.
    """
    print(f"  Waiting for {url} to respond (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            requests.get(url, timeout=5)
            print(f"    {url} responded (after {attempt} attempt(s))")
            return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(3)
    print(f"  WARNING: {url} did not respond within {timeout}s — continuing anyway")
    return False


def fix_dns_on_boxes(teams, boxes, ctx):
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    # Terraform's Step D already did this once before nakon ran; it has to happen again here
    # because clone_team_boxes()' `cloud-init clean` + reboot regenerates resolv.conf.
    print("  Fixing DNS on all team boxes...")
    for team in teams.values():
        for box in boxes:
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            for attempt in range(1, 9):
                try:
                    subprocess.run(
                        [
                            "ssh", "-i", key,
                            "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=10",
                            "-o", f"ProxyCommand={proxy}",
                            f"ubuntu@{ip}", DNS_FIX_CMD,
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


def fix_services_on_boxes(comp_dir, teams, boxes, ctx):
    """Post-Nakon service hardening: make services accessible externally.

    Writes a single hardening script to the gateway, then distributes it to each target box.
    Handles: mysql/mariadb bind address, postfix, nginx, vsftpd, dovecot, bind9.
    Also starts all services on every box.
    """
    import base64

    print("  Hardening services on all team boxes...")
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    # Build a map of which services each box should run
    box_service_map = {}
    for box in boxes:
        box_service_map[box["name"]] = box_services.get(box["name"], [])

    for team in teams.values():
        for box in boxes:
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            services = box_service_map.get(box["name"], [])

            # Build a per-box hardening script
            script_lines = ["#!/bin/bash", "set -e", ""]

            # Always create the credlist OS accounts. Quotient's Ssh/Smtp/Imap/Ftp login checks
            # authenticate against these system users; previously they were only created in the
            # postfix branch, so a box that ran ssh (or ftp/imap) but not postfix had no account
            # to log in as and scored permanently down. Creating them unconditionally is cheap
            # and idempotent, and matches the linux.credlist push_event_conf() writes.
            script_lines.extend([
                "# Credlist OS accounts (admin/user1/user2) for all auth-based service checks",
                "sudo useradd -m -s /bin/bash admin 2>/dev/null || true",
                "echo 'admin:changeme123' | sudo chpasswd",
                "sudo useradd -m -s /bin/bash user1 2>/dev/null || true",
                "echo 'user1:password1' | sudo chpasswd",
                "sudo useradd -m -s /bin/bash user2 2>/dev/null || true",
                "echo 'user2:password2' | sudo chpasswd",
                "",
            ])

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
                    "CREATE USER IF NOT EXISTS 'admin'@'%' IDENTIFIED BY 'changeme123';",
                    "GRANT ALL PRIVILEGES ON *.* TO 'admin'@'%' WITH GRANT OPTION;",
                    "CREATE USER IF NOT EXISTS 'user1'@'%' IDENTIFIED BY 'password1';",
                    "GRANT ALL PRIVILEGES ON *.* TO 'user1'@'%';",
                    "CREATE USER IF NOT EXISTS 'user2'@'%' IDENTIFIED BY 'password2';",
                    "GRANT ALL PRIVILEGES ON *.* TO 'user2'@'%';",
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
                    "sudo systemctl restart postfix 2>/dev/null || true",
                    "sleep 1",
                    "# Create mail users matching credlist for SMTP checks",
                    "sudo useradd -m -s /bin/bash admin 2>/dev/null || true",
                    "echo 'admin:changeme123' | sudo chpasswd",
                    "sudo useradd -m -s /bin/bash user1 2>/dev/null || true",
                    "echo 'user1:password1' | sudo chpasswd",
                    "sudo useradd -m -s /bin/bash user2 2>/dev/null || true",
                    "echo 'user2:password2' | sudo chpasswd",
                    "",
                ])

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
                    "sudo mkdir -p /home/user1/mail /home/user2/mail",
                    "sudo chmod 700 /home/user1/mail /home/user2/mail",
                    "sudo chown user1:user1 /home/user1/mail 2>/dev/null || true",
                    "sudo chown user2:user2 /home/user2/mail 2>/dev/null || true",
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

            if len(script_lines) <= 3:  # Only shebang, set -e, and empty line
                print(f"    No hardening needed on {ip}")
                continue

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
                result = ssh_via_gateway(ctx, ip, deploy_cmd, timeout=60)
                if result.returncode != 0:
                    print(f"    Service hardening warning on {ip}: {result.stderr.strip()[:200]}")
                else:
                    print(f"    Services hardened on {ip}")
            except Exception as e:
                print(f"    Service hardening error on {ip}: {e}")


def setup_ubuntu_auth(teams, boxes, ctx):
    """Enable password auth and NOPASSWD sudo for ubuntu on team boxes.

    The template has PasswordAuthentication disabled, but Nakon's paramiko
    connections use password auth (ubuntu/ubuntu). Also, Nakon runs sudo
    commands to install packages, so NOPASSWD is required.

    Runs via the scoring engine gateway since team boxes are on isolated bridges.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )

    print("  Enabling password auth + NOPASSWD sudo for ubuntu on team boxes...")
    auth_cmd = (
        "sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo systemctl restart sshd 2>/dev/null || true; "
        "echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ubuntu; "
        "sudo chmod 440 /etc/sudoers.d/ubuntu"
    )

    for team in teams.values():
        for box in boxes:
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            for attempt in range(1, 9):
                try:
                    subprocess.run(
                        [
                            "ssh", "-i", key,
                            "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=10",
                            "-o", f"ProxyCommand={proxy}",
                            f"ubuntu@{ip}", auth_cmd,
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


def clone_team_boxes(teams, boxes, ctx, comp_dir):
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

    # Step 1: Run cloud-init clean on team1 boxes
    print("  Running cloud-init clean on team1 boxes...")
    for box in boxes:
        ip = f"192.168.{team1['identifier']}.{box['last_octet']}"
        try:
            result = ssh_via_gateway(ctx, ip, "sudo cloud-init clean --logs --machine-id", timeout=30)
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
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
            wait_for_proxmox_task(node, upid)
        print(f"    team1-{box['name']} (vmid {vmid}) stopped")

    # Step 3: Clone team1 boxes for each subsequent team
    print("  Cloning team1 boxes to other teams...")
    # Record cloned VM ids so destroy-competition.py can tear them down — these are
    # created directly via the Proxmox API, so they're NOT in Terraform state and
    # `terraform destroy` won't remove them.
    cloned_vms = {}
    for team in team_ids[1:]:
        for box_idx, box in enumerate(boxes):
            src_vmid = vm_id_for(team1["identifier"], box_idx)
            dst_vmid = vm_id_for(team["identifier"], box_idx)

            clone_name = f"{team['identifier']}-{box['name']}"
            cloned_vms[clone_name] = dst_vmid
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src_vmid}/clone", data={
                "newid": dst_vmid,
                "name": clone_name,
                "full": 1,
            })["data"]
            print(f"    team1-{box['name']} (vmid {src_vmid}) -> {clone_name} (vmid {dst_vmid})...")
            wait_for_proxmox_task(node, upid)

            # Fix cloud-init IP for the cloned VM (last_octet is correct for IPs)
            team_subnet = team["identifier"]
            box_octet = box["last_octet"]
            ipconfig = f"ip=192.168.{team_subnet}.{box_octet}/24,gw=192.168.{team_subnet}.1"
            bridge = f"vmbr{team_subnet}"
            # `ipconfig0` is the cloud-init IP key; there is no `ciipconfig0` param and
            # including it makes Proxmox reject the whole request (400 Parameter verification
            # failed). Let Proxmox auto-assign the NIC MAC rather than pinning 00:00:00:00:00:00.
            proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                "ipconfig0": ipconfig,
                "net0": f"virtio,bridge={bridge}",
            })

    (comp_dir / "cloned_vms.json").write_text(json.dumps(cloned_vms, indent=2))

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

    # Wait for cloud-init to finish on all boxes. Poll each cloned box's SSH THROUGH the
    # gateway instead of a blind sleep; the fix_dns/auth loops below still retry on their own.
    wait_for_boxes_ssh(ctx, teams, boxes, timeout=300)

    # Step 5: Fix DNS on ALL team boxes
    fix_dns_on_boxes(teams, boxes, ctx)

    # Step 6: Setup ubuntu auth on ALL team boxes (cloned VMs may need it reset)
    setup_ubuntu_auth(teams, boxes, ctx)

    # Step 7: Harden services on ALL team boxes
    fix_services_on_boxes(comp_dir, teams, boxes, ctx)


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

    # CRITICAL: Docker sets FORWARD policy to DROP on start. Restore forwarding rules
    # so the scoring engine can route between team subnets and the internet.
    # Also enable TCP forwarding on sshd for ProxyCommand tunneling.
    print("  Restoring network forwarding rules after Docker start...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                # Allow all forwarding within team subnets and NAT to internet
                "sudo iptables -P FORWARD ACCEPT && "
                "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE && "
                # Enable TCP forwarding for ProxyCommand tunnels
                "sudo sed -i 's/^#*AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config && "
                "sudo systemctl reload sshd 2>/dev/null || true"
            ),
        ],
        check=True, timeout=15,
    )
    print("  Forwarding rules restored")

    # Make the team-subnet NAT durable. The rules restored just above are wiped every time
    # Docker re-syncs iptables (any container start/restart), silently cutting team boxes off
    # the internet. ensure_nat_forwarding() only re-asserts before nakon runs — not good enough
    # once the range is live. Install a tiny idempotent systemd oneshot + a 30s timer ON THE
    # ENGINE so the rules are continuously re-asserted for the lifetime of the range.
    print("  Installing quotient-nat systemd unit + timer (keeps team NAT durable)...")
    nat_script = (
        "#!/bin/bash\n"
        "iptables -P FORWARD ACCEPT\n"
        "iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null || "
        "iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE\n"
    )
    nat_service = (
        "[Unit]\n"
        "Description=Re-assert Quotient team-subnet NAT/forwarding (Docker wipes it on restart)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/sbin/quotient-nat.sh\n"
    )
    nat_timer = (
        "[Unit]\n"
        "Description=Periodically re-assert Quotient team-subnet NAT/forwarding\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=30\n"
        "OnUnitActiveSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    nat_script_b64 = base64.b64encode(nat_script.encode()).decode()
    nat_service_b64 = base64.b64encode(nat_service.encode()).decode()
    nat_timer_b64 = base64.b64encode(nat_timer.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                f"echo '{nat_script_b64}' | base64 -d | sudo tee /usr/local/sbin/quotient-nat.sh > /dev/null && "
                "sudo chmod +x /usr/local/sbin/quotient-nat.sh && "
                f"echo '{nat_service_b64}' | base64 -d | sudo tee /etc/systemd/system/quotient-nat.service > /dev/null && "
                f"echo '{nat_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/quotient-nat.timer > /dev/null && "
                "sudo systemctl daemon-reload && sudo systemctl enable --now quotient-nat.timer"
            ),
        ],
        check=True, timeout=30,
    )
    print("  quotient-nat.timer enabled (re-asserts NAT every 30s)")

    # Configure team bridge NICs so the scoring engine can reach team subnets.
    # The scoring engine has multiple virtio NICs (one per team bridge) but only
    # the management NIC (eth0) is configured by default. Additional NICs are
    # added by Terraform after clone, requiring a reboot for the guest to detect them.
    print("  Configuring team bridge interfaces...")
    teams_info = json.loads(
        subprocess.run(
            ["terraform", "output", "-json"],
            cwd="terraform", capture_output=True, text=True, check=True
        ).stdout
    )["teams"]["value"]
    eth_lines = ["    eth0:\n      dhcp4: true\n"]
    for team_key, identifier in teams_info.items():
        iface = f"ens{18 + int(identifier)}"
        eth_lines.append(f"    {iface}:\n      addresses: [192.168.{identifier}.1/24]\n      dhcp4: false\n")
    netplan_yaml = "network:\n  version: 2\n  ethernets:\n" + "".join(eth_lines)
    # Write netplan config, then reboot so new virtio NICs are detected and configured.
    result = subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                f"printf '{netplan_yaml}' | sudo tee /etc/netplan/02-team-bridges.yaml && "
                "sudo chmod 600 /etc/netplan/02-team-bridges.yaml && "
                "sudo netplan apply 2>&1 && echo 'Team bridge interfaces configured'"
            ),
        ],
        capture_output=True, text=True, timeout=20,
    )
    # Check if reboot is needed (interfaces not yet detected)
    if "ens19" not in result.stdout or result.returncode != 0:
        print("  Rebooting scoring engine to detect additional virtio NICs...")
        subprocess.run(
            [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                f"{scoring_user}@{scoring_ip}",
                "sudo reboot",
            ],
            capture_output=True, text=True, timeout=10,
        )
        # Wait for the scoring engine to come back online
        print("  Waiting for scoring engine to reboot...")
        for attempt in range(1, 31):
            time.sleep(10)
            try:
                r = subprocess.run(
                    ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null",
                     "-o", "ConnectTimeout=5",
                     f"{scoring_user}@{scoring_ip}", "echo alive"],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode == 0:
                    print(f"  Scoring engine back online after {attempt * 10}s")
                    break
            except subprocess.TimeoutExpired:
                pass
        else:
            print("  WARNING: Scoring engine did not come back online within 5 minutes")
        # Apply netplan after reboot
        subprocess.run(
            [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                f"{scoring_user}@{scoring_ip}",
                "sudo netplan apply 2>&1 && echo 'Team bridge interfaces configured'",
            ],
            check=True, timeout=20,
        )
    print("  Team bridge interfaces configured")


def ensure_nat_forwarding(ctx):
    """Idempotently (re)assert the engine's team-subnet NAT + forwarding.

    The scoring engine is every team's NAT gateway to the internet, which nakon needs for
    apt-get. But Docker re-syncs iptables on any container start/restart and drops the custom
    team-subnet MASQUERADE, leaving boxes offline — and nakon swallows the resulting apt
    failures, so services silently don't install. Call this right before any step that needs
    the team boxes online (i.e. before each nakon run). Only the boxes→internet path needs
    NAT; engine→box scoring is direct routing on the team bridge, so this is only about nakon.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    cmd = (
        "sudo iptables -P FORWARD ACCEPT; "
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
                    redis_password="redis_password"):
    """Build event.conf and push it to the scoring engine.

    postgres_password / redis_password come from deploy() so the .env rewritten here matches the
    one bootstrap_scoring_engine() wrote — Postgres and the app must agree on the same secret.
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

    # Write credlist
    credlist = "admin,changeme123\nuser1,password1\nuser2,password2\n"
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
    comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {comp_name}")
    print(f"{'='*60}\n")

    boxes = load_boxes(comp_dir)
    if not boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)

    # Resumable-phase state. Secrets (admin/inject/postgres/redis) and the team set are per-run;
    # a resume MUST reuse the originals or the engine's already-written .env / already-seeded
    # admin login won't match. Persist them here (gitignored, mode 0600) and reload on resume.
    state_path = comp_dir / ".deploy_state.json"
    resuming = from_phase > 1 and state_path.exists()

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
        inject_password = state.get("inject_password")
        print(f"  Resuming from phase {from_phase} "
              f"({number_of_teams} team(s), last completed phase {state.get('last_phase')})")
    else:
        if num_teams is not None:
            number_of_teams = num_teams
        else:
            number_of_teams = int(input("How many teams? "))
        teams = collect_teams(number_of_teams)
        # Per-competition Quotient web-admin password (scoreboard/admin login only). Postgres and
        # Redis passwords for the Quotient stack are generated once here and passed to both
        # bootstrap_scoring_engine() and push_event_conf() so the two .env writes agree. The box
        # service credlist (admin/changeme123 …) is a separate thing the checks authenticate WITH
        # and is left untouched.
        admin_password = random_password()
        postgres_password = random_password()
        redis_password = random_password()
        inject_password = random_password() if injects else None
        state = {
            "last_phase": 0,
            "teams": teams,
            "admin_password": admin_password,
            "inject_password": inject_password,
            "postgres_password": postgres_password,
            "redis_password": redis_password,
        }
        _save_state()

    # Generate Nakon config
    generate_nakon_config(teams, boxes, difficulty, comp_dir)

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
    # Tracks the phase currently executing so the failure handler can tell the operator exactly
    # where to resume from.
    current_phase = max(from_phase, 1)
    try:
        # [1/7] Clean up previous deployment (DESTRUCTIVE — skipped on resume)
        if from_phase <= 1:
            current_phase = 1
            print("[1/7] Cleaning up previous deployment...")
            for team in teams.values():
                # vm_id_for takes the 0-based box index (matching main.tf and clone_team_boxes),
                # NOT last_octet — passing last_octet computed the wrong vmids, so cleanup never
                # actually removed the previous run's team boxes and they lingered to collide with
                # the next run.
                for box_idx, _box in enumerate(boxes):
                    destroy_vm_if_exists(node, vm_id_for(team["identifier"], box_idx))
            # Destroy scoring engine (vmid hardcoded in main.tf)
            destroy_vm_if_exists(node, 1000)
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
            # -parallelism=1 serializes the clones: full-cloning the scoring engine and every team
            # box at once saturates the datastore and the Proxmox API starts returning HTTP 596
            # (timeout), failing the apply. Cloning one VM at a time is slower but reliable
            # (main.tf assumes this).
            subprocess.run(["terraform", "apply", "-auto-approve", "-parallelism=1"], cwd="terraform", check=True, timeout=2400)

            # Poll the engine's SSH reachability instead of a blind post-apply sleep.
            apply_ctx = read_terraform_ctx()
            wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                         apply_ctx["scoring_engine_ip"], timeout=300)
            checkpoint(2)
        else:
            print("[2/7] Skipped (resume) — not re-running terraform apply.")

        # Shared setup needed by every phase from [3/7] on. Runs even when phase 3 itself is
        # skipped, because phases 4–7 all reference key/ctx/scoring_ip/scoring_user.
        key = Path("terraform") / os.environ.get("TF_VAR_ssh_key_path", "proxmox")
        if not key.exists():
            key = Path("proxmox")
        scoring_user = os.environ["TF_VAR_vm_username"]
        ctx = read_terraform_ctx()
        scoring_ip = ctx["scoring_engine_ip"]

        # [3/7] Copy SSH key to scoring engine
        if from_phase <= 3:
            current_phase = 3
            print("[3/7] Copying SSH key to scoring engine...")
            subprocess.run(
                [
                    "scp", "-i", str(key),
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    str(key),
                    f"{scoring_user}@{scoring_ip}:/home/sysadmin/.ssh/proxmox_key",
                ],
                check=True, timeout=15,
            )
            subprocess.run(
                [
                    "ssh", "-i", str(key),
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    f"{scoring_user}@{scoring_ip}",
                    "sudo chmod 600 /home/sysadmin/.ssh/proxmox_key",
                ],
                check=True, timeout=10,
            )
            checkpoint(3)
        else:
            print("[3/7] Skipped (resume).")

        # [4/7] Bootstrap scoring engine
        if from_phase <= 4:
            current_phase = 4
            print("[4/7] Bootstrapping scoring engine (packages, Docker, Quotient)...")
            bootstrap_scoring_engine(ctx, postgres_password, redis_password)

            # Push event.conf now, BEFORE nakon. Quotient's server panics on every scoring round
            # while event.conf is absent, and each container restart re-syncs iptables and wipes
            # the team NAT rule — which starves nakon's apt-get of internet. Writing a valid
            # event.conf here stops the crash loop so NAT stays up through the nakon runs.
            # event.conf uses the 192.168._.N wildcard and lists every team, so it's complete even
            # though team2+ boxes don't exist yet.
            print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
            push_event_conf(comp_dir, teams, boxes, ctx, name,
                            inject_password=inject_password, admin_password=admin_password,
                            postgres_password=postgres_password, redis_password=redis_password)
            ensure_nat_forwarding(ctx)

            # [4.5/7] Enable password auth + NOPASSWD sudo for ubuntu on team1 boxes
            print("[4.5/7] Enabling password auth + NOPASSWD sudo for ubuntu on team1 boxes...")
            # Only team1 exists at this point (team2+ are cloned later)
            team1_only = {k: v for k, v in teams.items() if k == "team1"}
            setup_ubuntu_auth(team1_only, boxes, ctx)
            checkpoint(4)
        else:
            print("[4/7] Skipped (resume).")

        # [5/7] Fix DNS on team1 boxes (needed for apt-get in Nakon) + run Nakon deployment
        if from_phase <= 5:
            current_phase = 5
            print("[5/7] Fixing DNS on team1 boxes, then running Nakon deployment...")
            # Only team1 exists at this point — fix DNS so apt-get can resolve repos
            team1_only = {k: v for k, v in teams.items() if k == "team1"}
            fix_dns_on_boxes(team1_only, boxes, ctx)

            # Generate team1-only config for Nakon (team2+ don't exist yet)
            nakon_config = json.loads((NAKON_DIR / "config.json").read_text())
            team1_identifier = teams["team1"]["identifier"]
            nakon_config_team1 = {"machines": [
                m for m in nakon_config["machines"] if m["ip"].split(".")[2] == str(team1_identifier)
            ]}
            (NAKON_DIR / "config.json").write_text(json.dumps(nakon_config_team1, indent=2))

            # Make sure the boxes can still reach the internet right before nakon's apt-get runs.
            ensure_nat_forwarding(ctx)

            # Copy Nakon files to gateway
            subprocess.run(
                [
                    "scp", "-i", str(key),
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    str(NAKON_DIR / "deploy.py"),
                    str(NAKON_DIR / "configurations.py"),
                    str(NAKON_DIR / ".env"),
                    str(NAKON_DIR / "config.json"),
                    f"{scoring_user}@{scoring_ip}:/tmp/nakon/",
                ],
                check=True, timeout=30,
            )
            # Install Nakon dependencies and run deployment
            subprocess.run(
                [
                    "ssh", "-i", str(key),
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    f"{scoring_user}@{scoring_ip}",
                    "sudo mkdir -p /opt/nakon && sudo cp /tmp/nakon/* /tmp/nakon/.env /opt/nakon/ && "
                    "sudo pip3 install --break-system-packages 'mysql-connector-python>=8.3.0' paramiko python-dotenv requests 2>/dev/null && "
                    "cd /opt/nakon && sudo python3 deploy.py",
                ],
                check=True, timeout=2400,
            )
            # Restore full config for team2+ Nakon after cloning
            (NAKON_DIR / "config.json").write_text(json.dumps(nakon_config, indent=2))
            print("  Nakon deployment complete")
            checkpoint(5)
        else:
            print("[5/7] Skipped (resume).")

        # [6/7] Clone team1 boxes to other teams, fix DNS, harden services
        if from_phase <= 6:
            current_phase = 6
            print("[6/7] Cloning team1 boxes to other teams, fixing DNS, hardening services...")
            clone_team_boxes(teams, boxes, ctx, comp_dir)

            # Deploy Nakon on team2+ boxes (team1 already done at [5/7])
            if len(teams) > 1:
                print("  Deploying Nakon on team2+ boxes...")
                ensure_nat_forwarding(ctx)
                # Copy full Nakon config to gateway
                subprocess.run(
                    [
                        "scp", "-i", str(key),
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        str(NAKON_DIR / "deploy.py"),
                        str(NAKON_DIR / "configurations.py"),
                        str(NAKON_DIR / ".env"),
                        str(NAKON_DIR / "config.json"),
                        f"{scoring_user}@{scoring_ip}:/tmp/nakon/",
                    ],
                    check=True, timeout=30,
                )
                subprocess.run(
                    [
                        "ssh", "-i", str(key),
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        f"{scoring_user}@{scoring_ip}",
                        "sudo cp /tmp/nakon/* /tmp/nakon/.env /opt/nakon/ && "
                        "cd /opt/nakon && sudo python3 deploy.py",
                    ],
                    check=True, timeout=2400,
                )
                print("  Nakon deployment on team2+ complete")
            else:
                # Single team: clone_team_boxes() returns early without hardening services or
                # creating the credlist OS accounts (admin/user1/user2), so auth checks would
                # score down. Run that step here for the one-team case.
                print("  Single team — hardening services on team1 boxes...")
                fix_services_on_boxes(comp_dir, teams, boxes, ctx)
            checkpoint(6)
        else:
            print("[6/7] Skipped (resume).")

        # [7/7] Seed competition and create injects (event.conf was pushed at [4/7])
        current_phase = 7
        print("[7/7] Seeding competition and creating injects...")

        # Poll Quotient's HTTP endpoint instead of a blind sleep before seeding.
        wait_for_http(f"http://{scoring_ip}/api/login", timeout=120)

        # Seed teams and start competition
        print("  Seeding teams and starting the competition clock...")
        quotient_ctx = {
            "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
            "quotient_admin_password": admin_password,
        }
        seed_and_start(scoring_ip, quotient_ctx)

        # Create injects via Quotient's API (competition must be seeded/started first)
        if injects:
            print(f"  Creating {len(injects)} inject(s)...")
            create_injects(scoring_ip, admin_password, injects)
        checkpoint(7)
    except BaseException:
        print(f"\n  [!] Deploy failed during phase {current_phase} of '{comp_name}'.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {comp_name} --from-phase {current_phase} --yes")
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
    parser.add_argument("--from-phase", type=int, default=1, dest="from_phase",
                        help="Resume from this phase (>1 skips the destructive cleanup + terraform "
                             "apply). See the resume hint printed on a failed deploy.")
    args = parser.parse_args()

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
            difficulty = args.difficulty if args.difficulty is not None else int(input("Difficulty (1-10): "))
            (comp_dir / "Compfile").write_text(
                f"name {comp_name}\n"
                f"scenario {scenario}\n"
                f"difficulty {difficulty}\n"
            )
            boxes = collect_boxes()
            (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))

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
        difficulty = int(input("Difficulty (1-10): "))

        # Write Compfile in key=value format (matching utils.load_compfile)
        (comp_dir / "Compfile").write_text(
            f"name {comp_name}\n"
            f"scenario {scenario}\n"
            f"difficulty {difficulty}\n"
        )

        boxes = collect_boxes()
        (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))
    else:
        idx = int(choice) - 1
        if 0 <= idx < len(previous):
            comp_name = previous[idx]
        else:
            print("Invalid choice.")
            sys.exit(1)
        comp_dir = Path("competitions") / comp_name

    # Pass through --teams/--yes/--from-phase so they still work in interactive mode; they're
    # None/False/1 by default, giving exactly the prior interactive behavior.
    deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase)


if __name__ == "__main__":
    main()
