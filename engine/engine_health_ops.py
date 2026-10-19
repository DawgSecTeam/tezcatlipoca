"""Live-ops healthcheck timer and NAT/forwarding on the scoring engine."""

import base64
import subprocess
from pathlib import Path

from common.ssh_ops import engine_ssh_opts


def healthcheck_script():
    return (
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
        "# 2b. Scoring round loop: restart a stale, already-started loop after an engine reboot.\n"
        "# The helper is a no-op for a held pre-T0 competition or a deliberate pause.\n"
        "if [ -f /opt/quotient/config/event.conf ]; then\n"
        "  python3 /usr/local/sbin/range-round-loop-heal.py\n"
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


def round_loop_heal_script():
# The engine-side actor for round_loop.heal_actions. It reads the engine's own account file,
#     authenticates over localhost, and logs outcomes only — never credentials or config.
    return '''#!/usr/bin/env python3
"""Restart a stale Quotient round loop after an engine reboot (installed by the deploy)."""

import json
import sys
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/tezcatlipoca")
from round_loop import heal_actions

BASE = "http://localhost"
CONF = Path("/opt/quotient/config/event.conf")
LOG = Path("/var/log/range-healthcheck.log")


def log(message):
    with LOG.open("a") as fh:
        fh.write(f"round-loop heal: {message}\\n")


def credentials():
    # event.conf is flat [[admin]] tables; parse just those (no tomllib: the engine's python
    # may predate it). Passwords are quote/backslash-free by construction.
    entries, current = [], None
    for line in CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "[[admin]]":
            current = {}
            entries.append(current)
        elif line.startswith("["):
            current = None
        elif current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip().strip('"')
    accounts = {entry.get("name"): entry.get("pw") for entry in entries}
    for name in ("scoring", "admin"):
        if accounts.get(name):
            return name, accounts[name]
    return None, None


def main():
    username, password = credentials()
    if not username:
        log("no admin account in event.conf")
        return
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))
    login = urllib.request.Request(
        BASE + "/api/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    opener.open(login, timeout=10).read()
    engine = json.loads(opener.open(BASE + "/api/engine", timeout=10).read())
    for path, payload in heal_actions(engine):
        request = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        status = opener.open(request, timeout=10).status
        log(f"POST {path} -> {status} (stale started loop restarted)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                      # never print the request body / config
        try:
            log(f"failed ({type(exc).__name__})")
        except OSError:
            pass
'''


def install_range_healthcheck(ctx):
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    print("  Installing range-healthcheck systemd unit + timer...")
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
    healthcheck_script_b64 = base64.b64encode(healthcheck_script().encode()).decode()
    heal_script_b64 = base64.b64encode(round_loop_heal_script().encode()).decode()
    round_loop_b64 = base64.b64encode(
        Path(__file__).with_name("round_loop.py").read_text(encoding="utf-8").encode()
    ).decode()
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
                "sudo mkdir -p /usr/local/lib/tezcatlipoca && "
                f"echo '{round_loop_b64}' | base64 -d | sudo tee /usr/local/lib/tezcatlipoca/round_loop.py > /dev/null && "
                f"echo '{heal_script_b64}' | base64 -d | sudo tee /usr/local/sbin/range-round-loop-heal.py > /dev/null && "
                "sudo chmod +x /usr/local/sbin/range-round-loop-heal.py && "
                f"echo '{healthcheck_service_b64}' | base64 -d | sudo tee /etc/systemd/system/range-healthcheck.service > /dev/null && "
                f"echo '{healthcheck_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/range-healthcheck.timer > /dev/null && "
                "sudo touch /var/log/range-healthcheck.log && "
                "sudo systemctl daemon-reload && sudo systemctl enable --now range-healthcheck.timer"
            ),
        ],
        check=True, timeout=30,
    )
    print("  range-healthcheck.timer enabled (checks every 60s, logs failures only; "
          "round-loop heal installed)")


def ensure_nat_forwarding(ctx):
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
