"""Per-box apt quiesce/index refresh and DNS fix, run before a nakon plant."""

import os
import shlex
import subprocess
import time

from range_ops import diagnose_unreachable_box, guest_agent_exec_root, wait_for_guest_agent
from ssh_ops import gateway_proxy
from utils import DNS_FIX_CMD, DNS_FIX_CMD_ROOT, PRINT_LOCK, record_degradation, run_concurrent
from box_settle_ops import wait_boxes_settled, _box_settled_via_ssh


_APT_PREP_BODY = r"""
set +e
# Non-Debian boxes (fedora/dnf, alpine/apk) have no apt at all — exit before the
# mask/kill/purge noise so the prep is an honest rc=0 no-op instead of 5 retries of
# command-not-found (live-found 2026-09-27, distro-matrix run).
command -v apt-get >/dev/null 2>&1 || exit 0
# Fresh Ubuntu/Debian boots start apt-daily + unattended-upgrades, which (a) hold the
# dpkg lock (nakon's install-package times out) and (b) leave the apt index pointing at a
# package version the mirror has already rotated out (nakon's `apt-get install <svc>` then
# 404s — e.g. nginx 1.24.0-2ubuntu7.17). Kill them FAST (mask --now stops immediately;
# force-kill any stragglers) rather than waiting on the lock — a long-blocking script here
# outlives the ssh -W / guest-agent channel and the whole prep is scored as a failure.
systemctl mask --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service apt-daily.service apt-daily-upgrade.service 2>/dev/null
# Match by exact process name (-x), NOT -f: a `-f unattended-upgr` pattern also matches THIS
# script's own `bash -c` process (its command line contains the pattern), so the prep would
# kill itself mid-run (ssh dies rc=255). systemctl mask --now already stopped the services.
pkill -9 -x unattended-upgrade 2>/dev/null
pkill -9 -x unattended-upgr 2>/dev/null
sleep 2
rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend 2>/dev/null
dpkg --configure -a >/dev/null 2>&1
apt-get -o DPkg::Lock::Timeout=120 update
"""


def _apt_prep_script(gateway=None):
    """The apt-prep script, optionally pointing apt at the engine's apt-cacher-ng cache.

    gateway=None actively REMOVES any proxy file a previous prep wrote, so toggling the
    Compfile's `apt_cache` flag between deploys can't leave stale proxying behind. The
    proxy is an IP (the box's own gateway = the engine's team NIC), so apt-through-proxy
    keeps working when a planted resolv-conf-null-dns breaks the box's DNS — the one
    approved realism trade-off of the cache (see docs/internals.md); the other disruptive
    apt configs (empty sources, package holds) break apt through the proxy too."""
    if gateway:
        proxy_lines = (
            "mkdir -p /etc/apt/apt.conf.d\n"
            f"echo 'Acquire::http::Proxy \"http://{gateway}:3142\";' > /etc/apt/apt.conf.d/95tz-proxy\n"
            f"echo 'Acquire::https::Proxy \"http://{gateway}:3142\";' >> /etc/apt/apt.conf.d/95tz-proxy\n"
        )
    else:
        proxy_lines = "rm -f /etc/apt/apt.conf.d/95tz-proxy\n"
    return proxy_lines + _APT_PREP_BODY


