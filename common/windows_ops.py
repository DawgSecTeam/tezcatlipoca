"""Windows provisioning via QEMU guest agent."""

import time

from common.constants import WINDOWS_ADMIN_USER
from infra.guest_exec import guest_agent_exec_windows, wait_for_guest_agent


def is_windows_template(template_name):
# Single definition of the "win" rule; the routing/auth/target-split call sites must agree.
    return "win" in template_name.lower()


_IMAGE_STATE_PS = (
    "$s = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State' "
    "-ErrorAction SilentlyContinue; if ($s) { $s.ImageState } else { 'IMAGE_STATE_COMPLETE' }"
)

# Cap on one nested agent wait so the enclosing loop re-checks its own deadline; an uncapped
# wait consumed a whole 900 s window in one uninterruptible call.
AGENT_POLL_CAP = 120

# No single domain-chain probe may consume the whole helper deadline.
AGENT_PROBE_CAP = 60


def _bounded(timeout, remaining):
# No single agent call may outlive the loop waiting on it: the smallest of the three wins.
    return max(1, min(timeout, AGENT_POLL_CAP, int(remaining)))


def _wait_for_windows_setup_complete(node, vmid, deadline):
# Wait out a sysprepped image's first boot (specialize -> reboot -> OOBE). The agent answers
# during specialize, but the box reboots and renames itself minutes later, so anything applied
# before that races the reboot.
    while time.time() < deadline:
        remaining = deadline - time.time()
        if wait_for_guest_agent(node, vmid, timeout=_bounded(AGENT_POLL_CAP, remaining)):
            try:
                rc, out, _ = guest_agent_exec_windows(
                    node, vmid, _IMAGE_STATE_PS,
                    timeout=_bounded(60, deadline - time.time()))
                if rc == 0 and "IMAGE_STATE_COMPLETE" in out:
                    return True
            except Exception:
                pass  # agent busy/restarting mid-setup — keep waiting
        time.sleep(15)
    return False


def bootstrap_windows_box(node, vmid, ip, gateway, dns_server, admin_password, timeout=1800):
# Post-clone Windows setup (IP/gateway/DNS, admin password, sshd) via the QEMU guest agent.
# 1800 s is the golden-build budget; a fresh DC clone's first boot is IO-bound far longer than
# the post-clone path's old 900 s default.
    deadline = time.time() + timeout
    if not _wait_for_windows_setup_complete(node, vmid, deadline):
        # A clone's first boot can deadlock pre-specialize at idle CPU with no agent; a forced
        # stop/start clears it (golden boxes wedged identically on snapshot rollback).
        from infra.lifecycle import start_vm, stop_vm
        print(f"    vmid {vmid}: setup wait expired with no agent — hard-cycling once")
        stop_vm(node, vmid)
        time.sleep(3)
        start_vm(node, vmid)
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
# ErrorActionPreference doesn't abort on native exit codes — check it ourselves:
# a policy-rejected password here failed silently and
# every downstream WinRM/paramiko login died with 'Authentication failed'.
if ($LASTEXITCODE -ne 0) {{ throw "net user (admin password) failed, rc=$LASTEXITCODE" }}

Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name sshd -ErrorAction SilentlyContinue
# Defender is REMOVED on every Windows box (competition standard, owner
# directive 2026-10-18). It quarantines the red team's payloads on sight: live
# dc01 deleted the apollo agent AND its scheduled task within seconds of every
# plant (ThreatID 2147958560) while the same payload ran on win02. Every
# competition image ships Defender removed for exactly this reason.
try {{ Uninstall-WindowsFeature -Name Windows-Defender -ErrorAction SilentlyContinue | Out-Null }} catch {{}}
Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction SilentlyContinue
Set-MpPreference -DisableIOAVProtection $true -ErrorAction SilentlyContinue
Set-MpPreference -DisableScriptScanning $true -ErrorAction SilentlyContinue
sc.exe config WinDefend start= disabled | Out-Null
sc.exe stop WinDefend | Out-Null
New-Item -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' -Force -ErrorAction SilentlyContinue | Out-Null
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' -Name DisableAntiSpyware -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-ItemProperty -Path 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows Defender' -Name DisableAntiVirus -Value 1 -Type DWord -Force -ErrorAction SilentlyContinue
Set-Service -Name QEMU-GA -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name QEMU-GA -ErrorAction SilentlyContinue
# The template ships all three firewall profiles disabled, so every rule below
# (and any later firewall-rule effect or blue hardening) is dead paper until the
# profiles are on. Idempotent.
Set-NetFirewallProfile -All -Enabled True -ErrorAction SilentlyContinue | Out-Null
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}}
# The scored RDP check is a plain Tcp dial on 3389; the Remote Desktop rule
# group ships disabled on the template even after the RDP plant flips the
# registry, so the listener is up but every external dial is filtered.
Enable-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP,RemoteDesktop-UserMode-In-UDP -ErrorAction SilentlyContinue
"""
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
# sshd comes back well before ADWS after the promotion reboot, so AD plants run in that
# window die with ADServerDownException; returns the DomainSID string or None.
    deadline = time.time() + timeout
    errors = 0
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, vmid, "(Get-ADDomain -ErrorAction Stop).DomainSID.Value",
                timeout=_bounded(AGENT_PROBE_CAP, deadline - time.time()))
            sid = (out or "").strip()
            if rc == 0 and sid.startswith("S-1-5-21-"):
                return sid
        except Exception:
            errors += 1
        time.sleep(15)
    if errors:
        # Distinguishes an agent-unreachable DC from a merely slow ADWS.
        print(f"    WARNING: ADWS never came up in {timeout}s and {errors} agent probe(s) "
              f"failed — the DC may be agent-unreachable rather than slow")
    return None


def wait_for_windows_sshd(node, vmid, timeout=180):
# Agent up does not guarantee sshd up after an ADDS reboot.
    deadline = time.time() + timeout
    errors = 0
    while time.time() < deadline:
        try:
            rc, out, _ = guest_agent_exec_windows(
                node, vmid, "(Get-Service sshd -ErrorAction SilentlyContinue).Status",
                timeout=_bounded(20, deadline - time.time()))
            if rc == 0 and out.strip() == "Running":
                return True
        except Exception:
            errors += 1
        time.sleep(10)
    if errors:
        print(f"    WARNING: sshd was not confirmed Running within {timeout}s and {errors} "
              f"agent probe(s) failed — the box may be agent-unreachable")
    return False


def wait_for_dc_dns(node, dc_vmid, domain, dc_ip, timeout=300):
# Agent errors don't consume the probe budget, so 'DNS not up yet' isn't confused with
# 'DC unreachable'.
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
                timeout=_bounded(20, deadline - time.time()),
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
