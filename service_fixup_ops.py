"""Post-plant service fixups (mysql/postfix/dovecot/...) and credlist re-ensure."""

import base64
import json
import os
import re

from typing import Callable, NamedTuple
from range_ops import guest_agent_exec_detached, guest_agent_exec_root
from ssh_ops import ssh_via_gateway
from utils import PRINT_LOCK, run_concurrent


class _ServiceFixup(NamedTuple):
    """One conditional block of the service-hardening script.

    `triggers` are the catalog config names that select the block — the aliasing is
    data here, not the `or` chain the pre-refactor if-chain spelled out by hand. Those
    hand-written chains had drifted apart, and the drift is real and deliberate:
    the apache gap-filler fires on apache|httpd, the fedora named.conf block fires on
    bind ALONE, and the bind9 options block fires on bind|named|dns. Nothing here
    normalises that; it reproduces it.

    Exactly one of `lines` (a literal fragment) or `build` (a fragment that
    interpolates the credlist accounts) is set. Preserve every byte: this script runs
    on live boxes and has been debugged through several incidents."""

    name: str
    triggers: tuple[str, ...]
    lines: tuple[str, ...] = ()
    build: Callable[[dict[str, str]], list[str]] | None = None


def _credlist_account_lines(creds):
    """The `useradd ... || true` + `chpasswd` pair for every credlist account, in
    credlist order.

    ONE generator, TWO call sites: the script prologue and the postfix fragment. The
    postfix branch used to carry its own copy-pasted pair, so credlist accounts were
    asserted twice on a mail box (harmless — useradd is idempotent, chpasswd
    re-asserts the same password). That duplicate is IN the captured golden, so only
    the generator was deduplicated here; the emitted lines are unchanged."""
    lines = []
    for username, password in creds.items():
        lines.append(f"sudo useradd -m -s /bin/bash {username} 2>/dev/null || true")
        lines.append(f"echo '{username}:{password}' | sudo chpasswd")
    return lines


def _mysql_fixup(creds):
    """MySQL/MariaDB: bind 0.0.0.0, restart, then replay the credlist as DB users.
    Credlist order matters twice: the account lines, and which account gets
    WITH GRANT OPTION (the first one, as before)."""
    lines = [
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
    ]
    for idx, (username, password) in enumerate(creds.items()):
        lines.append(f"CREATE USER IF NOT EXISTS '{username}'@'%' IDENTIFIED BY '{password}';")
        grant = "GRANT ALL PRIVILEGES ON *.* TO '{}'@'%' WITH GRANT OPTION;" if idx == 0 \
            else "GRANT ALL PRIVILEGES ON *.* TO '{}'@'%';"
        lines.append(grant.format(username))
    lines.extend([
        "FLUSH PRIVILEGES;",
        "SQLEOF",
        "chmod 600 /tmp/setup_mysql.sql",
        "sudo mysql < /tmp/setup_mysql.sql || true",
        "rm -f /tmp/setup_mysql.sql",
        "",
    ])
    return lines


def _postfix_fixup(creds):
    """Postfix: listen on all interfaces + re-assert the credlist mail accounts."""
    lines = [
        "# Postfix: ensure it listens on all interfaces",
        "sudo postconf -e 'inet_interfaces = all' 2>/dev/null || true",
        "sudo postconf -e 'inet_protocols = ipv4' 2>/dev/null || true",
        "# Ensure smtpd listener exists (non-interactive install may leave master.cf empty).",
        "sudo postconf -M 'smtp/inet=smtp inet n - y - - smtpd' 2>/dev/null || true",
        "sudo systemctl restart postfix 2>/dev/null || true",
        "sleep 1",
        "# Create mail users matching credlist for SMTP checks",
    ]
    lines.extend(_credlist_account_lines(creds))
    lines.append("")
    return lines


def _dovecot_fixup(creds):
    """Dovecot: plaintext auth + a per-account maildir."""
    lines = [
        "# Dovecot: enable plaintext auth, create mail dirs",
        "sudo sed -i 's/^#*disable_plaintext_auth.*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf 2>/dev/null || true",
        "dovecot --version 2>/dev/null | grep -qE '^(2\\.[4-9]|[3-9]\\.)' && "
        "sudo bash -c \"echo 'auth_allow_cleartext = yes' > "
        "/etc/dovecot/conf.d/99-allow-plaintext.conf\" || true",
    ]
    for username, _password in creds.items():
        lines.append(f"sudo mkdir -p /home/{username}/mail")
        lines.append(f"sudo chmod 700 /home/{username}/mail")
        lines.append(f"sudo chown {username}:{username} /home/{username}/mail 2>/dev/null || true")
    lines.extend([
        "sudo systemctl restart dovecot 2>/dev/null || true",
        "sleep 1",
        "",
    ])
    return lines


