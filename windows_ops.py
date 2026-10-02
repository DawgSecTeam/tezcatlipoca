"""Windows provisioning via QEMU guest agent."""

import time

from constants import WINDOWS_ADMIN_USER
from range_ops import guest_agent_exec_windows, wait_for_guest_agent


def is_windows_template(template_name):
    """The single definition of the "win" rule (audit 2026-10-02).

    nakon's machine routing, ssh_ops' gateway auth choice, golden_ops/deploy's target
    split and redeploy's box_platform all classify the same free-text template name, and
    a divergence builds the wrong OS path for a box. There used to be three copies
    (here, ssh_ops, nakon_ops) plus nakon's private alias, with nothing enforcing that
    they agreed; callers import this one instead."""
    return "win" in template_name.lower()


_IMAGE_STATE_PS = (
    "$s = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State' "
    "-ErrorAction SilentlyContinue; if ($s) { $s.ImageState } else { 'IMAGE_STATE_COMPLETE' }"
)


def _wait_for_windows_setup_complete(node, vmid, deadline):
    """Wait out a sysprepped image's first boot (specialize -> reboot -> OOBE).

    The agent answers during specialize, but the box reboots (and renames itself)
    minutes later — anything applied before that races the reboot. Live-found on the
    golden path (winad-testrun 2026-09-25: exec timed out ~370s in, reboot at ~470s,
    settled ~520s). A longer exec timeout alone still races the reboot; wait for the
    image to actually report complete."""
    while time.time() < deadline:
        if wait_for_guest_agent(node, vmid, timeout=max(1, int(deadline - time.time()))):
            try:
                rc, out, _ = guest_agent_exec_windows(node, vmid, _IMAGE_STATE_PS, timeout=60)
                if rc == 0 and "IMAGE_STATE_COMPLETE" in out:
                    return True
            except Exception:
                pass  # agent busy/restarting mid-setup — keep waiting
        time.sleep(15)
    return False


def bootstrap_windows_box(node, vmid, ip, gateway, dns_server, admin_password, timeout=900):
    """Post-clone Windows setup (IP/gateway/DNS, admin password, sshd) via the QEMU guest agent."""
    deadline = time.time() + timeout
    if not _wait_for_windows_setup_complete(node, vmid, deadline):
        # A clone's first boot can deadlock pre-specialize at IDLE cpu with no agent
        # (amongus-cde-2026 2026-09-30: golden-skeld wedged identically on every
        # snapshot-rollback re-boot; a forced stop/start cleared it and specialize
        # then completed). Hard-cycle once and re-wait within the same budget —
        # the physical-power-cycle remedy from known-issues, automated.
        from range_ops import stop_vm, start_vm
        print(f"    vmid {vmid}: setup wait expired with no agent — hard-cycling once")
        stop_vm(node, vmid)
        time.sleep(3)
        start_vm(node, vmid)
        # a fresh full budget: the first-boot wedge clears on the SECOND boot (the
        # second boot's specialize completes — amongus-cde-2026 2026-09-30), and the
        # first wait consumed the original deadline
        if not _wait_for_windows_setup_complete(node, vmid, time.time() + timeout):
            raise RuntimeError(f"vmid {vmid}: Windows setup (sysprep first boot) did not complete "
                               f"within {timeout}s (after one hard-cycle)")

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$adapter = Get-NetAdapter | Where-Object {{ $_.Status -eq 'Up' }} | Select-Object -First 1
if (-not $adapter) {{ throw "no up NetAdapter found" }}
Remove-NetIPAddress -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceIndex $adapter.ifIndex -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress '{ip}' -PrefixLength 24 -DefaultGateway '{gateway}'
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses '{dns_server}'

