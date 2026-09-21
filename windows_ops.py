"""Windows provisioning via QEMU guest agent."""

import time

from constants import WINDOWS_ADMIN_USER
from range_ops import guest_agent_exec_windows, wait_for_guest_agent


def is_windows_template(template_name):
    return "win" in template_name.lower()


def bootstrap_windows_box(node, vmid, ip, gateway, dns_server, admin_password, timeout=600):
    """Post-clone Windows setup (IP/gateway/DNS, admin password, sshd) via the QEMU guest agent."""
    if not wait_for_guest_agent(node, vmid, timeout=timeout):
        raise RuntimeError(f"vmid {vmid}: guest agent never became responsive within {timeout}s")

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
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}}
"""
    rc, out, err = guest_agent_exec_windows(node, vmid, ps_script, timeout=90)
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
    """Poll DC DNS until it serves domain SRV records (DNS may lag guest-agent). Never raises."""

    record = f"_ldap._tcp.dc._msdcs.{domain}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, dc_vmid,
                f"@(Resolve-DnsName -Name {record} -Server {dc_ip} -Type SRV -ErrorAction "
                f"SilentlyContinue).Count -gt 0",
                timeout=20,
            )
            if rc == 0 and out.strip().lower() == "true":
                return True
        except Exception:
            pass
        time.sleep(10)
    return False
