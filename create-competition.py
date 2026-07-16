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

from quotient.setup import build_event_conf, seed_and_start
from utils import load_compfile, pick_competition

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


def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


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

    number_of_boxes = int(input("How many box types for this competition? "))
    boxes = []
    for i in range(1, number_of_boxes + 1):
        print(f"\n─── Box {i} of {number_of_boxes} " + "─" * 40)

        name = input("  Name (e.g. web01, db, mail): ").strip()
        while not name:
            name = input("  Name can't be blank: ").strip()

        if templates:
            while True:
                raw = input(f"  Template [{1}–{len(templates)}]: ").strip()
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(templates):
                        template = templates[idx - 1]
                        break
                except ValueError:
                    pass
                print(f"  Enter a number from 1 to {len(templates)}.")
        else:
            template = input("  Template name: ").strip()
            while not template:
                template = input("  Template can't be blank — must match a tagged Proxmox VM exactly: ").strip()

        cpu = int(input("  CPU cores    [1]: ").strip() or 1)
        memory_mb = int(input("  Memory (MB)  [2048]: ").strip() or 2048)

        box = {"name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb, "template": template}
        boxes.append(box)
    return boxes


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
    (comp_dir / "box_services.json").write_text(
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


def fix_dns_on_boxes(teams, boxes, ctx):
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    dns_cmd = (
        'sudo chattr -i /etc/resolv.conf 2>/dev/null; '
        'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf; '
        'printf "nameserver 8.8.8.8\\n" | sudo tee /etc/resolv.conf.head; '
        "sudo mkdir -p /etc/systemd/resolved.conf.d; "
        'printf "[Resolve]\\nDNS=8.8.8.8\\n" | sudo tee /etc/systemd/resolved.conf.d/upstream.conf; '
        "sudo systemctl restart systemd-resolved 2>/dev/null || true"
    )
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
                            f"ubuntu@{ip}", dns_cmd,
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
                    "sudo mysql < /tmp/setup_mysql.sql",
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
    for team in team_ids[1:]:
        for box_idx, box in enumerate(boxes):
            src_vmid = vm_id_for(team1["identifier"], box_idx)
            dst_vmid = vm_id_for(team["identifier"], box_idx)

            clone_name = f"{team['identifier']}-{box['name']}"
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
            proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                "ciipconfig0": ipconfig,
                "ipconfig0": ipconfig,
                "net0": f"virtio=00:00:00:00:00:00,bridge={bridge}",
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

    # Wait for cloud-init to finish on all boxes
    print("  Waiting for VMs to initialize (60s)...")
    time.sleep(60)

    # Step 5: Fix DNS on ALL team boxes
    fix_dns_on_boxes(teams, boxes, ctx)

    # Step 6: Setup ubuntu auth on ALL team boxes (cloned VMs may need it reset)
    setup_ubuntu_auth(teams, boxes, ctx)

    # Step 7: Harden services on ALL team boxes
    fix_services_on_boxes(comp_dir, teams, boxes, ctx)


def bootstrap_scoring_engine(ctx):
    """Bootstrap the scoring engine: install packages, Docker, Quotient."""
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
        "POSTGRES_PASSWORD=postgres_password\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        "REDIS_PASSWORD=redis_password\n"
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
        check=True, timeout=300,
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


def push_event_conf(comp_dir, teams, boxes, ctx, event_name):
    """Build event.conf and push it to the scoring engine."""
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
        "quotient_admin_password": "changeme123",
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
        "POSTGRES_PASSWORD=postgres_password\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        "REDIS_PASSWORD=redis_password\n"
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


def deploy(comp_dir):
    """Main deployment pipeline."""
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

    number_of_teams = int(input("How many teams? "))
    teams = collect_teams(number_of_teams)

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

    # Confirm deployment
    if not confirm_deploy(name, scenario, difficulty, teams, boxes):
        print("  Deployment cancelled.")
        return

    # [1/7] Clean up previous deployment
    print("[1/7] Cleaning up previous deployment...")
    node = os.environ["TF_VAR_proxmox_node"]
    for team in teams.values():
        for box in boxes:
            destroy_vm_if_exists(node, vm_id_for(team["identifier"], box["last_octet"]))
    # Destroy scoring engine (vmid hardcoded in main.tf)
    destroy_vm_if_exists(node, 1000)
    # Destroy bridges
    for team in teams.values():
        destroy_bridge_if_exists(node, f"vmbr{team['identifier']}")
    time.sleep(5)

    # [2/7] Terraform init & apply
    print("[2/7] Running Terraform init & apply...")
    subprocess.run(["terraform", "init"], cwd="terraform", check=True, timeout=60)
    subprocess.run(["terraform", "apply", "-auto-approve"], cwd="terraform", check=True, timeout=600)

    # Wait for VMs to initialize
    print("  Waiting for VMs to initialize (60s)...")
    time.sleep(60)

    # [3/7] Copy SSH key to scoring engine
    print("[3/7] Copying SSH key to scoring engine...")
    key = Path("terraform") / os.environ.get("TF_VAR_ssh_key_path", "proxmox")
    if not key.exists():
        key = Path("proxmox")
    scoring_user = os.environ["TF_VAR_vm_username"]

    # Read scoring engine IP from Terraform output
    ctx = read_terraform_ctx()
    scoring_ip = ctx["scoring_engine_ip"]

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

    # [4/7] Bootstrap scoring engine
    print("[4/7] Bootstrapping scoring engine (packages, Docker, Quotient)...")
    bootstrap_scoring_engine(ctx)

    # [4.5/7] Enable password auth + NOPASSWD sudo for ubuntu on team1 boxes
    print("[4.5/7] Enabling password auth + NOPASSWD sudo for ubuntu on team1 boxes...")
    # Only team1 exists at this point (team2+ are cloned later)
    team1_only = {k: v for k, v in teams.items() if k == "team1"}
    setup_ubuntu_auth(team1_only, boxes, ctx)

    # [5/7] Fix DNS on team1 boxes (needed for apt-get in Nakon) + run Nakon deployment
    print("[5/7] Fixing DNS on team1 boxes, then running Nakon deployment...")
    # Only team1 exists at this point — fix DNS so apt-get can resolve repos
    team1_only = {k: v for k, v in teams.items() if k == "team1"}
    fix_dns_on_boxes(team1_only, boxes, ctx)

    # Generate team1-only config for Nakon (team2+ don't exist yet)
    import copy
    nakon_config = json.loads((NAKON_DIR / "config.json").read_text())
    nakon_config_team1 = {"machines": [m for m in nakon_config["machines"] if ".101." in m["ip"]]}
    (NAKON_DIR / "config.json").write_text(json.dumps(nakon_config_team1, indent=2))

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
        check=True, timeout=1200,
    )
    # Restore full config for team2+ Nakon after cloning
    (NAKON_DIR / "config.json").write_text(json.dumps(nakon_config, indent=2))
    print("  Nakon deployment complete")

    # [6/7] Clone team1 boxes to other teams, fix DNS, harden services
    print("[6/7] Cloning team1 boxes to other teams, fixing DNS, hardening services...")
    clone_team_boxes(teams, boxes, ctx, comp_dir)

    # Deploy Nakon on team2+ boxes (team1 already done at [5/7])
    if len(teams) > 1:
        print("  Deploying Nakon on team2+ boxes...")
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
            check=True, timeout=180,
        )
        print("  Nakon deployment on team2+ complete")

    # [7/7] Push event config and seed competition
    print("[7/7] Pushing event configuration and seeding competition...")
    push_event_conf(comp_dir, teams, boxes, ctx, name)

    # Wait for Quotient to be ready
    print("  Waiting for Quotient to be ready...")
    time.sleep(10)

    # Seed teams and start competition
    print("  Seeding teams and starting the competition clock...")
    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
        "quotient_admin_password": "changeme123",
    }
    seed_and_start(scoring_ip, quotient_ctx)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  {name} is live")
    print(f"{'='*60}")
    print(f"Scenario: {scenario}")
    print(f"Saved to: competitions/{comp_name}/")
    print(f"\nScoreboard:    http://{scoring_ip}")
    print(f"Admin login:   admin / changeme123")
    print(f"\nTeam logins:")
    for team_name, team_data in teams.items():
        print(f"  {team_name} / {team_data['password']}  (subnet 192.168.{team_data['identifier']}.0/24)")
    print(f"\nScoring engine SSH: ssh -i {key} {scoring_user}@{scoring_ip}")
    print(f"{'='*60}")


def main():
    print("Tezcatlipoca - CTF Range Deployment")
    print("=" * 40)

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

    deploy(comp_dir)


if __name__ == "__main__":
    main()