# Per-service script fragments, keyed by canonical group name. SERVICE_FIXUP_ORDER is
# the emission order and is OUTPUT, not a preference: the old if-chain appended the
# fedora named.conf block before the bind9 options block, so a box pinned `bind` gets
# both, in that order. Never sort this dict, and never merge the two bind groups.
SERVICE_FIXUPS: dict[str, _ServiceFixup] = {
    "apache": _ServiceFixup("apache", ("apache", "httpd"), lines=(
        # Catalog scoreability gaps on non-Debian packagings
        # (distro-matrix-2026-09-27): the apache/bind dnf/yum branches install+start
        # but never touch distro defaults, so the checks they're pinned for still fail
        # — fedora httpd serves an EMPTY docroot (403 on /) and fedora named listens
        # on 127.0.0.1 behind the systemd-resolved stub (refused from the scoring
        # engine). Idempotent; no-ops on Debian packagings (index.html already exists;
        # the catalog's apt branch already did the resolved/named work).
        "if [ -d /var/www/html ] && [ ! -f /var/www/html/index.html ]; then "
        "echo '<html><body>tz</body></html>' | sudo tee /var/www/html/index.html >/dev/null; fi || true",
        # Emitted, not a Python comment: this line is part of the generated script.
        "# httpd resolves its (unset) ServerName against DNS at boot and can hang "
        "when the resolver isn't up yet (pfsense-ad fedora member); localhost "
        "short-circuits that on every later boot",
        "if [ -d /etc/httpd/conf.d ] && [ ! -f /etc/httpd/conf.d/00-tzc-servername.conf ]; then "
        "echo 'ServerName localhost' | sudo tee /etc/httpd/conf.d/00-tzc-servername.conf >/dev/null; "
        "sudo systemctl restart httpd 2>/dev/null || true; fi || true",
        "",
    )),
    "bind_named_conf": _ServiceFixup("bind_named_conf", ("bind",), lines=(
        "if [ -f /etc/named.conf ]; then "
        "sudo sed -i 's/listen-on port 53 { 127.0.0.1; };/listen-on port 53 { any; };/' /etc/named.conf; "
        "sudo sed -i 's/allow-query\\(.*\\) { localhost; };/allow-query\\1 { any; };/' /etc/named.conf; "
        "sudo mkdir -p /etc/systemd/resolved.conf.d; "
        "printf '[Resolve]\\nDNSStubListener=no\\n' | sudo tee /etc/systemd/resolved.conf.d/no-stub.conf >/dev/null; "
        "sudo systemctl restart systemd-resolved 2>/dev/null || true; "
        "sudo systemctl restart named 2>/dev/null || true; fi || true",
        "",
    )),
    "mysql": _ServiceFixup("mysql", ("mysql", "mariadb"), build=_mysql_fixup),
    "postfix": _ServiceFixup("postfix", ("postfix", "smtp"), build=_postfix_fixup),
    "nginx": _ServiceFixup("nginx", ("nginx", "http", "web"), lines=(
        "# Nginx: ensure it starts and listens on port 80",
        "sudo systemctl restart nginx 2>/dev/null || true",
        "sleep 1",
        "",
    )),
    "vsftpd": _ServiceFixup("vsftpd", ("vsftpd", "ftp"), lines=(
        "# Vsftpd: ensure anonymous is off, local users can login",
        "sudo sed -i 's/^#*anonymous_enable.*/anonymous_enable=NO/' /etc/vsftpd.conf 2>/dev/null || true",
        "sudo sed -i 's/^#*local_enable.*/local_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
        "sudo sed -i 's/^#*write_enable.*/write_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
        "sudo systemctl restart vsftpd 2>/dev/null || true",
        "sleep 1",
        "",
    )),
    "dovecot": _ServiceFixup("dovecot", ("dovecot", "imap"), build=_dovecot_fixup),
    "bind9": _ServiceFixup("bind9", ("bind", "named", "dns"), lines=(
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
    )),
    "telnet": _ServiceFixup("telnet", ("telnet-service", "telnet"), lines=(
        "# Telnet: enable disabled inetd entry and restart.",
        "sudo update-inetd --enable telnet 2>/dev/null || true",
        "sudo systemctl restart inetutils-inetd 2>/dev/null || true",
        "sleep 1",
        "",
    )),
    "splunk": _ServiceFixup("splunk", ("splunk",), lines=(
        "# Splunk: Nakon only creates user, need something on port 8000",
        "sudo apt-get install -y lighttpd 2>/dev/null || true",
        "sudo sed -i 's/server.port.*/server.port = 8000/' /etc/lighttpd/lighttpd.conf 2>/dev/null || true",
        "sudo systemctl enable lighttpd 2>/dev/null || true",
        "# restart, not start: lighttpd may already be running on :80 from the",
        "# plant — start is a no-op on an active unit and the port move never lands",
        "sudo systemctl restart lighttpd 2>/dev/null || true",
        "sleep 1",
        "",
    )),
}

