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
    We use ProxyCommand through the scoring engine, authenticating as ubuntu.
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
    
    Writes a hardening script to the gateway, then distributes it to each target box.
    This avoids nested SSH quoting issues that plague inline command execution.
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
                    "# MySQL: bind to 0.0.0.0",
                    "if [ -f /etc/mysql/mysql.conf.d/50-server.cnf ]; then",
                    "  sudo sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' /etc/mysql/mysql.conf.d/50-server.cnf",
                    "fi",
                    "if [ -f /etc/mysql/my.cnf ]; then",
                    "  sudo sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' /etc/mysql/my.cnf",
                    "fi",
                    "sudo systemctl restart mysql 2>/dev/null || sudo systemctl restart mariadb 2>/dev/null || true",
                    "sleep 2",
                    "# Create user1/user2 with credlist passwords",
                    "echo 'CREATE USER IF NOT EXISTS \\'user1\\'@\\'%\\' IDENTIFIED BY \\'password1\\'; GRANT ALL PRIVILEGES ON *.* TO \\'user1\\'@\\'%\\'; FLUSH PRIVILEGES;' | sudo mysql 2>/dev/null || true",
                    "echo 'CREATE USER IF NOT EXISTS \\'user2\\'@\\'%\\' IDENTIFIED BY \\'password2\\'; GRANT ALL PRIVILEGES ON *.* TO \\'user2\\'@\\'%\\'; FLUSH PRIVILEGES;' | sudo mysql 2>/dev/null || true",
                    "",
                ])
            
            if "dovecot" in services:
                script_lines.extend([
                    "# Dovecot: enable plaintext auth, create mail dirs",
                    "sudo sed -i 's/^#*disable_plaintext_auth.*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf 2>/dev/null || true",
                    "sudo mkdir -p /home/user1/mail /home/user2/mail",
                    "sudo chmod 700 /home/user1/mail /home/user2/mail",
                    "sudo chown user1:user1 /home/user1/mail",
                    "sudo chown user2:user2 /home/user2/mail",
                    "sudo systemctl restart dovecot 2>/dev/null || true",
                    "",
                ])
            
            if "bind" in services or "named" in services:
                script_lines.extend([
                    "# Bind9: allow queries from anywhere",
                    "cat > /tmp/named.conf.options << 'BIND9EOF'",
                    "options {",
                    "  directory \"/var/cache/bind\";",
                    "  recursion yes;",
                    "  allow-query { any; };",
                    "  forwarders { 8.8.8.8; 1.1.1.1; };",
                    "};",
                    "BIND9EOF",
                    "sudo cp /tmp/named.conf.options /etc/bind/named.conf.options",
                    # Fix missing named.conf.default-zones
                    "if [ ! -f /etc/bind/named.conf.default-zones ]; then",
                    "  cat > /tmp/named.conf.default-zones << 'BZEOF'",
                    "zone \".\" {",
                    "  type hint;",
                    "  file \"/usr/share/dns/root.hints\";",
                    "};",
                    "zone \"localhost\" {",
                    "  type master;",
                    "  file \"/etc/bind/db.local\";",
                    "};",
                    "zone \"127.in-addr.arpa\" {",
                    "  type master;",
                    "  file \"/etc/bind/db.127\";",
                    "};",
                    "BZEOF",
                    "  sudo cp /tmp/named.conf.default-zones /etc/bind/named.conf.default-zones",
                    "fi",
                    "sudo systemctl restart bind9 2>/dev/null || true",
                    "",
                ])
            
            if len(script_lines) <= 2:  # Only shebang and set -e
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
                result = ssh_via_gateway(ctx, ip, deploy_cmd, timeout=45)
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
                        check=True, timeout=40, capture_output=True, text=True,
                    )
                    print(f"    Auth configured on {ip}")
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    if attempt < 8:
                        print(f"    Auth setup attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                        time.sleep(15)
                    else:
                        print(f"  WARNING: Auth setup failed for {ip} after 8 attempts — proceeding anyway")


def clone_team_boxes(teams, boxes, ctx, comp_dir):
    sorted_keys = sorted(teams.keys())
    team1_key = sorted_keys[0]
    team1 = teams[team1_key]
    other_keys = sorted_keys[1:]

    if not other_keys:
        print("  Single team — skipping clone step.")
        return {}

    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    ssh_pubkey = os.environ["TF_VAR_ssh_public_key"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )

    # Reset cloud-init state on team1 boxes so clones re-run cloud-init with their own IPs.
    # Installed packages and service configs survive — cloud-init clean only removes the
    # instance data that marks "cloud-init has already run".
    print("  Running cloud-init clean on team1 boxes...")
    for box in boxes:
        ip = f"192.168.{team1['identifier']}.{box['last_octet']}"
        subprocess.run(
            [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10", "-o", f"ProxyCommand={proxy}",
                f"ubuntu@{ip}", "sudo cloud-init clean",
            ],
            check=True,
        )
        print(f"    {box['name']}: cloud-init clean done")

    print("  Shutting down team1 boxes...")
    for i, box in enumerate(boxes):
        vmid = vm_id_for(team1["identifier"], i)
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown")["data"]
        wait_for_proxmox_task(node, upid)
        print(f"    {team1_key}-{box['name']} (vmid {vmid}) stopped")

    # Clone sequentially to avoid Proxmox's VM lock contention (same issue the Terraform
    # retries=15 works around when cloning from templates concurrently).
    cloned_vms = {}
    print("  Cloning team1 boxes to other teams...")
    for team_key in other_keys:
        team = teams[team_key]
        for i, box in enumerate(boxes):
            src_vmid = vm_id_for(team1["identifier"], i)
            new_vmid = vm_id_for(team["identifier"], i)
            new_name = f"{team_key}-{box['name']}"
            print(f"    {team1_key}-{box['name']} (vmid {src_vmid}) → {new_name} (vmid {new_vmid})...")
            destroy_vm_if_exists(node, new_vmid)
            upid = proxmox_api(
                "POST", f"/nodes/{node}/qemu/{src_vmid}/clone",
                json={"newid": new_vmid, "name": new_name, "full": 1, "target": node},
            )["data"]
            wait_for_proxmox_task(node, upid, timeout=600)
            cloned_vms[new_name] = new_vmid

    print("  Configuring cloud-init on clones...")
    dns_box = next((b for b in boxes if b["name"].startswith("dns")), None)
    for team_key in other_keys:
        team = teams[team_key]
        for i, box in enumerate(boxes):
            vmid = cloned_vms[f"{team_key}-{box['name']}"]
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            gw = f"192.168.{team['identifier']}.1"
            dns = (
                f"192.168.{team['identifier']}.{dns_box['last_octet']}"
                if dns_box else "8.8.8.8"
            )
            proxmox_api(
                "PUT", f"/nodes/{node}/qemu/{vmid}/config",
                json={
                    "ipconfig0": f"ip={ip}/24,gw={gw}",
                    "nameserver": dns,
                    "ciuser": "ubuntu",
                    "cipassword": "ubuntu",
                    "sshkeys": url_quote(ssh_pubkey, safe=""),
                    "net0": f"virtio,bridge=vmbr{team['identifier']}",
                },
            )

    print("  Starting clones and restarting team1 boxes...")
    for team_key in other_keys:
        for i, box in enumerate(boxes):
            vmid = cloned_vms[f"{team_key}-{box['name']}"]
            proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")
    for i, box in enumerate(boxes):
        vmid = vm_id_for(team1["identifier"], i)
        proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")

    (comp_dir / "cloned_vms.json").write_text(json.dumps(cloned_vms, indent=2))
    print(f"  Saved {len(cloned_vms)} clone VM IDs to {comp_dir}/cloned_vms.json")
    return cloned_vms


def push_event_conf(comp_dir):
    # Run after `terraform apply` so the scoring engine's real (DHCP-leased) IP
    # is already in the output. Writes event.conf into the competition folder,
    # then pushes/restarts Quotient and seeds teams.
    ctx = read_terraform_ctx()
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    event_conf_path = comp_dir / "event.conf"
    event_conf_path.write_text(toml.dumps(build_event_conf(ctx, box_services)))

    ip, key, user = ctx["scoring_engine_ip"], ctx["ssh_key_path"], ctx["vm_username"]
    ssh = ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", f"{user}@{ip}"]

    print(f"  Pushing event.conf to the scoring engine ({ip})...")
    subprocess.run(
        ["scp", "-i", key, "-o", "StrictHostKeyChecking=no", str(event_conf_path), f"{user}@{ip}:/tmp/event.conf"],
        check=True,
    )
    subprocess.run(
        ssh + ["sudo cp /tmp/event.conf /opt/quotient/config/event.conf && cd /opt/quotient && sudo docker compose restart"],
        check=True,
    )
    print("  Seeding teams and starting the competition clock...")
    seed_and_start(f"http://{ip}:80", ctx)

    return ctx


def bootstrap_scoring_engine(ctx):
    """Install packages, clone Quotient, build Docker on the scoring engine.
    
    Previously done in terraform main.tf Step A/C — moved here to keep terraform apply fast.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    
    print("  Waiting for scoring engine to become reachable...")
    for attempt in range(1, 25):
        try:
            result = subprocess.run(
                ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=5", f"{scoring_user}@{scoring_ip}", "echo ready"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                print(f"  Scoring engine reachable on attempt {attempt}")
                break
        except:
            pass
        if attempt < 24:
            print(f"  Scoring engine not ready yet (attempt {attempt}/24), waiting 15s...")
            time.sleep(15)
        else:
            print("  WARNING: Scoring engine not reachable after 24 attempts")
            return
    
    print("  Installing packages on scoring engine...")
    # Step A: Install Docker, git, pip, clone Quotient
    install_cmd = (
        "for i in $(seq 1 30); do sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; sleep 2; done; "
        "sudo apt-get update && "
        "sudo apt-get install -y git python3-pip python3-venv ca-certificates curl && "
        "curl -fsSL https://get.docker.com | sudo sh && "
        "sudo systemctl enable --now docker && "
        "sudo pip3 install --break-system-packages mysql-connector-python paramiko python-dotenv"
    )
    subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         f"{scoring_user}@{scoring_ip}", install_cmd],
        timeout=300,
    )
    print("  Packages installed on scoring engine")
    
    print("  Cloning Quotient repo...")
    clone_cmd = (
        "if [ ! -d /opt/quotient ]; then "
        "sudo rm -rf /opt/quotient && sudo git clone https://github.com/dbaseqp/Quotient /opt/quotient && "
        "cd /opt/quotient && sudo git submodule update --init --recursive && "
        "sudo cp /opt/quotient/config/credlists/linux.credlist.example /opt/quotient/config/credlists/linux.credlist && "
        "sudo cp /opt/quotient/config/credlists/windows.credlist.example /opt/quotient/config/credlists/windows.credlist; "
        "fi; "
        "if [ ! -d /opt/nakon ]; then "
        "sudo rm -rf /opt/nakon && sudo git clone https://github.com/CyberDawgsTeam/nakon /opt/nakon; "
        "fi"
    )
    subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         f"{scoring_user}@{scoring_ip}", clone_cmd],
        timeout=120,
    )
    print("  Quotient repo cloned")
    
    print("  Building Quotient Docker containers...")
    # Step C: Write .env and start Docker
    # Use echo/printf instead of heredoc to avoid SSH quoting issues
    docker_cmd = (
        "if [ ! -f /opt/quotient/.env ]; then "
        "printf '%s\\n' 'POSTGRES_USER=engineuser' 'POSTGRES_PASSWORD=changeme_in_prod' "
        "'POSTGRES_DB=engine' 'POSTGRES_HOST=quotient_database' "
        "'REDIS_PASSWORD=changeme_in_prod' 'REDIS_HOST=quotient_redis' "
        "| sudo tee /opt/quotient/.env > /dev/null; fi; "
        "cd /opt/quotient && sudo docker compose up -d --build; "
        "sleep 15; "
        "sudo iptables -I DOCKER-USER -s 192.168.0.0/16 -j ACCEPT 2>/dev/null; "
        "sudo iptables -I DOCKER-USER -d 192.168.0.0/16 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true"
    )
    subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         f"{scoring_user}@{scoring_ip}", docker_cmd],
        timeout=300,
    )
    print("  Quotient Docker containers started")


def run_nakon_deploy(ctx):
    """Run Nakon deployment on team1 boxes via the scoring engine.
    
    Previously done in terraform main.tf Step D — moved here to keep terraform apply fast.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    
    print("  Staging Nakon config to scoring engine...")
    # Copy nakon config.json and .env to scoring engine /tmp/ (root-owned /opt/nakon needs sudo cp)
    subprocess.run(
        ["scp", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         str(NAKON_DIR / "config.json"), str(NAKON_DIR / ".env"),
         f"{scoring_user}@{scoring_ip}:/tmp/"],
        check=True,
    )
    
    # Run Nakon deploy — stage via /tmp then sudo cp to /opt/nakon
    print("  Running Nakon deployment on team1 boxes...")
    deploy_cmd = (
        "sudo cp /tmp/config.json /opt/nakon/config.json && "
        "sudo cp /tmp/.env /opt/nakon/.env && "
        "cd /opt/nakon && sudo python3 deploy.py"
    )
    subprocess.run(
        ["ssh", "-i", key, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         f"{scoring_user}@{scoring_ip}", deploy_cmd],
        timeout=300,
    )
    print("  Nakon deployment complete")


def deploy_competition(comp_dir, name, scenario, difficulty, teams, admin_password, boxes):
    # Shared tail for both "reuse" and "new" flows below — everything from here on is
    # identical regardless of how name/teams/boxes were decided. Compfile is written by the
    # caller as soon as name/scenario/difficulty are known, not here — no reason to hold a
    # file back once its contents are decided, just because later steps might still fail.
    (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))
    (comp_dir / "teams.json").write_text(json.dumps(teams, indent=2))

    print(f"\n=== Deploying '{name}' ===")

    print("[1/7] Saving competition settings to .env for Terraform...")
    update_env({
        "TF_VAR_event_name": name,
        "TF_VAR_quotient_admin_password": admin_password,
        "TF_VAR_teams": json.dumps(teams),
        "TF_VAR_boxes_per_team": json.dumps(boxes),
    })

    sorted_team_keys = sorted(teams.keys())
    team1_only = {sorted_team_keys[0]: teams[sorted_team_keys[0]]}

    print("[2/7] Generating Nakon config for team1 (other teams clone from team1)...")
    generate_nakon_config(team1_only, boxes, difficulty, comp_dir)

    print("[3/7] Running Terraform — provisioning bridges, scoring engine, and team1 boxes.")
    print("      (Several minutes; output streams below.)")
    # Clean up stale bridges from previous runs to avoid "resource already exists" errors
    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    for team_key in sorted(teams.keys()):
        team = teams[team_key]
        destroy_bridge_if_exists(node, f"vmbr{team['identifier']}")
    subprocess.run(["terraform", "init"], cwd="terraform", check=True)
    subprocess.run(["terraform", "apply", "-parallelism=1", "-auto-approve"], cwd="terraform", check=True)

    print("[4/7] Bootstrapping scoring engine (packages, Docker, Quotient)...")
    ctx = read_terraform_ctx()
    bootstrap_scoring_engine(ctx)

    print("[4.5/7] Enabling password auth + NOPASSWD sudo for ubuntu on team1 boxes...")
    setup_ubuntu_auth(team1_only, boxes, ctx)

    print("[5/7] Running Nakon deployment on team1 boxes...")
    run_nakon_deploy(ctx)

    print("[6/7] Cloning team1 boxes to other teams, fixing DNS, hardening services...")
    clone_team_boxes(teams, boxes, ctx, comp_dir)
    if len(teams) > 1:
        print("  Waiting 30s for clone VMs to finish booting...")
        time.sleep(30)
    fix_dns_on_boxes(teams, boxes, ctx)
    fix_services_on_boxes(comp_dir, teams, boxes, ctx)

    print("[7/7] Configuring Quotient and starting the competition...")
    ctx = push_event_conf(comp_dir)

    print_summary(name, scenario, comp_dir, teams, admin_password, ctx)


