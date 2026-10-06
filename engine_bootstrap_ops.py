"""Scoring-engine bootstrap: Quotient clone/install, firewall + healthcheck, .env push."""

import base64
import re

from engine_cmd_ops import _run_engine_cmd
from engine_health_ops import install_range_healthcheck


def _clone_quotient_cmd(quotient_ref):
    """The git clone command for the pinned Quotient ref.

    A 40-hex commit can't be `git clone --branch`ed, so it goes through an explicit
    fetch + checkout (GitHub serves arbitrary SHA fetches at depth 1). Branch/tag refs
    clone directly. Empty ref = historical behavior: depth-1 HEAD of the default
    branch (deterministic only because the template freezes whatever it built)."""
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


def bootstrap_scoring_engine(ctx, postgres_password, redis_password, quotient_ref=None):
    """Bootstrap the scoring engine: install packages, Docker, Quotient.

    M4: runs on the engine-TEMPLATE build VM (never on the deployed engine, which
    clones from that template). Returns build info {"quotient_head": ...} for
    traceability record; the HASH input is the pinned ref from config, not this
    realized HEAD."""
    _run_engine_cmd(ctx, (
        # Stop the apt machinery for the build window: unattended-upgrades both holds
        # the dpkg lock past Lock::Timeout and re-fires when `apt-get update` refreshes
        # the lists — and the preamble's killall can catch it mid-upgrade, leaving an
        # old libc6 under freshly unpacked -dev packages (live-found 2026-09-26, two
        # engine-template builds in a row: rc=100, first on the lock, then on the
        # half-upgrade; stopping only the TIMERS left an already-running
        # apt-daily-upgrade.service free to spawn apt-get mid-bootstrap).
        #
        # `systemctl stop` alone still lost the race (live-found 2026-09-27, cyberrange:
        # rc=100, `/var/cache/apt/archives/lock` held by a fresh apt-get pid that
        # apt-daily-upgrade.service spawned *between* the stop and our `upgrade`).
        # DPkg::Lock::Timeout does NOT cover the archives-cache lock, so that timeout
        # can't save us. Fix: MASK the units (a masked unit cannot be started or
        # re-activated by the timer/dbus), kill their cgroups, then poll until every
        # apt/dpkg lock is actually free before touching apt. Upgrade first, then
        # install, so the dependency set is deterministic; `dpkg --configure -a` +
        # `-f install` repair whatever an earlier interrupted upgrade left behind.
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
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 upgrade -y && "
        "sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y "
        "docker.io git curl python3-pip apt-cacher-ng"
    ), timeout=1800, step="install engine packages")

    # apt-cacher-ng defaults to port 3142 on all interfaces; the boxes' gateway IP is the
    # engine's team-NIC address, so no listener change is needed. Enable it here so it is
    # serving before the first box-side apt prep (phase 5). Not fatal if the probe fails:
    # the per-box proxy write in hardening_ops is gated by the same Compfile flag and apt
    # works unproxied, just slower.
    print("  Enabling apt-cacher-ng (package mirror cache for team boxes)...")
    _run_engine_cmd(ctx, (
        "sudo systemctl enable --now apt-cacher-ng && "
        "ss -ltn | grep -q ':3142 ' && echo '    apt-cacher-ng listening on 3142' "
        "|| echo '    WARNING: apt-cacher-ng not listening on 3142 yet'"
    ), check=False, timeout=60, step="enable apt-cacher-ng")

    print("  Installing Docker Compose v2 plugin...")
    _run_engine_cmd(ctx, (
        "install -m 0755 -d /etc/apt/keyrings && "
        "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc > /dev/null && "
        "sudo chmod a+r /etc/apt/keyrings/docker.asc && "
        "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] "
        "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable\" "
        "| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && "
        "sudo apt-get update && sudo apt-get install -y docker-compose-plugin"
    ), timeout=600, step="install docker compose v2 plugin")

    print("  Starting Docker (already installed by Terraform)...")
    _run_engine_cmd(ctx, (
        "sudo systemctl start docker && sudo systemctl enable docker && sleep 2 && sudo docker version"
    ), timeout=30, step="start docker")

    print(f"  Cloning Quotient ({quotient_ref or 'default branch'})...")
    _run_engine_cmd(ctx, _clone_quotient_cmd(quotient_ref), timeout=120,
                    step="clone Quotient")

    realized = _run_engine_cmd(ctx, "git -C /opt/quotient rev-parse HEAD 2>/dev/null || true",
                               check=False, timeout=10, capture=True, step="read Quotient HEAD")
    quotient_head = (realized.stdout or "").strip()

    push_quotient_env(ctx, postgres_password, redis_password)

    print("  Building Quotient Docker images...")
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose build --no-cache",
                    timeout=1800, step="compose build")

    print("  Starting Quotient Docker containers...")
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose up -d", timeout=600,
                    step="compose up")

    _install_firewall_and_healthcheck(ctx)
    return {"quotient_head": quotient_head, "quotient_ref": quotient_ref or ""}


def _install_firewall_and_healthcheck(ctx):
    """Disk state every engine carries: forwarding rules, sshd multiplexing headroom,
    the range-firewall + range-healthcheck units. Runs at template build; the firewall
    timer re-asserts the rules on every clone."""
    print("  Restoring network forwarding rules after Docker start...")
    _run_engine_cmd(ctx, (
            "sudo iptables -P FORWARD ACCEPT && "
            "sudo iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
            "sudo iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP && "
            "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE && "
            # MaxSessions raised for the operator's ControlMaster multiplexing (M1.5): every
            # per-box ProxyCommand shares one engine connection, and the default 10 would
            # throttle M2's concurrent waits; MaxStartups absorbs cold-start bursts.
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
    """Write /opt/quotient/.env (per-competition DB secrets). Extracted from bootstrap:
    the template build removes this file before conversion, and every engine clone
    re-writes it BEFORE compose up so the fresh postgres volume initializes with the
    right credentials."""
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