# Emission order, preserved from the pre-refactor if-chain (output depends on it).
SERVICE_FIXUP_ORDER: tuple[str, ...] = (
    "apache", "bind_named_conf", "mysql", "postfix", "nginx",
    "vsftpd", "dovecot", "bind9", "telnet", "splunk",
)


def service_fixup_script(services, creds):
    """Build the per-box service-hardening bash script.

    PURE by design — no I/O, no env, no printing — so tests/test_service_fixups.py
    can compare it byte for byte against a golden captured from the pre-refactor
    code (the script runs on live boxes; a subtle change is a live incident).

    `services` is the box's pin list from box_services.json: catalog config names, or
    per-pin override dicts that ride the file for scoring only (fixups key on the
    catalog name). `creds` is the per-run credlist {username: password} in credlist
    order.

    Block order comes from SERVICE_FIXUP_ORDER, NOT from the pin order: a box pinned
    ["splunk", "nginx"] gets nginx first, exactly as the old if-chain did."""
    names = [s if isinstance(s, str) else s.get("name") for s in services]

    lines = ["#!/bin/bash", "set -e", ""]

    lines.append(f"# Credlist OS accounts ({'/'.join(creds)}) for all auth-based service checks")
    lines.extend(_credlist_account_lines(creds))
    lines.append("")

    lines.extend([
        "# Un-wedge sshd if a burst of ssh-* misconfig restarts tripped the start-limit",
        "sudo systemctl reset-failed ssh 2>/dev/null || true",
        "sudo systemctl is-active --quiet ssh || sudo systemctl start ssh 2>/dev/null || true",
        "",
    ])

    for name in SERVICE_FIXUP_ORDER:
        fixup = SERVICE_FIXUPS[name]
        if not any(trigger in names for trigger in fixup.triggers):
            continue
        lines.extend(fixup.lines if fixup.build is None else fixup.build(creds))

    lines.extend([
        "# Ensure all installed services are running",
        "for svc in mysql mariadb postfix nginx vsftpd dovecot bind9 lighttpd apache2; do",
        "  if systemctl list-unit-files \"$svc.service\" &>/dev/null; then",
        "    sudo systemctl start $svc 2>/dev/null || true",
        "  fi",
        "done",
    ])

    return "\n".join(lines)


def fix_services_on_boxes(comp_dir, targets, ctx, box_creds):
    """Post-nakon service hardening (bind address, mail, ftp, dns, etc.) using the per-run credlist secrets."""

    node = os.environ["TF_VAR_proxmox_node"]
    print("  Hardening services on all team boxes...")
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    # Script construction is pure string building (fast, no I/O) and stays serial; the
    # SSH/guest-agent execution per box is what runs concurrently. The per-service shell
    # lives in SERVICE_FIXUPS, rendered by the pure service_fixup_script() above.
    scripts = [(t, service_fixup_script(box_services.get(t["box_name"], []), box_creds))
               for t in targets]

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
                    # Detached: this script restarts several services and can outlive
                    # the agent channel's budget (the 120s cap is what the exec logs
                    # showed dying mid-plant). The guest-side log is the evidence.
                    res = guest_agent_exec_detached(
                        node, vmid, root_script, f"/tmp/tz-harden-{vmid}.log", timeout=900)
                    if res.rc == 0:
                        with PRINT_LOCK:
                            print(f"    Services hardened on {ip} (via guest agent)")
                    else:
                        with PRINT_LOCK:
                            print(f"    Service hardening still failing on {ip} via guest agent: "
                                  f"rc={res.rc} {res.log[-200:].strip()}")
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


