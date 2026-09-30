"""Post-nakon hardening and DNS/auth fixes for team boxes."""

import base64
import json
import os
import re
import shlex
import subprocess
import time

from range_ops import diagnose_unreachable_box, guest_agent_exec_root, wait_for_guest_agent
from ssh_ops import gateway_proxy, ssh_via_gateway
from utils import DNS_FIX_CMD, DNS_FIX_CMD_ROOT, PRINT_LOCK, run_concurrent, valid_unix_username


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


# Pinned scored service -> (apk package, OpenRC service) for the alpine_services shim
# (ensure_alpine_services). The vulndb catalog service scripts are apt/dnf/yum-only, so
# Alpine boxes get their services here instead (distro-matrix-2026-09-27).
ALPINE_SERVICES = {
    "nginx": ("nginx", "nginx"),
    "apache": ("apache2", "apache2"),
    "bind": ("bind", "named"),
    # sshd ships in the -fix template (nakon's own transport); the shim entry just
    # re-asserts it so an `ssh` pin scores a credlist Ssh check on Alpine boxes.
    "ssh": ("openssh", "sshd"),
}

# Match the upgrade WORKER by full command line. `pgrep -x unattended-upgr` (the
# 15-char truncated comm) ALSO matches Ubuntu's permanent unattended-upgrade-shutdown
# --wait-for-signal daemon, so every Ubuntu box read BUSY forever and each prep_apt
# pass burned the full settle budget (winad-testrun 2026-09-25). The [e] keeps the
# pattern from matching this script's own shell.
_SETTLE_CHECK = (
    # Non-apt distro: nothing can hold a dpkg lock, so it is settled by definition —
    # without this guard `apt-get check` is rc=127 BUSY forever and every such box
    # burns the full 240s settle budget per pass (live-found 2026-09-27).
    "command -v apt-get >/dev/null 2>&1 || { echo SETTLED; exit 0; }\n"
    "pgrep -f '/usr/bin/unattended-upgrad[e]( |$)' >/dev/null 2>&1 && echo BUSY\n"
    "apt-get -o DPkg::Lock::Timeout=1 check >/dev/null 2>&1 || echo BUSY\n"
    "echo SETTLED\n"
)


def _box_settled(node, vmid):
    """True once a freshly booted box has settled: guest agent responds, no
    unattended-upgrades process, dpkg lock free. All via the guest agent (root,
    virtio-serial) — SSH is exactly the channel the boot storm starves. The lock check
    is `apt-get check` with a 1s lock timeout rather than `fuser`: same condition, no
    psmisc dependency on minimal cloud images.

    Returns None (not False) when the agent channel is alive but its data calls are
    useless — the realm node answers pings and returns NULL for every exec result, so
    "no SETTLED seen" there means 'cannot verify', not 'busy'. Treating that as False
    burned the whole 240s settle budget per prep_apt pass on realm (2026-09-25);
    callers fall back to the SSH probe on None."""
    try:
        if not wait_for_guest_agent(node, vmid, timeout=20):
            return False
        _rc, out, _err = guest_agent_exec_root(
            node, vmid, _SETTLE_CHECK, timeout=30)
        out = out or ""
        if "SETTLED" in out:
            return "BUSY" not in out
        return None  # agent ping ok, data channel dead (realm) — let the caller probe SSH
    except Exception:
        return None


def _box_settled_via_ssh(ctx, t):
    """SSH fallback of the settle check, for nodes whose guest agent won't return data."""
    script = _SETTLE_CHECK
    try:
        r = ssh_via_gateway(ctx, t["ip"], f"sudo bash -c {shlex.quote(script)}",
                            timeout=20, user=ctx.get("box_username", "ubuntu"))
    except Exception:
        return False
    return r.returncode == 0 and "SETTLED" in (r.stdout or "") and "BUSY" not in (r.stdout or "")


