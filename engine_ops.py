"""Scoring-engine bootstrap, NAT, and Quotient event.conf push."""

import base64
import json
from pathlib import Path
import re
import subprocess

import toml

from quotient.setup import build_event_conf
from ssh_ops import engine_ssh_opts


def _run_engine_cmd(ctx, cmd, check=True, timeout=60, capture=False, step=None):
    """One SSH command to the scoring engine — the shared body of every bootstrap step.

    `step` names the pipeline step this command belongs to. Every command here runs
    the same `ssh ... <cmd>` shape, so a raw CalledProcessError/TimeoutExpired out of
    this function names nothing an operator can act on: bootstrap_scoring_engine alone
    is ~11 steps over up to 1800s, and the only enclosing handler is deploy's generic
    per-phase message. Raise a labelled RuntimeError instead, chaining the original as
    __cause__ so the traceback still carries the ssh argv and return code."""
    argv = ["ssh", "-i", ctx["ssh_key_path"], *engine_ssh_opts(ctx),
            f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}", cmd]
    # Guard: _fork_exec dies with an opaque "expected str, bytes or os.PathLike
    # object, not tuple" when a non-string sneaks into the argv (live-found
    # 2026-09-25, first engine-template build). Fail with the full context instead.
    bad = [(i, repr(a)) for i, a in enumerate(argv) if not isinstance(a, str)]
    if bad:
        raise RuntimeError(
            f"engine SSH argv has non-string element(s) {bad} — ctx keys "
            f"{sorted(ctx)}: ssh_key_path={ctx.get('ssh_key_path')!r:.120} "
            f"vm_username={ctx.get('vm_username')!r} "
            f"scoring_engine_ip={ctx.get('scoring_engine_ip')!r}")
    try:
        return subprocess.run(argv, check=check, timeout=timeout,
                              capture_output=capture, text=capture)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        label = repr(step) if step else "<unnamed>"
        raise RuntimeError(f"engine step {label} failed: {e}") from e


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


