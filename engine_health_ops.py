"""Live-ops healthcheck timer and NAT/forwarding on the scoring engine."""

import base64
import subprocess

from ssh_ops import engine_ssh_opts


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
            *engine_ssh_opts(ctx),
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
    """Idempotently (re)assert the engine's team-subnet NAT + forwarding + team isolation."""
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
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}", cmd,
        ],
        check=False, timeout=30,
    )
    print("  NAT/forwarding ensured on scoring engine")