def mysql_credlist_reensure_script(cred_items):
    """Bash that re-creates the credlist SQL accounts, tolerating the post-plant client/server
    TLS mismatch (final-stage mysql flags disable server TLS while the stock client insists
    on it — hence the --skip-ssl fallbacks). Idempotent."""
    lines = ["#!/bin/bash", "set -e", ""]
    lines.append("cat > /tmp/tz-credlist.sql << 'SQLEOF'")
    for username, password in cred_items:
        lines.append(f"CREATE USER IF NOT EXISTS '{username}'@'%' IDENTIFIED BY '{password}';")
        lines.append(f"GRANT ALL PRIVILEGES ON *.* TO '{username}'@'%';")
    lines.append("FLUSH PRIVILEGES;")
    lines.append("SQLEOF")
    lines.append("chmod 600 /tmp/tz-credlist.sql")
    lines.append("sudo mysql < /tmp/tz-credlist.sql 2>/dev/null "
                 "|| sudo mariadb < /tmp/tz-credlist.sql 2>/dev/null "
                 "|| sudo mysql --skip-ssl < /tmp/tz-credlist.sql 2>/dev/null "
                 "|| sudo mariadb --skip-ssl < /tmp/tz-credlist.sql")
    lines.append("rm -f /tmp/tz-credlist.sql")
    return "\n".join(lines)


def reensure_mysql_credlist_users(comp_dir, targets, ctx, box_creds):
    """Re-create the credlist SQL accounts AFTER the phase-6 final-stage pass.

    fix_services creates them pre-plant, but the mysql final-stage plants rebuild the
    auth tables (2026-09-30 testcomp-7box: db01 ended with only the install + plant
    accounts, so the auth-based sql check failed until the users were re-added by
    hand). Idempotent; touches only boxes whose box_services pins include
    mysql/mariadb."""
    cred_items = list(box_creds.items())
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    jobs = []
    for t in targets:
        services = [s if isinstance(s, str) else s.get("name")
                    for s in box_services.get(t["box_name"], [])]
        if any(s in ("mysql", "mariadb") for s in services):
            jobs.append((t, mysql_credlist_reensure_script(cred_items)))
    if not jobs:
        return
    node = os.environ["TF_VAR_proxmox_node"]
    with PRINT_LOCK:
        print("  Re-ensuring credlist SQL users on mysql-pinned box(es) "
              f"({', '.join(t['box_name'] for t, _ in jobs)})...")

    def _exec(item):
        t, script_content = item
        script_b64 = base64.b64encode(script_content.encode()).decode()
        deploy_cmd = (
            f"echo '{script_b64}' | base64 -d > /tmp/tz-mysql-credlist.sh && "
            "chmod 600 /tmp/tz-mysql-credlist.sh && "
            "bash /tmp/tz-mysql-credlist.sh; rc=$?; rm -f /tmp/tz-mysql-credlist.sh; exit $rc"
        )
        try:
            result = ssh_via_gateway(ctx, t["ip"], deploy_cmd, timeout=60,
                                     user=ctx.get("box_username", "ubuntu"))
            if result.returncode != 0:
                raise RuntimeError(f"rc={result.returncode}: {result.stderr.strip()[:160]}")
            with PRINT_LOCK:
                print(f"    Credlist SQL users ensured on {t['ip']}")
        except Exception as e:
            root_script = re.sub(r"\bsudo ", "", script_content)
            try:
                rc, _out, err = guest_agent_exec_root(node, t["vmid"], root_script, timeout=120)
                with PRINT_LOCK:
                    if rc == 0:
                        print(f"    Credlist SQL users ensured on {t['ip']} (via guest agent)")
                    else:
                        print(f"    Credlist SQL re-ensure still failing on {t['ip']} via "
                              f"guest agent: rc={rc} {err.strip()[:160]}")
            except Exception as agent_error:
                with PRINT_LOCK:
                    print(f"    Credlist SQL re-ensure error on {t['ip']}: {e} / "
                          f"guest-agent fallback: {agent_error}")

    run_concurrent(jobs, _exec)