def install_round_loop_guard(ctx, comp_dir, scoring_password):
    """Install the round-loop watchdog on the engine (opt-in, Compfile `round_loop_guard`).

    After an engine reboot Docker brings the containers back but the round loop stays
    stopped, and the scoreboard freezes while everything that reads it keeps working —
    an old UP is reported as a live UP (docs/known-issues.md). verify detects it and
    `--fix-round-loop` repairs it, but both need someone to run them.

    The timer authenticates as the dedicated `scoring` account, never `admin`: Quotient
    allows one session per account, so an unattended login on `admin` would evict the
    operator's, verify's or the harness's session on every tick. The decision itself is
    round_loop.round_loop_state — the same function verify's gate uses, pushed here so
    the two cannot drift.

    Idempotent: re-pushing the same files and re-enabling the timer is safe, so a resume
    or a redeploy with a fresh `scoring` password just rewrites them.
    """
    tools_dir = Path(__file__).resolve().parent / "tools"
    round_loop_src = (Path(__file__).resolve().parent / "round_loop.py").read_text()
    guard_src = (tools_dir / "round_loop_guard.py").read_text()
    config = json.dumps({"base_url": "http://localhost", "username": "scoring",
                         "password": scoring_password}, indent=2)

    service = (
        "[Unit]\n"
        "Description=Restart Quotient's scoring round loop if an engine reboot stopped it\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/bin/python3 /usr/local/sbin/round_loop_guard.py --quiet\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Check the scoring round loop periodically\n"
        "\n"
        "[Timer]\n"
        # Delay is 60s, so a 60s tick sees a stopped loop within one round of it becoming
        # stale; the 120s boot delay lets Quotient come up before the first check.
        "OnBootSec=120\n"
        "OnUnitActiveSec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    def b64(text):
        return base64.b64encode(text.encode()).decode()

    _run_engine_cmd(ctx, (
        f"echo '{b64(round_loop_src)}' | base64 -d | sudo tee /usr/local/sbin/round_loop.py > /dev/null && "
        f"echo '{b64(guard_src)}' | base64 -d | sudo tee /usr/local/sbin/round_loop_guard.py > /dev/null && "
        "sudo chmod 644 /usr/local/sbin/round_loop.py && "
        "sudo chmod 755 /usr/local/sbin/round_loop_guard.py && "
        f"echo '{b64(config)}' | base64 -d | sudo tee /opt/quotient/round-loop-guard.json > /dev/null && "
        "sudo chmod 600 /opt/quotient/round-loop-guard.json && "
        f"echo '{b64(service)}' | base64 -d | sudo tee /etc/systemd/system/round-loop-guard.service > /dev/null && "
        f"echo '{b64(timer)}' | base64 -d | sudo tee /etc/systemd/system/round-loop-guard.timer > /dev/null && "
        "sudo systemctl daemon-reload && sudo systemctl enable --now round-loop-guard.timer"
    ), timeout=60, step="install round-loop-guard unit+timer")
    print("    round-loop-guard.timer enabled (restarts a stopped round loop; logs as `scoring`)")


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


def clean_engine_for_template(ctx, vmid=None):
    """Strip every per-deploy / per-competition trace before qm template (M4 template
    point): containers + data volumes (no scoring DB, teams or injects baked), .env and
    event.conf secrets, machine-id, cloud-init state, SSH host keys. The apt-cacher-ng
    package cache is deliberately KEPT — a warm cache speeds up every later run.

    `vmid` is the engine-template BUILD VM. Pass it whenever it is known: the command
    below runs over SSH to a management IP that a second competition's engine can share,
    and this is the most destructive command in the pipeline. See
    assert_engine_build_identity()."""
    if vmid is not None:
        assert_engine_build_identity(ctx, vmid)
    _run_engine_cmd(ctx, (
        "cd /opt/quotient && sudo docker compose down -v --remove-orphans 2>/dev/null; "
        # ~1 GB of image build cache is dead weight once the images exist.
        "sudo docker builder prune -af >/dev/null 2>&1; "
        "sudo rm -f /opt/quotient/.env /opt/quotient/config/event.conf && "
        "sudo rm -rf /opt/quotient/config/credlists && "
        "sudo truncate -s 0 /etc/machine-id && "
        "sudo rm -f /var/lib/dbus/machine-id && "
        "sudo cloud-init clean --logs --machine-id && "
        "sudo rm -f /etc/ssh/ssh_host_* && "
        # The stamp must NOT survive into the template, or every clone would carry it.
        f"sudo rm -f {ENGINE_BUILD_STAMP} && "
        "echo '    engine template cleaned (volumes, secrets, identity, host keys)'"
    ), timeout=300, step="clean engine for template")


# Identity stamp for the engine-template build VM. The build VM is booted on the PLANNED
# engine management IP and the cleanup is delivered by SSH to that IP, so with two
# engines (or an engine and a build VM) up on one node, ARP flaps can land the cleanup
# on a LIVE foreign engine — observed: an engine lost /etc/ssh/ssh_host_* (every session
# reset at kex while the listener stayed up) plus its /opt/quotient/.env and containers
# (known-issues: clean_engine_for_template). The stamp is written when the build VM
# first accepts SSH and removed by the clean, so it exists only while a build is in
# progress and never gets baked into the template.
ENGINE_BUILD_STAMP = "/etc/tezcatlipoca-build-id"


def engine_build_identity(vmid):
    """The identity string a build VM carries. Keyed on the reserved vmid, which is
    deterministic (engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET) and unique per slot."""
    return f"tezcatlipoca-engine-template/{vmid}"


def stamp_engine_build(ctx, vmid):
    """Mark the guest at ctx['scoring_engine_ip'] as this build's VM.

    Called as soon as the freshly cloned build VM accepts SSH, before any other work, so
    a later SSH that resolves to the wrong machine is detectable."""
    return _run_engine_cmd(
        ctx, f"echo '{engine_build_identity(vmid)}' | sudo tee {ENGINE_BUILD_STAMP} >/dev/null",
        timeout=60, step="stamp engine build")


def assert_engine_build_identity(ctx, vmid):
    """Refuse to touch the engine unless the guest at this address is THIS build's VM.

    A missing stamp is fatal, not a warning: it means either we are pointed at a machine
    this pipeline never stamped (a foreign engine, or an engine clone made from a
    template) or the build VM is not the one we think it is. Both are worse than
    stopping."""
    expected = engine_build_identity(vmid)
    result = _run_engine_cmd(ctx, f"cat {ENGINE_BUILD_STAMP} 2>/dev/null || true",
                             timeout=60, capture=True, step="verify engine build identity")
    found = (getattr(result, "stdout", "") or "").strip()
    if found != expected:
        raise RuntimeError(
            f"refusing to run a destructive engine step: the host at "
            f"{ctx.get('scoring_engine_ip')!r} reports build identity {found!r}, expected "
            f"{expected!r}. This is the shared-management-IP hazard — the IP is answering "
            f"for a different machine (a foreign competition's live engine, or a clone of "
            f"a template). Nothing was changed. Give this competition its own "
            f"TF_VAR_engine_mgmt_ip, or stop the other range, then retry.")


# Grow / to the whole disk. Terraform resizes the engine's virtual disk (main.tf: 40 GB)
# but nothing grew the partition/PV/LV/filesystem, so the engine ran on the base image's
# 10 GB root — full within one Windows+Linux deploy (winad-testrun 2026-09-25: 100% used,
# apt-cacher-ng answering 500 to every box; Postgres would be next). Idempotent: growpart
# exits nonzero ("NOCHANGE") once the partition already fills the disk.
_GROW_ROOT_CMD = (
    "ROOT=$(findmnt -no SOURCE /); "
    "if sudo lvs \"$ROOT\" >/dev/null 2>&1; then "
    "PV=$(sudo pvs --noheadings -o pv_name | head -1 | xargs); "
    "DISK=/dev/$(lsblk -no pkname \"$PV\" | head -1); "
    "PART=$(cat /sys/class/block/$(basename \"$PV\")/partition); "
    "sudo growpart \"$DISK\" \"$PART\" >/dev/null; "
    "sudo pvresize \"$PV\" >/dev/null && sudo lvextend -r -l +100%FREE \"$ROOT\" >/dev/null 2>&1; "
    "else "
    "DISK=/dev/$(lsblk -no pkname \"$ROOT\" | head -1); "
    "PART=$(cat /sys/class/block/$(basename \"$ROOT\")/partition); "
    "sudo growpart \"$DISK\" \"$PART\" >/dev/null && sudo resize2fs \"$ROOT\" >/dev/null 2>&1; "
    "fi; "
    "echo \"  engine root: $(df -h / | awk 'NR==2 {print $2\" total, \"$4\" free\"}')\""
)


def prepare_engine_from_template(ctx, postgres_password, redis_password):
    """Per-deploy steps on an engine cloned from the engine template (M4).

    The clone boots with a fresh identity and fresh host keys (baked by the template's
    clean step). .env goes down BEFORE compose up: the fresh postgres volume must
    initialize with this competition's credentials — up-then-rewrite would leave a
    volume initialized with an empty password. compose up on a template with no
    volumes creates exactly that: an empty scoring DB every run."""
    print("  Preparing engine from template (.env + fresh-volume compose up)...")
    _run_engine_cmd(ctx, _GROW_ROOT_CMD, check=False, timeout=120,
                    step="grow engine root filesystem")
    push_quotient_env(ctx, postgres_password, redis_password)
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose up -d", timeout=600,
                    step="compose up")
    _run_engine_cmd(ctx, (
        "ss -ltn | grep -q ':3142 ' && echo '  apt-cacher-ng listening on 3142' "
        "|| sudo systemctl enable --now apt-cacher-ng"
    ), check=False, timeout=60, step="ensure apt-cacher-ng")



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