net user {WINDOWS_ADMIN_USER} "{admin_password}"
# ErrorActionPreference doesn't abort on native exit codes — check it ourselves.
# scrim-extreme-2026-09-20: a policy-rejected password here failed silently and
# every downstream WinRM/paramiko login died with 'Authentication failed'.
if ($LASTEXITCODE -ne 0) {{ throw "net user (admin password) failed, rc=$LASTEXITCODE" }}

Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name sshd -ErrorAction SilentlyContinue
Set-Service -Name QEMU-GA -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name QEMU-GA -ErrorAction SilentlyContinue
# The template ships all three firewall profiles disabled, so every rule below
# (and any later firewall-rule effect or blue hardening) is dead paper until the
# profiles are on. Idempotent; svc-matrix-2026-09-28.
Set-NetFirewallProfile -All -Enabled True -ErrorAction SilentlyContinue | Out-Null
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}}
# The scored RDP check is a plain Tcp dial on 3389; the Remote Desktop rule
# group ships disabled on the template even after the RDP plant flips the
# registry, so the listener is up but every external dial is filtered.
Enable-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP,RemoteDesktop-UserMode-In-UDP -ErrorAction SilentlyContinue
"""
    # The script is idempotent; an agent drop (not a script error) is retried until the
    # deadline. A nonzero rc is a real failure (e.g. password policy) — fail fast.
    # Fresh budget: the setup waits above may have consumed the entry deadline (the
    # hard-cycle path waits out its own 900s), and the exec phase needs its own.
    deadline = time.time() + timeout
    while True:
        try:
            rc, out, err = guest_agent_exec_windows(node, vmid, ps_script, timeout=120)
            break
        except Exception as e:
            if time.time() >= deadline:
                raise RuntimeError(f"vmid {vmid}: bootstrap exec never completed: {e}")
            time.sleep(15)
            wait_for_guest_agent(node, vmid, timeout=max(1, int(deadline - time.time())))
    if rc != 0:
        raise RuntimeError(f"vmid {vmid}: bootstrap script failed (rc={rc}): {err or out}")


def dns_repoint_windows_box(node, vmid, dns_server, timeout=60):
    """Point Windows DNS at the team's DC before domain join."""
    ps_script = f"""
$ErrorActionPreference = 'Stop'
$adapter = Get-NetAdapter | Where-Object {{ $_.Status -eq 'Up' }} | Select-Object -First 1
if (-not $adapter) {{ throw "no up NetAdapter found" }}
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses '{dns_server}'
"""
    rc, out, err = guest_agent_exec_windows(node, vmid, ps_script, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"vmid {vmid}: DNS repoint failed (rc={rc}): {err or out}")


def wait_for_adws(node, vmid, timeout=900):
    """Wait until the AD cmdlets work on a freshly promoted DC (ADWS up), returning the
    DomainSID string, or None on timeout. Never raises.

    sshd comes back well before Active Directory Web Services after the promotion
    reboot; AD-flavored plants run in that window died with ADServerDownException
    (winad-testrun 2026-09-25: svc-support never created, silently — the AD misconfig
    pass is non-strict and outside nakon-config.json's coverage)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, vmid, "(Get-ADDomain -ErrorAction Stop).DomainSID.Value", timeout=60)
            sid = (out or "").strip()
            if rc == 0 and sid.startswith("S-1-5-21-"):
                return sid
        except Exception:
            pass
        time.sleep(15)
    return None


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
    """Poll DC DNS until it serves domain SRV records (DNS may lag guest-agent). Never raises.

    Agent errors don't consume the budget alongside real probes — a DC whose
    agent never answers at all gets its own diagnosis, so 'DNS not up yet'
    isn't confused with 'DC unreachable'."""
    record = f"_ldap._tcp.dc._msdcs.{domain}"
    deadline = time.time() + timeout
    probes = 0
    agent_errors = 0
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, dc_vmid,
                f"@(Resolve-DnsName -Name {record} -Server {dc_ip} -Type SRV -ErrorAction "
                f"SilentlyContinue).Count -gt 0",
                timeout=20,
            )
            probes += 1
            if rc == 0 and out.strip().lower() == "true":
                if agent_errors:
                    print(f"    (DC DNS probe succeeded after {agent_errors} agent error(s))")
                return True
        except Exception:
            agent_errors += 1
        time.sleep(10)
    if probes == 0:
        print(f"    WARNING: the DC's guest agent never answered a single DNS probe in "
              f"{timeout}s — the DC is agent-unreachable, not merely DNS-slow")
    return False