def print_summary(name, scenario, comp_dir, teams, admin_password, ctx):
    ip = ctx["scoring_engine_ip"]
    print("\n" + "=" * 64)
    print(f"  {name} is live")
    print("=" * 64)
    print(f"Scenario: {scenario}")
    print(f"Saved to: {comp_dir}/")
    print()
    print(f"Scoreboard:    http://{ip}")
    print(f"Admin login:   admin / {admin_password}")
    print()
    print("Team logins:")
    for key, t in teams.items():
        print(f"  {key} / {t['password']}  (subnet 192.168.{t['identifier']}.0/24)")
    print()
    print(f"Scoring engine SSH: ssh -i {ctx['ssh_key_path']} {ctx['vm_username']}@{ip}")
    print("=" * 64)


### Logic
print("=" * 64)
print("  COMPETITION DEPLOYMENT TOOL")
print("=" * 64)
print("Collects your settings, then runs Terraform → nakon → Quotient")
print("automatically — no further input needed once deployment starts.\n")
print("  [1] Create a new competition")
print("  [2] Rerun an existing competition with new teams")
print()

while True:
    choice = input("→ ").strip()
    if choice in ("1", "2"):
        break
    print("  Enter 1 or 2.")

use_previous_competition = choice == "2"

if use_previous_competition:
   previous_competitions = load_previous_competitions()
   if not previous_competitions:
      print("No previous competitions found (none have a Compfile yet) — exiting.")
      sys.exit()

   print()
   competition = pick_competition(previous_competitions)
   if competition is None:
      print("Quitting.")
      sys.exit()

   name, scenario, difficulty = load_compfile(f"competitions/{competition}/Compfile")
   comp_dir = Path("competitions") / competition
   print(f"\nReusing '{name}' ({competition}) — same scenario/difficulty/boxes, fresh teams.")

   print("\n─── Teams (fresh passwords are generated for this run) " + "─" * 9)
   number_of_teams = int(input("How many teams will be playing? "))
   teams = collect_teams(number_of_teams)
   admin_password = random_password()

   boxes = load_boxes(comp_dir)
   if boxes is None:
      print(f"\nNo boxes.json saved for '{competition}' yet — let's define its boxes now.")
      print("\n─── Boxes (every team gets a clone of each one you define here) " + "─" * 0)
      boxes = collect_boxes()
   else:
      print(f"\nReplaying the {len(boxes)} box(es) saved from when '{competition}' was created:")
      for b in boxes:
         print(f"  - {b['name']} ({b['template']})")

   if not confirm_deploy(name, scenario, difficulty, teams, boxes):
      print("Cancelled — nothing was deployed.")
      sys.exit()

   deploy_competition(comp_dir, name, scenario, difficulty, teams, admin_password, boxes)

else:
   print("\n─── New competition " + "─" * 44)
   name = input("Competition name: ").strip()
   scenario = input("Scenario description: ").strip()
   difficulty = int(input("Difficulty (1-10): "))
   comp_id = name.lower().replace(" ", "-")
   comp_dir = Path("competitions") / comp_id
   comp_dir.mkdir(parents=True, exist_ok=True)

   # Written immediately, not after the deploy succeeds — name/scenario/difficulty are
   # already final at this point, and the folder should look like a real competition from
   # the moment it's created, not only once everything downstream has also gone right.
   (comp_dir / "Compfile").write_text(f"name {name}\nscenario {scenario}\ndifficulty {difficulty}\n")

   print("\n─── Teams " + "─" * 53)
   number_of_teams = int(input("How many teams will be playing? "))
   teams = collect_teams(number_of_teams)
   admin_password = random_password()

   print("\n─── Boxes (every team gets a clone of each one you define here) " + "─" * 0)
   boxes = collect_boxes()

   if not confirm_deploy(name, scenario, difficulty, teams, boxes):
      print("Cancelled — nothing was deployed.")
      sys.exit()

   deploy_competition(comp_dir, name, scenario, difficulty, teams, admin_password, boxes)