def read_event_conf(ctx):
    """Pull the engine-authoritative secrets: /opt/quotient/config/event.conf
    (TOML), the linux credlist, and /opt/quotient/.env.

    The engine is the source of truth after any re-bootstrap or partially
    applied seed — .deploy_state.json can drift, these files cannot (the
    scrim-dress-2026-09-20 credential-drift incident). box_password itself is
    baked into the boxes at bootstrap and lives nowhere on the engine, so it
    is NOT recoverable here."""
    import tomllib

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    def _read(path):
        r = subprocess.run(
            ["ssh", "-i", key, *engine_ssh_opts(ctx),
             f"{scoring_user}@{scoring_ip}", f"sudo cat {path}"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        return r.stdout

    secrets = {}
    event = tomllib.loads(_read("/opt/quotient/config/event.conf"))
    admins = event.get("admin") or []
    if admins:
        secrets["admin_password"] = admins[0].get("pw")
    team_pws = {t.get("name"): t.get("pw") for t in event.get("team") or []}
    if team_pws:
        secrets["team_passwords"] = team_pws
    injects = event.get("inject") or []
    if injects:
        secrets["inject_password"] = injects[0].get("pw")

    box_creds = {}
    for line in _read("/opt/quotient/config/credlists/linux.credlist").splitlines():
        line = line.strip()
        if line and "," in line:
            user, pw = line.split(",", 1)
            box_creds[user] = pw
    if box_creds:
        secrets["box_creds"] = box_creds

    # Packet dual-credit adds further credlists (e.g. domain.credlist); recover them all.
    extra = {}
    listing = subprocess.run(
        ["ssh", "-i", key, *engine_ssh_opts(ctx),
         f"{scoring_user}@{scoring_ip}",
         "ls /opt/quotient/config/credlists/ 2>/dev/null || true"],
        capture_output=True, text=True, check=False, timeout=30).stdout
    for fname in listing.split():
        if fname == "linux.credlist" or not fname.endswith(".credlist"):
            continue
        pairs = {}
        for line in _read(f"/opt/quotient/config/credlists/{fname}").splitlines():
            line = line.strip()
            if line and "," in line:
                user, pw = line.split(",", 1)
                pairs[user] = pw
        if pairs:
            extra[fname[:-len(".credlist")]] = pairs
    if extra:
        secrets["extra_credlists"] = extra

    for line in _read("/opt/quotient/.env").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            secrets["postgres_password"] = line.split("=", 1)[1]
        elif line.startswith("REDIS_PASSWORD="):
            secrets["redis_password"] = line.split("=", 1)[1]
    return secrets


def push_event_conf(comp_dir, teams, boxes, ctx, event_name, admin_password,
                    postgres_password, redis_password, box_creds, inject_password=None,
                    extra_credlists=None, scoring_password=None):
    """Build event.conf and push it to the scoring engine with the per-run secrets.

    extra_credlists maps additional credlist names to {user: pw} (packet dual-credit's
    domain.credlist); each is pushed as <name>.credlist next to linux.credlist and must
    be referenced by some check's credlist override so build_event_conf declares it in
    CredlistSettings."""

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    box_services = json.loads((comp_dir / "box_services.json").read_text())

    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
        "boxes_per_team": boxes,
        "team_passwords": {team_key: team_data["password"] for team_key, team_data in teams.items()},
        "event_name": event_name,
        "quotient_admin_password": admin_password,
        "quotient_scoring_password": scoring_password or admin_password,
        "inject_password": inject_password,
    }

    event_conf = build_event_conf(quotient_ctx, box_services)
    event_conf_toml = toml.dumps(event_conf)

    event_conf_b64 = base64.b64encode(event_conf_toml.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            # /opt/quotient is root-owned (sudo git clone at template build); the
            # config dir must be created with sudo too — the clean step removes
            # event.conf/credlists, and an unsudo'd mkdir here died with rc=1 on
            # the M4 validation run (live-found 2026-09-25).
            f"sudo mkdir -p /opt/quotient/config && "
            f"echo '{event_conf_b64}' | base64 -d | sudo tee /opt/quotient/config/event.conf > /dev/null && "
            "sudo chmod 600 /opt/quotient/config/event.conf",
        ],
        check=True, timeout=30,
    )

    credlist = "".join(f"{user},{pw}\n" for user, pw in box_creds.items())
    credlist_b64 = base64.b64encode(credlist.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            f"sudo mkdir -p /opt/quotient/config/credlists && echo '{credlist_b64}' | base64 -d | sudo tee /opt/quotient/config/credlists/linux.credlist > /dev/null && "
            "sudo chmod 600 /opt/quotient/config/credlists/linux.credlist",
        ],
        check=True, timeout=30,
    )

    for list_name, pairs in (extra_credlists or {}).items():
        content = "".join(f"{user},{pw}\n" for user, pw in pairs.items())
        content_b64 = base64.b64encode(content.encode()).decode()
        subprocess.run(
            [
                "ssh", "-i", key,
                *engine_ssh_opts(ctx),
                f"{scoring_user}@{scoring_ip}",
                f"sudo mkdir -p /opt/quotient/config/credlists && echo '{content_b64}' | base64 -d | sudo tee /opt/quotient/config/credlists/{list_name}.credlist > /dev/null && "
                f"sudo chmod 600 /opt/quotient/config/credlists/{list_name}.credlist",
            ],
            check=True, timeout=30,
        )
        print(f"  Pushed credlist {list_name}.credlist ({len(pairs)} account(s))")

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
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env > /dev/null && "
            "sudo chmod 600 /opt/quotient/.env",
        ],
        check=True, timeout=30,
    )

    subprocess.run(
        [
            "ssh", "-i", key,
            *engine_ssh_opts(ctx),
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose restart",
        ],
        check=True, timeout=60,
    )

    print("  Event configuration pushed to scoring engine")