def prep_apt_on_boxes(targets, ctx, use_proxy=True):
    """Quiesce apt-daily/unattended-upgrades and refresh the apt index on every Linux
    target before a Nakon plant. SSH-via-gateway + sudo is the primary channel (the
    guest agent is unreliable in the minute after a tz-base rollback/reboot, when the
    box is busy with unattended-upgrades — it timed out on every box in one run); the
    guest agent (root) is the fallback. Non-fatal per box, but reported clearly.
    use_proxy writes the engine's apt-cacher-ng proxy (Compfile `apt_cache`)."""
    key = ctx["ssh_key_path"]
    box_username = ctx.get("box_username", "ubuntu")
    node = os.environ["TF_VAR_proxmox_node"]
    proxy = gateway_proxy(ctx)
    print("  Prepping apt on Linux boxes (disable apt-daily/unattended-upgrades, refresh index)...")
    # Replaces the old blind 150s sleep: wait for the actual settle condition instead.
    # Runs once; a box that never settles still gets its full retry ladder below.
    # On agentless-data nodes (realm) the settle check falls back to SSH — otherwise
    # every box burns the full budget returning 'cannot verify' forever.
    unsettled = wait_boxes_settled(targets, node, timeout=240,
                                   ssh_fallback=lambda t: _box_settled_via_ssh(ctx, t))
    if unsettled:
        print(f"    WARNING: {len(unsettled)} box(es) never settled after boot — proceeding")
        record_degradation("boxes never settled after boot",
                           ", ".join(str(t.get("ip")) for t in unsettled)[:200])

    def _prep(t):
        ip = t["ip"]
        gateway = f"192.168.{t['identifier']}.1" if use_proxy else None
        script = _apt_prep_script(gateway)
        remote_cmd = f"sudo bash -c {shlex.quote(script)}"
        wait_for_guest_agent(node, t["vmid"], timeout=120)
        for attempt in range(1, 6):
            try:
                r = subprocess.run(
                    ["ssh", "-i", key,
                     "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null",
                     "-o", "ConnectTimeout=10",
                     "-o", f"ProxyCommand={proxy}",
                     f"{box_username}@{ip}", remote_cmd],
                    capture_output=True, text=True, timeout=240,
                )
                if r.returncode == 0:
                    with PRINT_LOCK:
                        print(f"    apt prepped on {ip}")
                    return True
                err = r.stderr or ""
                if "Permission denied" in err or "Host key verification failed" in err:
                    break  # definitive rejection: retries can't help — go to the agent
            except subprocess.TimeoutExpired:
                pass  # box still booting — retry
            if attempt < 5:
                with PRINT_LOCK:
                    print(f"    apt prep attempt {attempt}/5 failed for {ip}, retrying in 15s...")
                time.sleep(15)
        try:
            rc, _out, err = guest_agent_exec_root(node, t["vmid"], script, timeout=240)
            if rc == 0:
                with PRINT_LOCK:
                    print(f"    apt prepped on {ip} (via guest agent)")
            else:
                with PRINT_LOCK:
                    print(f"    WARNING: apt prep rc={rc} on {ip}: {(err or '').strip()[:160]} — proceeding")
                    record_degradation("apt prep failed", f"{ip}: rc={rc} {(err or '').strip()[:160]}")
        except Exception as exc:
            with PRINT_LOCK:
                print(f"    WARNING: apt prep failed on {ip} ({exc}) — proceeding")
                record_degradation("apt prep failed", f"{ip}: {exc}")
        return False

    run_concurrent(targets, _prep)


def fix_dns_on_boxes(targets, ctx):
    """Fix DNS on every target box concurrently, each box with its own full retry ladder.
    All-fail aborts (systemic vs transient)."""

    key = ctx["ssh_key_path"]
    box_username = ctx.get("box_username", "ubuntu")
    node = os.environ["TF_VAR_proxmox_node"]
    proxy = gateway_proxy(ctx)
    print("  Fixing DNS on all team boxes...")

    def _fix(t):
        ip = t["ip"]
        for attempt in range(1, 9):
            try:
                r = subprocess.run(
                    [
                        "ssh", "-i", key,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10",
                        "-o", f"ProxyCommand={proxy}",
                        f"{box_username}@{ip}", DNS_FIX_CMD,
                    ],
                    capture_output=True, text=True, timeout=40,
                )
                if r.returncode == 0:
                    with PRINT_LOCK:
                        print(f"    DNS fixed on {ip}")
                    return True
                err = r.stderr or ""
                if "Permission denied" in err or "Host key verification failed" in err:
                    break  # definitive rejection: retries can't help — go to the agent
            except subprocess.TimeoutExpired:
                pass  # box still booting — retry
            if attempt < 8:
                with PRINT_LOCK:
                    print(f"    DNS fix attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                time.sleep(15)
        try:
            rc, _out, err = guest_agent_exec_root(
                node, t["vmid"], DNS_FIX_CMD_ROOT, timeout=40)
            if rc == 0:
                with PRINT_LOCK:
                    print(f"    DNS fixed on {ip} (via guest agent)")
                return True
            raise RuntimeError(f"rc={rc}: {(err or '').strip()[:120]}")
        except Exception as agent_exc:
            with PRINT_LOCK:
                print(f"  WARNING: DNS fix failed for {ip} after 8 attempts "
                      f"and guest-agent fallback ({agent_exc}) — proceeding anyway")
                record_degradation("DNS fix failed", f"{ip}: {agent_exc}")
                print(diagnose_unreachable_box(node, t["vmid"]))
            return False

    results = run_concurrent(targets, _fix)
    failed = sum(1 for r in results if r is not True)

    if targets and failed == len(targets):
        raise RuntimeError(
            f"DNS fix failed on all {len(targets)} team box(es) after 8 attempts each — this looks "
            f"systemic (see the guest-agent diagnosis above for each box), not a one-off timing "
            f"fluke. Aborting rather than proceeding into Nakon against boxes that are already "
            f"known unreachable."
        )
