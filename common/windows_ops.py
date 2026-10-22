"""Windows provisioning via QEMU guest agent."""

import time

from common.constants import WINDOWS_ADMIN_USER
from infra.guest_exec import guest_agent_exec_windows, wait_for_guest_agent


def is_windows_template(template_name):
# Routing/auth/target-split call sites must agree on the "win" rule.
    return "win" in template_name.lower()


_IMAGE_STATE_PS = (
    "$s = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Setup\\State' "
    "-ErrorAction SilentlyContinue; if ($s) { $s.ImageState } else { 'IMAGE_STATE_COMPLETE' }"
)

# Uncapped nested waits ate whole 900 s windows uninterruptibly; cap so the outer loop re-checks.
AGENT_POLL_CAP = 120

AGENT_PROBE_CAP = 60


def _bounded(timeout, remaining):
    return max(1, min(timeout, AGENT_POLL_CAP, int(remaining)))


def _wait_for_windows_setup_complete(node, vmid, deadline):
# The agent answers during specialize, but the box then reboots and renames itself
# minutes later — anything applied before COMPLETE races the reboot.
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




def _feature_block(features):
    if not features:
        return ""
    feature_list = ", ".join(f"'{f}'" for f in features)
    # A feature install can hit ERROR_SHARING_VIOLATION (0x80070020) while setup/Update
    # holds the file; that is transient, so retry it instead of failing the deploy
    # (live: run-f7976169 failed ad01's bootstrap on AD-Domain-Services).
    return (
        f"$featureOk = $false\n"
        f"for ($attempt = 0; $attempt -lt 3 -and -not $featureOk; $attempt++) {{\n"
        f"  try {{ Install-WindowsFeature -Name @({feature_list}) "
        f"-IncludeManagementTools -ErrorAction Stop | Out-Null; $featureOk = $true }}\n"
        f"  catch {{\n"
        f"    if ($_.Exception.Message -notmatch '0x80070020|being used by another process') "
        f"{{ throw }}\n"
        f"    Start-Sleep -Seconds 20\n"
        f"  }}\n"
        f"}}\n"
        f"if (-not $featureOk) {{ throw 'Install-WindowsFeature failed after 3 attempts' }}\n")

def bootstrap_windows_box(node, vmid, ip, gateway, dns_server, admin_password, timeout=1800,
                          features=()):
# 1800 s: a fresh DC clone's sysprep first boot is IO-bound far past the old 900 s default.
    deadline = time.time() + timeout
    if not _wait_for_windows_setup_complete(node, vmid, deadline):
        # First boot can deadlock pre-specialize (idle CPU, no agent); a stop/start clears it.
        from infra.lifecycle import start_vm, stop_vm
        print(f"    vmid {vmid}: setup wait expired with no agent — hard-cycling once")
        stop_vm(node, vmid)
        time.sleep(3)
        start_vm(node, vmid)
        if not _wait_for_windows_setup_complete(node, vmid, time.time() + timeout):
            raise RuntimeError(f"vmid {vmid}: Windows setup (sysprep first boot) did not complete "
                               f"within {timeout}s (after one hard-cycle)")

    # A competition whose box configures IIS needs the WebAdministration module
    # BEFORE the plan's step runs: the template ships the server role without the
    # management tools, and `Import-Module WebAdministration` then fails with
    # FileNotFoundException (live: scrim-one/win02 IIS FTP, 2026-10-19).
    feature_block = _feature_block(features)
# (the block itself lives in _feature_block, so a test can assert its retry)
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
# Idempotency: on a SECOND bootstrap of the same box the uninstall above has
# already removed the Defender module, and CommandNotFoundException is a
# terminating error per-command — -ErrorAction does not catch it under
# $ErrorActionPreference='Stop', so a resume/re-bootstrap died rc=1 (a golden
# rebuild refused mid-deploy). Ask first, then set.
if (Get-Command Set-MpPreference -ErrorAction SilentlyContinue) {{
    Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction SilentlyContinue
    Set-MpPreference -DisableIOAVProtection $true -ErrorAction SilentlyContinue
    Set-MpPreference -DisableScriptScanning $true -ErrorAction SilentlyContinue
}}
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
# Every CCDC blue team administers Windows over RDP, so it must exist even on a
# competition that does not plant an "RDP misconfig": the template ships RDP
# disabled and the rule group disabled, and without both the scored Tcp dial
# (and a human's mstsc) is filtered (live: cde-2026 ad01/ftp01, 3389 closed).
Set-ItemProperty 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0 -ErrorAction SilentlyContinue
Set-Service -Name TermService -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name TermService -ErrorAction SilentlyContinue
Enable-NetFirewallRule -Name RemoteDesktop-UserMode-In-TCP,RemoteDesktop-UserMode-In-UDP,RemoteDesktop-Shadow-In-TCP -ErrorAction SilentlyContinue
{feature_block}"""
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
    if features:
        # Feature installs stay InstallPending until a real restart; the agent
        # answers during the shutdown window, so wait for the BOOT TIME to change
        # rather than for the first reply (live: ad01 inherited pending features).
        before = _windows_boot_time(node, vmid)
        try:
            guest_agent_exec_windows(node, vmid, "Restart-Computer -Force", timeout=60)
        except Exception:  # the agent drops the channel as the box goes down
            pass
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(10)
            now = _windows_boot_time(node, vmid)
            if now and now != before:
                break
        else:
            from infra.lifecycle import reboot_vm
            reboot_vm(node, vmid)
        wait_for_guest_agent(node, vmid, timeout=600)


def _windows_boot_time(node, vmid):
    try:
        rc, out, _ = guest_agent_exec_windows(
            node, vmid,
            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToFileTimeUtc()",
            timeout=60)
    except Exception:
        return None
    return (out or "").strip() or None


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
# ADWS lags sshd after the promotion reboot — AD plants in that window die ADServerDownException.
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
