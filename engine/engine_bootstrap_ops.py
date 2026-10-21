"""Scoring-engine bootstrap: Quotient clone/install, firewall + healthcheck, .env push."""

import base64
import re
import time

from engine.engine_cmd_ops import _run_engine_cmd
from engine.engine_health_ops import install_range_healthcheck


def _clone_quotient_cmd(quotient_ref):
# A 40-hex commit can't be `git clone --branch`ed, so it goes through fetch + checkout.
    url = "https://github.com/dbaseqp/Quotient.git"
    ref = (quotient_ref or "").strip()
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return (
            f"sudo mkdir -p /opt/quotient && cd /opt/quotient && "
            f"sudo git init -q && sudo git remote add origin {url} && "
            f"sudo git fetch --depth 1 origin {ref} && "
            f"sudo git checkout -q FETCH_HEAD && "
            f"sudo git submodule update --init --recursive"
        )
    if ref:
        return (f"sudo mkdir -p /opt/quotient && "
                f"sudo git clone --depth 1 --branch {ref} --recurse-submodules {url} /opt/quotient "
                f"2>/dev/null || (cd /opt/quotient && sudo git submodule update --init --recursive)")
    return (f"sudo mkdir -p /opt/quotient && "
            f"sudo git clone --depth 1 --recurse-submodules {url} /opt/quotient "
            f"2>/dev/null || (cd /opt/quotient && sudo git submodule update --init --recursive)")


# Upstream is unpushable, so every clone gets the inject-detail fix re-applied here. The
# anchors are exact strings: a miss fails the build loudly rather than ship the bug.
_QUOTIENT_INJECTS_PY = r'''
import pathlib, sys
path = pathlib.Path(sys.argv[1])
legacy = "descPara.textContent = inject.Description"
fixed = "descPara.innerHTML = marked.parse(inject.Description)"
dup = "from.textContent = inject.Title"
if not path.is_file():
    sys.exit(f"injects.html not found at {path}")
src = path.read_text(encoding="utf-8")
new = src.replace(legacy, fixed, 1) if legacy in src else src
new = "\n".join(line for line in new.split("\n") if line.strip() != dup)
if fixed not in new or dup in new:
    sys.exit(f"injects.html patch did not apply at {path} — anchor text changed upstream")
if new != src:
    path.write_text(new, encoding="utf-8")
    print("Quotient inject detail page patched (markdown + single title)")
else:
    print("Quotient inject detail page already patched — verified")
'''


def _patch_quotient_injects_cmd():
    blob = base64.b64encode(_QUOTIENT_INJECTS_PY.encode()).decode()
    return (f"echo '{blob}' | base64 -d | sudo python3 - "
            f"/opt/quotient/static/templates/pages/injects.html")


