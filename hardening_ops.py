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
from utils import DNS_FIX_CMD, DNS_FIX_CMD_ROOT, valid_unix_username


_APT_PREP_SCRIPT = r"""
set +e
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


def prep_apt_on_boxes(targets, ctx):
    """Quiesce apt-daily/unattended-upgrades and refresh the apt index on every Linux
    target before a Nakon plant. SSH-via-gateway + sudo is the primary channel (the
    guest agent is unreliable in the minute after a tz-base rollback/reboot, when the
    box is busy with unattended-upgrades — it timed out on every box in one run); the
    guest agent (root) is the fallback. Non-fatal per box, but reported clearly."""
    key = ctx["ssh_key_path"]
    box_username = ctx.get("box_username", "ubuntu")
    node = os.environ["TF_VAR_proxmox_node"]
    proxy = gateway_proxy(ctx)
    remote_cmd = f"sudo bash -c {shlex.quote(_APT_PREP_SCRIPT)}"
    print("  Prepping apt on Linux boxes (disable apt-daily/unattended-upgrades, refresh index)...")
    # Boxes that just cold-booted from a tz-base rollback are saturated by the initial
    # unattended-upgrades run, which starves BOTH the guest agent and the ssh -W forward for
    # a couple of minutes. Let that settle first, or every prep below times out.
    print("    (letting boxes settle after boot for 150s before prepping)")
    time.sleep(150)
    for t in targets:
        ip = t["ip"]
        wait_for_guest_agent(node, t["vmid"], timeout=120)
        ok = False
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
                print(f"    apt prepped on {ip}")
                ok = True
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 5:
                    print(f"    apt prep attempt {attempt}/5 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
        if ok:
            continue
        try:
            rc, _out, err = guest_agent_exec_root(node, t["vmid"], _APT_PREP_SCRIPT, timeout=240)
            if rc == 0:
                print(f"    apt prepped on {ip} (via guest agent)")
            else:
                print(f"    WARNING: apt prep rc={rc} on {ip}: {(err or '').strip()[:160]} — proceeding")
        except Exception as exc:
            print(f"    WARNING: apt prep failed on {ip} ({exc}) — proceeding")


def fix_dns_on_boxes(targets, ctx):
    """Fix DNS on every target box. All-fail aborts (systemic vs transient)."""

    key = ctx["ssh_key_path"]
    box_username = ctx.get("box_username", "ubuntu")
    node = os.environ["TF_VAR_proxmox_node"]
    proxy = gateway_proxy(ctx)
    print("  Fixing DNS on all team boxes...")
    total = 0
    failed = 0
    for t in targets:
        total += 1
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
                print(f"    DNS fixed on {ip}")
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 8:
                    print(f"    DNS fix attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
                else:
                    try:
                        rc, _out, err = guest_agent_exec_root(
                            node, t["vmid"], DNS_FIX_CMD_ROOT, timeout=40)
                        if rc == 0:
                            print(f"    DNS fixed on {ip} (via guest agent)")
                            break
                        raise RuntimeError(f"rc={rc}: {(err or '').strip()[:120]}")
                    except Exception as agent_exc:
                        print(f"  WARNING: DNS fix failed for {ip} after 8 attempts "
                              f"and guest-agent fallback ({agent_exc}) — proceeding anyway")
                        print(diagnose_unreachable_box(node, t["vmid"]))
                        failed += 1

    if total > 0 and failed == total:
        raise RuntimeError(
            f"DNS fix failed on all {total} team box(es) after 8 attempts each — this looks "
            f"systemic (see the guest-agent diagnosis above for each box), not a one-off timing "
            f"fluke. Aborting rather than proceeding into Nakon against boxes that are already "
            f"known unreachable."
        )


def fix_services_on_boxes(comp_dir, targets, ctx, box_creds):
    """Post-nakon service hardening (bind address, mail, ftp, dns, etc.) using the per-run credlist secrets."""

    creds = box_creds
    cred_items = list(creds.items())

    node = os.environ["TF_VAR_proxmox_node"]
    print("  Hardening services on all team boxes...")
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    for t in targets:
        ip = t["ip"]
        services = box_services.get(t["box_name"], [])

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
                "sudo systemctl start lighttpd 2>/dev/null || true",
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

        script_content = "\n".join(script_lines)
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
                print(f"    Service hardening warning on {ip}: {result.stderr.strip()[:200]}")
                print(f"    Retrying {ip} via guest agent (as root, no sudo needed)...")
                vmid = t["vmid"]
                root_script = re.sub(r"\bsudo ", "", script_content)
                try:
                    rc, out, err = guest_agent_exec_root(node, vmid, root_script, timeout=120)
                    if rc == 0:
                        print(f"    Services hardened on {ip} (via guest agent)")
                    else:
                        print(f"    Service hardening still failing on {ip} via guest agent: "
                              f"rc={rc} {err.strip()[:200]}")
                except Exception as e:
                    print(f"    Guest-agent fallback failed for {ip} (vmid {vmid}): {e}")
            else:
                print(f"    Services hardened on {ip}")
        except Exception as e:
            print(f"    Service hardening error on {ip}: {e}")


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
    auth_cmd = (
        "sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo systemctl restart sshd 2>/dev/null || true; "
        f"echo {sudoers_line} | sudo tee /etc/sudoers.d/{box_username}; "
        f"sudo chmod 440 /etc/sudoers.d/{box_username}"
    )

    for t in targets:
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
                print(f"    Auth configured on {ip}")
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt < 8:
                    print(f"    Auth attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                    time.sleep(15)
                else:
                    print(f"    Auth setup failed for {ip} after 8 attempts — retrying via guest agent (root)...")
                    vmid = t["vmid"]
                    root_script = re.sub(r"\bsudo ", "", auth_cmd)
                    try:
                        rc, out, err = guest_agent_exec_root(
                            os.environ["TF_VAR_proxmox_node"], vmid, root_script, timeout=120)
                        if rc == 0:
                            print(f"    Auth configured on {ip} (via guest agent)")
                        else:
                            raise RuntimeError(
                                f"auth setup failed on {ip} via guest agent too: "
                                f"rc={rc} {err.strip()[:200]}")
                    except RuntimeError:
                        raise
                    except Exception as e:
                        raise RuntimeError(
                            f"auth setup failed on {ip}: SSH dead after 8 attempts and the "
                            f"guest-agent fallback raised ({e})") from e
