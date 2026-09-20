"""Scoring-engine bootstrap, NAT, and Quotient event.conf push."""

import base64
import json
import subprocess

import toml

from quotient.setup import build_event_conf


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
        # first up after a --no-cache build cold-starts postgres (initdb) and
        # recreates every container — measured >60 s twice on e2e-2026-09-19
        check=True, timeout=600,
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


def push_event_conf(comp_dir, teams, boxes, ctx, event_name, admin_password,
                    postgres_password, redis_password, box_creds, inject_password=None):
    """Build event.conf and push it to the scoring engine.

    admin_password / postgres_password / redis_password / box_creds come from deploy() — the
    same per-run secrets passed to bootstrap_scoring_engine() (so the .env rewritten here
    matches the one it wrote; Postgres and the app must agree) and to fix_services_on_boxes()
    (which creates these OS accounts on the boxes). box_creds ({"admin": ..., "user1": ...})
    is what Quotient's Ssh/Smtp/Imap/Sql/Ftp credlist checks authenticate WITH; any mismatch
    or stale fallback literal silently scores healthy boxes as down, so there are no defaults.
    """

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
    credlist = "".join(f"{user},{pw}\n" for user, pw in box_creds.items())
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