def bootstrap_scoring_engine(ctx, postgres_password, redis_password, quotient_ref=None):
    # Grow the root to the disk we sized before the packages/Docker/Quotient build.
    from engine.engine_identity_ops import _GROW_ROOT_CMD
    _run_engine_cmd(ctx, _GROW_ROOT_CMD, check=False, timeout=120,
                    step="grow engine root filesystem")
    _run_engine_cmd(ctx, (
        # unattended-upgrades holds the dpkg/archives locks past Lock::Timeout and re-fires
        # mid-upgrade; stopping the units alone let apt-daily-upgrade spawn apt-get between
        # the stop and our upgrade. So mask the units, kill their cgroups, poll until every
        # lock is free, then upgrade before install.
        "sudo systemctl mask --now unattended-upgrades.service apt-daily.service "
        "apt-daily-upgrade.service apt-daily.timer apt-daily-upgrade.timer 2>/dev/null; "
        "sudo systemctl kill --kill-whom=all unattended-upgrades.service "
        "apt-daily.service apt-daily-upgrade.service 2>/dev/null; "
        "for i in $(seq 1 60); do "
        "  if ! sudo fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock "
        "/var/cache/apt/archives/lock >/dev/null 2>&1 "
        "&& ! pgrep -x 'apt|apt-get|dpkg|unattended-upgr' >/dev/null; then break; fi; "
        "  sudo killall -9 apt-get apt dpkg unattended-upgrade 2>/dev/null; sleep 3; "
        "done; "
        "sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>/dev/null; "
        "sudo dpkg --configure -a 2>/dev/null; "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 -f install -y 2>/dev/null; "
        # A reset mid-download fails the whole apt run (live: run-802b37f5, "Unable to connect
        # to us.archive.ubuntu.com"); the retry only costs time when the mirror really is gone.
        "ok=0; for i in 1 2 3; do "
        "  if sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update && "
        "     sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 upgrade -y && "
        "     sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y --fix-missing "
        # python3-paramiko is what the remote nakon deploy imports; baked in so a deploy
        # never pip-installs into the engine's system python.
        "       docker.io git curl python3-pip python3-paramiko apt-cacher-ng; "
        "  then ok=1; break; fi; sleep 15; "
        "done; test \"$ok\" = 1"
    ), timeout=1800, step="install engine packages")

    # apt-cacher-ng defaults to port 3142 on all interfaces; the boxes' gateway is the
    # engine's team-NIC address, so no listener change is needed.
    print("  Enabling apt-cacher-ng (package mirror cache for team boxes)...")
    _run_engine_cmd(ctx, (
        "sudo systemctl enable --now apt-cacher-ng && "
        "ss -ltn | grep -q ':3142 ' && echo '    apt-cacher-ng listening on 3142' "
        "|| echo '    WARNING: apt-cacher-ng not listening on 3142 yet'"
    ), check=False, timeout=60, step="enable apt-cacher-ng")

    print("  Installing Docker Compose v2 plugin...")
    # The key fetch must own its exit status: piped into tee, a reset mid-download still
    # "succeeded" and the deploy died later as an unsigned-repo GPG error (live: run-8d6ea600).
    _run_engine_cmd(ctx, (
        "set -e; "
        # An engine derived from a template already carries the plugin; re-fetching its key
        # is how a CDN reset killed a deploy whose engine needed nothing (live: run-dbb5c3dc).
        "if sudo docker compose version >/dev/null 2>&1 && command -v growpart >/dev/null 2>&1; then "
        "  echo '  docker compose v2 already present'; exit 0; "
        "fi; "
        "sudo install -m 0755 -d /etc/apt/keyrings; "
        "for i in 1 2 3; do "
        "  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && break; "
        "  sleep 5; "
        "done; "
        "sudo test -s /etc/apt/keyrings/docker.asc && "
        "sudo chmod a+r /etc/apt/keyrings/docker.asc && "
        "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] "
        "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable\" "
        "| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && "
        # cloud-guest-utils carries growpart: without it the 40 GB engine disk stays
        # on the image's ~10 GB root and fills within a deploy (live: 92% full engine).
        "sudo apt-get update && sudo apt-get install -y docker-compose-plugin cloud-guest-utils"
    ), timeout=600, step="install docker compose v2 plugin")

    print("  Starting Docker (installed by the engine template)...")
    _run_engine_cmd(ctx, (
        "sudo systemctl start docker && sudo systemctl enable docker && sleep 2 && sudo docker version"
    ), timeout=30, step="start docker")

    print(f"  Cloning Quotient ({quotient_ref or 'default branch'})...")
    # 120s was tight enough to fail a deploy while GitHub crawled (live: run-5286db18 timed
    # out mid-clone); a shallow clone with submodules is still fast on a healthy link, so the
    # budget only costs a slow uplink's patience, never a false success.
    _run_engine_cmd(ctx, _clone_quotient_cmd(quotient_ref), timeout=600,
                    step="clone Quotient")

    _run_engine_cmd(ctx, _patch_quotient_injects_cmd(), timeout=30,
                    step="patch Quotient inject page")

    realized = _run_engine_cmd(ctx, "git -C /opt/quotient rev-parse HEAD 2>/dev/null || true",
                               check=False, timeout=10, capture=True, step="read Quotient HEAD")
    quotient_head = (realized.stdout or "").strip()

    push_quotient_env(ctx, postgres_password, redis_password)

    print("  Building Quotient Docker images...")
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose build --no-cache",
                    timeout=1800, step="compose build")

    print("  Starting Quotient Docker containers...")
    # 600s: the fresh volume's Postgres initdb is a cold start on every build, and
    # on a loaded node it still misses docker compose's health window; the containers
    # keep starting, so retry instead of failing the deploy (live: run-3d569d1f).
    last = None
    for attempt in range(3):
        try:
            _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose up -d", timeout=600,
                            step="compose up")
            last = None
            break
        except Exception as e:  # noqa: BLE001 - the DB health window is a race
            last = e
            print(f"    compose up attempt {attempt + 1} failed ({e}); retrying")
            time.sleep(15)
    if last is not None:
        raise last

    _install_firewall_and_healthcheck(ctx)
    return {"quotient_head": quotient_head, "quotient_ref": quotient_ref or ""}


def _install_firewall_and_healthcheck(ctx):
    print("  Restoring network forwarding rules after Docker start...")
    _run_engine_cmd(ctx, (
            "sudo iptables -P FORWARD ACCEPT && "
            "sudo iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
            "sudo iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP && "
            "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE && "
            # MaxSessions 64 for the operator's ControlMaster multiplexing: every per-box
            # ProxyCommand shares one engine connection and the default 10 throttles it.
            "sudo sed -i -e 's/^#*AllowTcpForwarding.*/AllowTcpForwarding yes/' "
            "-e 's/^#*MaxSessions.*/MaxSessions 64/' "
            "-e 's/^#*MaxStartups.*/MaxStartups 30:30:100/' /etc/ssh/sshd_config && "
            "sudo systemctl reload sshd 2>/dev/null || true"
    ), timeout=15, step="restore forwarding rules")
    print("  Forwarding rules restored")

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
    _run_engine_cmd(ctx, (
        f"echo '{firewall_script_b64}' | base64 -d | sudo tee /usr/local/sbin/range-firewall.sh > /dev/null && "
        "sudo chmod +x /usr/local/sbin/range-firewall.sh && "
        f"echo '{firewall_service_b64}' | base64 -d | sudo tee /etc/systemd/system/range-firewall.service > /dev/null && "
        f"echo '{firewall_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/range-firewall.timer > /dev/null && "
        "sudo systemctl daemon-reload && sudo systemctl enable --now range-firewall.timer"
    ), timeout=30, step="install range-firewall unit+timer")
    print("  range-firewall.timer enabled (re-asserts NAT + isolation every 30s)")

    install_range_healthcheck(ctx)


def push_quotient_env(ctx, postgres_password, redis_password):
# Must precede compose up: the fresh postgres volume initializes with the credentials in this file.
    print("  Writing Quotient .env...")
    quotient_env = (
        f"POSTGRES_PASSWORD={postgres_password}\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_b64 = base64.b64encode(quotient_env.encode()).decode()
    _run_engine_cmd(ctx, (
        f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env > /dev/null && "
        "sudo chmod 600 /opt/quotient/.env"
    ), timeout=10, step="write Quotient .env")