def wait_boxes_settled(targets, node, timeout=240, ssh_fallback=None):
    """Poll every box until it settles after boot — the real condition the old blind
    150s sleep waited out (the initial unattended-upgrades run starves both the guest
    agent and the ssh -W forward). Boxes poll concurrently, each with the full budget.
    Returns the vmids that never settled; that is not fatal — the caller's per-box
    retry ladder covers a stubborn box exactly as before."""
    deadline = time.time() + timeout

    def _wait(t):
        while time.time() < deadline:
            settled = _box_settled(node, t["vmid"])
            if settled is True:
                return True
            if settled is None and ssh_fallback is not None and ssh_fallback(t):
                return True
            time.sleep(10)
        return False

    results = run_concurrent(targets, _wait)
    return {t["vmid"] for t, r in zip(targets, results) if r is not True}


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

    def _prep(t):
        ip = t["ip"]
        gateway = f"192.168.{t['identifier']}.1" if use_proxy else None
        script = _apt_prep_script(gateway)
        remote_cmd = f"sudo bash -c {shlex.quote(script)}"
        wait_for_guest_agent(node, t["vmid"], timeout=120)
        for attempt in range(1, 6):
            try:
                subprocess.run(
                    ["ssh", "-i", key,
                     "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null",
                     "-o", "ConnectTimeout=10",
                     "-o", f"ProxyCommand={proxy}",
                     f"{box_username}@{ip}", remote_cmd],
                    check=True, timeout=240,
                )
                with PRINT_LOCK:
                    print(f"    apt prepped on {ip}")
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
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
        except Exception as exc:
            with PRINT_LOCK:
                print(f"    WARNING: apt prep failed on {ip} ({exc}) — proceeding")
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
                subprocess.run(
                    [
                        "ssh", "-i", key,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10",
                        "-o", f"ProxyCommand={proxy}",
                        f"{box_username}@{ip}", DNS_FIX_CMD,
                    ],
                    check=True, timeout=40,
                )
                with PRINT_LOCK:
                    print(f"    DNS fixed on {ip}")
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 8:
                    with PRINT_LOCK:
                        print(f"    DNS fix attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
                else:
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
                            print(diagnose_unreachable_box(node, t["vmid"]))
                        return False
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


# Alpine's nginx package ships a stub vhost that returns 404 for everything — the
# Quotient web check needs 200 on /. This conf (base64 to survive the ssh quoting
# layers) replaces the stub.
_ALPINE_NGINX_200_VHOST_B64 = ("c2VydmVyIHsKICAgIGxpc3RlbiA4MCBkZWZhdWx0X3NlcnZlcjsKICAgIGxpc3Rl"
                               "biBbOjpdOjgwIGRlZmF1bHRfc2VydmVyOwogICAgbG9jYXRpb24gLyB7IHJldHVy"
                               "biAyMDAgInR6LWFscGluZSB1cFxuIjsgYWRkX2hlYWRlciBDb250ZW50LVR5cGUg"
                               "dGV4dC9wbGFpbjsgfQp9Cg==")


def ensure_alpine_services(comp_dir, targets, ctx):
    """Install + enable pinned scored services on Alpine boxes via apk + OpenRC.

    The vulndb service configs only speak apt/dnf/yum (and systemd), so on Alpine every
    catalog service step exits 1 in 0 seconds (distro-matrix-2026-09-27). With the
    Compfile `alpine_services` knob set, the golden plant runs non-strict and this shim
    owns Alpine services instead: apk add, rc-update add default, rc-service start, and
    a status probe as the pass condition. Idempotent — clones inherit the golden disk,
    so phase 5's pass usually confirms rather than installs. Unknown pinned services
    are reported, not silently skipped."""
    proxy = gateway_proxy(ctx)
    box_username = ctx.get("box_username", "ubuntu")
    pins = json.loads((comp_dir / "box_services.json").read_text())
    alpine = [t for t in targets
              if "alpine" in str(t.get("box", {}).get("template") or t.get("template", "")).lower()]
    if not alpine:
        return

    print("  Ensuring pinned services on Alpine boxes (apk + OpenRC shim)...")
    pending = []
    for t in alpine:
        box_name = t.get("box_name") or t["box"]["name"]
        pin_names = [s if isinstance(s, str) else s.get("name") for s in pins.get(box_name, [])]
        unknown = [s for s in pin_names if s not in ALPINE_SERVICES]
        if unknown:
            raise RuntimeError(
                f"alpine_services shim has no apk mapping for {unknown} pinned on {box_name} — "
                f"add it to hardening_ops.ALPINE_SERVICES or drop the pin")
        wanted = [ALPINE_SERVICES[s] for s in pin_names]
        if wanted:
            pending.append((t, wanted))

    def _ensure(t_wanted):
        t, wanted = t_wanted
        ip = t["ip"]
        markers = " ".join(f"TZ-SVC-OK-{pkg}" for pkg, _svc in wanted)
        blocks = []
        for pkg, svc in wanted:
            block = (f"echo \"TZ-SVC-TRY-{pkg}\"; apk add --no-cache {pkg} && rc-update add {svc} default && "
                     f"{{ rc-service {svc} restart >/dev/null 2>&1 || rc-service {svc} start; }} && ")
            if pkg == "nginx":
                block += (f"echo {_ALPINE_NGINX_200_VHOST_B64} | base64 -d > /etc/nginx/http.d/tz-default.conf && "
                          "rm -f /etc/nginx/http.d/default.conf && ")
            block += f"rc-service {svc} status >/dev/null && echo \"TZ-SVC-OK-{pkg}\""
            blocks.append(block)
        script = "; ".join(blocks)

        def _markers_ok(stdout):
            missing = [m for m in markers.split() if m not in (stdout or "")]
            return missing

        # Primary channel: guest agent as root. The repair sweep's writable-sudoers
        # pin makes sudoers.d world-writable post-clone, sudo then ignores the whole
        # dir and `sudo sh -c` as medic demands a password it can't answer
        # (live-found 2026-09-28). The -fix alpine template ships the agent.
        node = os.environ["TF_VAR_proxmox_node"]
        vmid = t.get("vmid")
        if vmid is not None:
            try:
                wait_for_guest_agent(node, vmid, timeout=120)
                rc, out, err = guest_agent_exec_root(node, vmid, script, timeout=180)
                missing = _markers_ok(out)
                if rc == 0 and not missing:
                    with PRINT_LOCK:
                        print(f"    Alpine services ensured on {ip} ({', '.join(p for p, _ in wanted)}) [guest-agent]")
                    return True
                with PRINT_LOCK:
                    print(f"    Alpine shim guest-agent attempt failed for {ip} "
                          f"(rc={rc}, missing={missing}) {(err or '').strip()[:160]}")
            except Exception as exc:
                with PRINT_LOCK:
                    print(f"    Alpine shim guest-agent channel failed for {ip} (vmid {vmid}): {exc} — falling back to ssh")
        for attempt in range(1, 5):
            try:
                r = ssh_via_gateway(ctx, ip, f"sudo sh -c {shlex.quote(script)}",
                                    timeout=180, user=box_username)
                missing = _markers_ok(r.stdout)
                if r.returncode == 0 and not missing:
                    with PRINT_LOCK:
                        print(f"    Alpine services ensured on {ip} ({', '.join(p for p, _ in wanted)})")
                    return True
                err = (r.stderr or "").strip()[:160]
                with PRINT_LOCK:
                    print(f"    Alpine shim attempt {attempt}/4 failed for {ip} "
                          f"(rc={r.returncode}, missing={missing}) {err}")
            except Exception as exc:
                with PRINT_LOCK:
                    print(f"    Alpine shim attempt {attempt}/4 failed for {ip}: {exc}")
            time.sleep(10)
        return False

    results = run_concurrent(pending, _ensure)
    failed = sorted({t["ip"] for (t, _w), r in zip(pending, results) if r is not True})
    if failed:
        raise RuntimeError(f"Alpine service shim failed on {failed} — nginx/scores would be DOWN")


def fix_services_on_boxes(comp_dir, targets, ctx, box_creds):
    """Post-nakon service hardening (bind address, mail, ftp, dns, etc.) using the per-run credlist secrets."""

    creds = box_creds
    cred_items = list(creds.items())

    node = os.environ["TF_VAR_proxmox_node"]
    print("  Hardening services on all team boxes...")
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    # Script construction is pure string building (fast, no I/O) and stays serial;
    # the SSH/guest-agent execution per box is what runs concurrently.
    scripts = []
    for t in targets:
        # per-pin override dicts ride box_services.json for scoring only; fixups key
        # on the catalog config name
        services = [s if isinstance(s, str) else s.get("name")
                    for s in box_services.get(t["box_name"], [])]

        script_lines = ["#!/bin/bash", "set -e", ""]

        script_lines.append(f"# Credlist OS accounts ({'/'.join(creds)}) for all auth-based service checks")
        for username, password in cred_items:
            script_lines.append(f"sudo useradd -m -s /bin/bash {username} 2>/dev/null || true")
            script_lines.append(f"echo '{username}:{password}' | sudo chpasswd")
        script_lines.append("")

        script_lines.extend([
            "# Un-wedge sshd if a burst of ssh-* misconfig restarts tripped the start-limit",
            "sudo systemctl reset-failed ssh 2>/dev/null || true",
            "sudo systemctl is-active --quiet ssh || sudo systemctl start ssh 2>/dev/null || true",
            "",
        ])

        # Catalog scoreability gaps on non-Debian packagings (distro-matrix-2026-09-27):
        # the apache/bind dnf/yum branches install+start but never touch distro defaults,
        # so the checks they're pinned for still fail — fedora httpd serves an EMPTY
        # docroot (403 on /) and fedora named listens on 127.0.0.1 behind the
        # systemd-resolved stub (refused from the scoring engine). Idempotent; no-ops on
        # Debian packagings (index.html already exists; the catalog's apt branch already
        # did the resolved/named work).
        if any(s in ("apache", "httpd") for s in services):
            script_lines.extend([
                "if [ -d /var/www/html ] && [ ! -f /var/www/html/index.html ]; then "
                "echo '<html><body>tz</body></html>' | sudo tee /var/www/html/index.html >/dev/null; fi || true",
                "# httpd resolves its (unset) ServerName against DNS at boot and can hang "
                "when the resolver isn't up yet (pfsense-ad fedora member); localhost "
                "short-circuits that on every later boot",
                "if [ -d /etc/httpd/conf.d ] && [ ! -f /etc/httpd/conf.d/00-tzc-servername.conf ]; then "
                "echo 'ServerName localhost' | sudo tee /etc/httpd/conf.d/00-tzc-servername.conf >/dev/null; "
                "sudo systemctl restart httpd 2>/dev/null || true; fi || true",
                "",
            ])
        if "bind" in services:
            script_lines.extend([
                "if [ -f /etc/named.conf ]; then "
                "sudo sed -i 's/listen-on port 53 { 127.0.0.1; };/listen-on port 53 { any; };/' /etc/named.conf; "
                "sudo sed -i 's/allow-query\\(.*\\) { localhost; };/allow-query\\1 { any; };/' /etc/named.conf; "
                "sudo mkdir -p /etc/systemd/resolved.conf.d; "
                "printf '[Resolve]\\nDNSStubListener=no\\n' | sudo tee /etc/systemd/resolved.conf.d/no-stub.conf >/dev/null; "
                "sudo systemctl restart systemd-resolved 2>/dev/null || true; "
                "sudo systemctl restart named 2>/dev/null || true; fi || true",
                "",
            ])

        if "mysql" in services or "mariadb" in services:
            script_lines.extend([
                "# MySQL/MariaDB: bind to 0.0.0.0",
                "# Try all possible config file locations",
                "for cnf in /etc/mysql/mysql.conf.d/50-server.cnf /etc/mysql/mariadb.conf.d/50-server.cnf /etc/mysql/my.cnf; do",
                '  if [ -f "$cnf" ]; then',
                "    sudo sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' \"$cnf\"",
                "  fi",
                "done",
                "sudo systemctl restart mysql 2>/dev/null || sudo systemctl restart mariadb 2>/dev/null || true",
                "sleep 2",
                "# Create MySQL users from credlist",
                "cat > /tmp/setup_mysql.sql << 'SQLEOF'",
            ])
            for idx, (username, password) in enumerate(cred_items):
                script_lines.append(f"CREATE USER IF NOT EXISTS '{username}'@'%' IDENTIFIED BY '{password}';")
                grant = "GRANT ALL PRIVILEGES ON *.* TO '{}'@'%' WITH GRANT OPTION;" if idx == 0 \
                    else "GRANT ALL PRIVILEGES ON *.* TO '{}'@'%';"
                script_lines.append(grant.format(username))
            script_lines.extend([
                "FLUSH PRIVILEGES;",
                "SQLEOF",
                "chmod 600 /tmp/setup_mysql.sql",
                "sudo mysql < /tmp/setup_mysql.sql || true",
                "rm -f /tmp/setup_mysql.sql",
                "",
            ])

        if "postfix" in services or "smtp" in services:
            script_lines.extend([
                "# Postfix: ensure it listens on all interfaces",
                "sudo postconf -e 'inet_interfaces = all' 2>/dev/null || true",
                "sudo postconf -e 'inet_protocols = ipv4' 2>/dev/null || true",
                "# Ensure smtpd listener exists (non-interactive install may leave master.cf empty).",
                "sudo postconf -M 'smtp/inet=smtp inet n - y - - smtpd' 2>/dev/null || true",
                "sudo systemctl restart postfix 2>/dev/null || true",
                "sleep 1",
                "# Create mail users matching credlist for SMTP checks",
            ])
            for username, password in cred_items:
                script_lines.append(f"sudo useradd -m -s /bin/bash {username} 2>/dev/null || true")
                script_lines.append(f"echo '{username}:{password}' | sudo chpasswd")
            script_lines.append("")

        if "nginx" in services or "http" in services or "web" in services:
            script_lines.extend([
                "# Nginx: ensure it starts and listens on port 80",
                "sudo systemctl restart nginx 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "vsftpd" in services or "ftp" in services:
            script_lines.extend([
                "# Vsftpd: ensure anonymous is off, local users can login",
                "sudo sed -i 's/^#*anonymous_enable.*/anonymous_enable=NO/' /etc/vsftpd.conf 2>/dev/null || true",
                "sudo sed -i 's/^#*local_enable.*/local_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
                "sudo sed -i 's/^#*write_enable.*/write_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
                "sudo systemctl restart vsftpd 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "dovecot" in services or "imap" in services:
            script_lines.extend([
                "# Dovecot: enable plaintext auth, create mail dirs",
                "sudo sed -i 's/^#*disable_plaintext_auth.*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf 2>/dev/null || true",
                "dovecot --version 2>/dev/null | grep -qE '^(2\\.[4-9]|[3-9]\\.)' && "
                "sudo bash -c \"echo 'auth_allow_cleartext = yes' > "
                "/etc/dovecot/conf.d/99-allow-plaintext.conf\" || true",
            ])
            for username, _ in cred_items:
                script_lines.append(f"sudo mkdir -p /home/{username}/mail")
                script_lines.append(f"sudo chmod 700 /home/{username}/mail")
                script_lines.append(f"sudo chown {username}:{username} /home/{username}/mail 2>/dev/null || true")
            script_lines.extend([
                "sudo systemctl restart dovecot 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "bind" in services or "named" in services or "dns" in services:
            script_lines.extend([
                "# Bind9: allow queries from anywhere",
                # The nakon bind plant declares zone "localhost" in
                # named.conf.local; on Ubuntu named.conf.default-zones already
                # declares it and named refuses to start on the duplicate. The
                # default-zones copy serves db.local (A localhost -> 127.0.0.1),
                # which is what the scored Dns check resolves.
                "sudo python3 -c \"import re; p='/etc/bind/named.conf.local'; \""
                "\"s=open(p).read(); \""
                "\"open(p,'w').write(re.sub(r'zone \\\"localhost\\\" \\{[^}]*\\};\\\\n?', '', s))\""
                " 2>/dev/null || true",
                "cat > /tmp/named.conf.options << 'BIND9EOF'",
                "options {",
                '  directory "/var/cache/bind";',
                "  recursion yes;",
                "  allow-query { any; };",
                "  forwarders { 8.8.8.8; 1.1.1.1; };",
                "};",
                "BIND9EOF",
                "sudo cp /tmp/named.conf.options /etc/bind/named.conf.options",
                "if [ ! -f /etc/bind/named.conf.default-zones ]; then",
                "  cat > /tmp/named.conf.default-zones << 'BZEOF'",
                'zone "." {',
                "  type hint;",
                '  file "/usr/share/dns/root.hints";',
                "};",
                'zone "localhost" {',
                "  type master;",
                '  file "/etc/bind/db.local";',
                "};",
                'zone "127.in-addr.arpa" {',
                "  type master;",
                '  file "/etc/bind/db.127";',
                "};",
                "BZEOF",
                "  sudo cp /tmp/named.conf.default-zones /etc/bind/named.conf.default-zones",
                "fi",
                "if [ ! -f /etc/bind/db.local ]; then",
                "  cat > /tmp/db.local << 'DLEOF'",
                '$TTL 86400',
                '@   IN  SOA ns1.localhost. root.localhost. (',
                '        2026071201',
                '        3600',
                '        1800',
                '        604800',
                '        86400 )',
                '    IN  NS  ns1.localhost.',
                'ns1 IN  A   127.0.0.1',
                '@   IN  A   127.0.0.1',
                "DLEOF",
                "  sudo cp /tmp/db.local /etc/bind/db.local",
                "fi",
                "sudo systemctl restart bind9 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "telnet-service" in services or "telnet" in services:
            script_lines.extend([
                "# Telnet: enable disabled inetd entry and restart.",
                "sudo update-inetd --enable telnet 2>/dev/null || true",
                "sudo systemctl restart inetutils-inetd 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        if "splunk" in services:
            script_lines.extend([
                "# Splunk: Nakon only creates user, need something on port 8000",
                "sudo apt-get install -y lighttpd 2>/dev/null || true",
                "sudo sed -i 's/server.port.*/server.port = 8000/' /etc/lighttpd/lighttpd.conf 2>/dev/null || true",
                "sudo systemctl enable lighttpd 2>/dev/null || true",
                "# restart, not start: lighttpd may already be running on :80 from the",
                "# plant — start is a no-op on an active unit and the port move never lands",
                "sudo systemctl restart lighttpd 2>/dev/null || true",
                "sleep 1",
                "",
            ])

        script_lines.extend([
            "# Ensure all installed services are running",
            "for svc in mysql mariadb postfix nginx vsftpd dovecot bind9 lighttpd apache2; do",
            "  if systemctl list-unit-files \"$svc.service\" &>/dev/null; then",
            "    sudo systemctl start $svc 2>/dev/null || true",
            "  fi",
            "done",
        ])

        scripts.append((t, "\n".join(script_lines)))

    def _exec(item):
        t, script_content = item
        ip = t["ip"]
        script_b64 = base64.b64encode(script_content.encode()).decode()

        deploy_cmd = (
            f"echo '{script_b64}' | base64 -d > /tmp/harden.sh && "
            "chmod 600 /tmp/harden.sh && "
            "bash /tmp/harden.sh; rc=$?; rm -f /tmp/harden.sh; exit $rc"
        )

        try:
            result = ssh_via_gateway(ctx, ip, deploy_cmd, timeout=60,
                                      user=ctx.get("box_username", "ubuntu"))
            if result.returncode != 0:
                with PRINT_LOCK:
                    print(f"    Service hardening warning on {ip}: {result.stderr.strip()[:200]}")
                    print(f"    Retrying {ip} via guest agent (as root, no sudo needed)...")
                vmid = t["vmid"]
                root_script = re.sub(r"\bsudo ", "", script_content)
                try:
                    rc, out, err = guest_agent_exec_root(node, vmid, root_script, timeout=120)
                    if rc == 0:
                        with PRINT_LOCK:
                            print(f"    Services hardened on {ip} (via guest agent)")
                    else:
                        with PRINT_LOCK:
                            print(f"    Service hardening still failing on {ip} via guest agent: "
                                  f"rc={rc} {err.strip()[:200]}")
                except Exception as e:
                    with PRINT_LOCK:
                        print(f"    Guest-agent fallback failed for {ip} (vmid {vmid}): {e}")
            else:
                with PRINT_LOCK:
                    print(f"    Services hardened on {ip}")
        except Exception as e:
            with PRINT_LOCK:
                print(f"    Service hardening error on {ip}: {e}")

    run_concurrent(scripts, _exec)


def setup_ubuntu_auth(targets, ctx):
    """Enable password auth + NOPASSWD sudo for box_username (nakon uses password auth + sudo).

    Raises when a box ends the pass without working auth: nakon authenticates
    with these credentials, so marching into the plant against a box that
    provably can't SSH just defers the failure into noisy per-config timeouts."""

    key = ctx["ssh_key_path"]
    box_username = ctx.get("box_username", "ubuntu")
    if not valid_unix_username(box_username):
        raise RuntimeError(
            f"box_username {box_username!r} is not a safe sudoers filename/remote-shell token"
        )
    proxy = gateway_proxy(ctx)

    print(f"  Enabling password auth + NOPASSWD sudo for {box_username} on team boxes...")
    sudoers_line = shlex.quote(f"{box_username} ALL=(ALL) NOPASSWD:ALL")
    # sshd_config.d drop-in: cloud-init ships `PasswordAuthentication no` in
    # /etc/ssh/sshd_config.d/50-cloud-init.conf (alpine; distro-matrix-2026-09-27), and
    # OpenSSH keeps the FIRST value seen — the Include beats the main-file sed lines
    # below. A 00- drop-in wins on every distro with the include; the sed lines still
    # cover distros whose sshd_config has no include.
    auth_cmd = (
        "sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo mkdir -p /etc/ssh/sshd_config.d; "
        "printf 'PasswordAuthentication yes\\n' | sudo tee /etc/ssh/sshd_config.d/00-tz-password-auth.conf >/dev/null; "
        "sudo systemctl restart sshd 2>/dev/null || true; "
        f"echo {sudoers_line} | sudo tee /etc/sudoers.d/{box_username}; "
        f"sudo chmod 440 /etc/sudoers.d/{box_username}"
    )

    def _auth(t):
        ip = t["ip"]
        for attempt in range(1, 9):
            try:
                subprocess.run(
                    [
                        "ssh", "-i", key,
                        "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10",
                        "-o", f"ProxyCommand={proxy}",
                        f"{box_username}@{ip}", auth_cmd,
                    ],
                    check=True, timeout=40,
                )
                with PRINT_LOCK:
                    print(f"    Auth configured on {ip}")
                return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 8:
                    with PRINT_LOCK:
                        print(f"    Auth attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
                else:
                    with PRINT_LOCK:
                        print(f"    Auth setup failed for {ip} after 8 attempts — retrying via guest agent (root)...")
                    vmid = t["vmid"]
                    root_script = re.sub(r"\bsudo ", "", auth_cmd)
                    try:
                        rc, out, err = guest_agent_exec_root(
                            os.environ["TF_VAR_proxmox_node"], vmid, root_script, timeout=120)
                        if rc == 0:
                            with PRINT_LOCK:
                                print(f"    Auth configured on {ip} (via guest agent)")
                            return True
                        raise RuntimeError(
                            f"auth setup failed on {ip} via guest agent too: "
                            f"rc={rc} {err.strip()[:200]}")
                    except RuntimeError:
                        raise
                    except Exception as e:
                        raise RuntimeError(
                            f"auth setup failed on {ip}: SSH dead after 8 attempts and the "
                            f"guest-agent fallback raised ({e})") from e
        return False

    results = run_concurrent(targets, _auth)
    for t, r in zip(targets, results):
        if r is True:
            continue
        if isinstance(r, Exception):
            raise r
        raise RuntimeError(f"auth setup failed on {t['ip']} after 8 attempts and the fallback")
