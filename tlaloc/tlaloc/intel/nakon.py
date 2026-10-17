"""Nakon intel: facts the club already plants, read instead of guessed.

This is the sanctioned cheat the plan calls for (principle 4: facts belong to
nakon). It parses the competition artifacts staged on red01 into ground-truth
credentials, the planted-vuln set per box, and the service/pin inventory, with
technique hints for slugs whose exploitation is not obvious from the name.

Nothing here is hand-maintained: every value comes from a nakon artifact
(`nakon-config.json`, `.deploy_state.json`, `box_vulns.json`,
`box_services.json`, `users.json`). A slug that nakon does not know still works —
it degrades to the raw slug with dashes as spaces.
"""

from .base import Intel

# vuln slug -> one-line exploitation hint (fed to the digest / arbiters).
VULN_HINTS = {
    "sudo-nopasswd": "sudo -n runs anything as root",
    "writable-sudoers": "/etc/sudoers is writable — append NOPASSWD",
    "wildcard-sudo": "sudo wildcard abuse (tar/less GTFOBins wildcard)",
    "suid-find": "SUID find: find . -exec /bin/sh -p -c id \\;",
    "suid-vim": "SUID vim: vim -c ':!sh'",
    "suid-nano": "SUID nano: ^T runs commands",
    "suid-python": "SUID python3 setuid(0) escape",
    "suid-ed": "SUID ed: shell escape",
    "suid-binary": "SUID root shell clone at an odd path (see vars DEST)",
    "file-capability": "binary with file capabilities (cap_setuid default) — quieter than SUID",
    "uid0-backdoor-account": "planted UID0 account — look in vars for its name",
    "multiple-uid0-accounts": "extra UID0 accounts in /etc/passwd",
    "uid0-user": "extra UID0 account (see vars)",
    "empty-password-account": "account with empty password (SSH may allow it)",
    "weak-password-account": "weak known password — see vars",
    "www-data-shell": "www-data has a login shell",
    "authorized-keys-backdoor": "rogue authorized_keys on the box",
    "ssh-root-login": "root SSH login enabled",
    "ssh-empty-passwords": "SSH allows empty passwords",
    "ssh-password-auth": "SSH password auth enabled",
    "ssh-backdoor-listener-2222": "second sshd on :2222 — hardening :22 does not touch it",
    "sftp-subsystem-enabled": "SFTP enabled — file channel over SSH with no shell",
    "mysql-remote-root": "MySQL root reachable remotely",
    "mysql-root-nopassword": "MySQL root with no password",
    "redis-no-auth": "unauthenticated Redis",
    "postgresql-no-auth": "PostgreSQL trust auth",
    "nfs-no-root-squash": "NFS export without root squash",
    "pam-permit-auth": "PAM pam_permit on common-auth — ANY password logs in as any local user",
    "pam-permit-empty": "pam_permit in system-auth/login/su — any/empty password accepted",
    "passwordless-sudo": "per-user NOPASSWD sudo (see vars for the account)",
    "user-sudo-nopasswd": "user account with passwordless sudo (see vars)",
    "sudoers-dropin-world-admins": "sudoers.d NOPASSWD:ALL for a whole group — members are root",
    "sudoers-gtfobins-allowlist": "sudo allowlist of GTFOBins — argument/wildcard abuse",
    "sudo-preserve-env": "sudo preserves attacker-useful env (LD_*)",
    "world-writable-cron-d": "/etc/cron.d world-writable — drop a root cron job",
    "world-writable-crond": "/etc/cron.d world-writable — drop a root cron job",
    "exploitable-cron-job": "root cron runs a world-writable script — overwrite for root",
    "backdoored-core-utils": "curl/wget wrappers exec a payload before the real binary",
    "uac-disabled-win": "UAC disabled",
    "rdp-no-nla-win": "RDP without NLA",
    "smb1-enabled-win": "SMB1 enabled",
    "guest-enabled-win": "guest account enabled",
    "Elevate Guest Account": "Guest is in Administrators/Domain Admins — guest login is admin",
    "extra-admins-win": "extra local admin, password==username (see vars)",
    "rid-hijack-win": "Guest SAM F-value hijacked to RID 500 — Guest logons carry admin token",
    "anon-smb-share-win": "anonymous SMB share + Everyone token — credless file channel",
    "smb-anon-token-win": "EveryoneIncludesAnonymous=1 — null sessions get Everyone rights",
    "insecure-smb-share-win": "SMB share with Everyone:Full (see vars SHARE_NAME/PATH)",
    "anonymous-enumeration-win": "RestrictAnonymous=0 — unauthenticated account/share listing",
    "windows-firewall-allow-all-strict": "Windows firewall allow-all rules planted",
    "windows-adminsdholder-everyone-ace": "AdminSDHolder Everyone:GenericAll (DC) — any user can reach DA",
    "remote-admin-duplication-win": "VNC+TeamViewer installed — default VNC password unless vars",
    "ifeo-backdoor-win": "IFEO debugger hijack on tools",
    "journald-vacuum-cron": "root cron vacuums the systemd journal every 15 min — the audit trail is being erased",
    "carrier-elf-section-marker": "legit binary carrying an appended ELF section (marker payload)",
    "xattr-payload-helper": "payload lives in a user xattr on /etc/hosts, read and run by a cron helper",
    "ifeo-tool-block-win": "IFEO Debugger blocklist over ~25 hunting tools — procmon/autorunsc/sysmon refuse to start",
    "windows-ads-stored-payload": "marker payload in an NTFS ADS on the hosts file, decoded by a SYSTEM task hourly",
    "windows-appinit-hook-registry": "AppInit_DLLs/AppCertDLLs machine-wide hook registration pointing at a planted DLL",
    "windows-hidden-scheduled-task": "SYSTEM at-logon task registered hidden — invisible in the Task Scheduler UI",
    "windows-service-sd-lockdown": "service with a SYSTEM-only SDDL — present but not queryable/deletable by Administrators",
    "windows-wmi-kill-respawn": "WMI deletion-event subscription respawns the marker process within 5s of a kill",
    "windows-deny-delete-artifact": "marker file with a DENY-delete ACE — deletion fails until the ACE is cleared",
    "mysql-udf-sys-exec": "sys_exec/sys_eval UDFs in mysql.func — SELECT sys_eval('id') runs OS commands as mysqld",
    "port-knock-hidden-service": "knockd + inetd root shell behind an iptables DROP — see /etc/knockd.conf",
    "bashrc-command-hijack": "ps/ss shell functions in .bashrc filter an IP from interactive listings",
    "mutual-watchdog-tasks-win": "two scheduled tasks each re-create the other — delete BOTH in one pass",
}

# Plants that re-assert themselves: symptom-fixing regrows them, so the digest
# must say the MECHANISM is the real target.
SELF_HEALING_SLUGS = {
    "cron-sudo-users": "root cron re-adds a sudo account every 5 min",
    "priv-user-respawn-service": "systemd timer re-creates a sudo account every 60s",
    "priv-user-respawn-win": "SYSTEM scheduled task re-adds a local admin every 60s",
    "firewall-flush-refire": "root cron re-flushes firewall rules every minute",
    "cron-watchdog-reinstaller": "root cron restores deleted artifacts from a staged copy",
    "windows-wmi-kill-respawn": "WMI deletion-event subscription respawns a killed process within 5s",
    "windows-deny-delete-artifact": "the DENY-delete ACE survives a plain delete; clear the ACE first",
    "mutual-watchdog-tasks-win": "each task re-creates the other; delete both in one pass",
}

# Any fixed string works on a pam_permit stack; this one is distinctive in logs.
PAM_ANY_PASSWORD = "tlaloc-pam-anything"

_SERVICE_WORDS = ("nginx", "apache", "dovecot", "postfix", "bind", "vsftpd", "mysql",
                  "mariadb", "postgres", "roundcube", "splunk", "telnet", "ssh",
                  "ftp", "dns", "web", "mail", "redis")


def _iter_vuln_entries(entries):
    """Yield (slug, vars) from a configurations list (slug or {name, vars})."""
    for e in entries or []:
        if isinstance(e, str):
            yield e, {}
        elif isinstance(e, dict) and e.get("name"):
            yield e["name"], e.get("vars") or {}


def _looks_like_service(slug):
    return any(w in (slug or "").lower() for w in _SERVICE_WORDS)


def is_windows_local(machine):
    return "win" in ((machine or {}).get("os") or "").lower()


class NakonIntel(Intel):
    source_name = "nakon"

    def _machines(self):
        cfg = self.file("nakon-config.json") or {}
        return {m["ip"]: m for m in cfg.get("machines", [])}

    def _machine_for(self, ip):
        return self._machines().get(ip)

    # -- credentials ---------------------------------------------------------

    def known_creds(self):
        state = self.file(".deploy_state.json") or {}
        box_pw = state.get("box_password")
        creds = []
        for b in self.boxes():
            users = ["Administrator"] if b["windows"] else [self.box_username(), "root"]
            for user in users:
                if box_pw:
                    creds.append({"user": user, "password": box_pw,
                                  "note": f"box_password on {b['ip']}"})
            for user, pw in (state.get("box_creds") or {}).items():
                creds.append({"user": user, "password": pw,
                              "note": f"credlist account on {b['ip']}"})
            m = self._machine_for(b["ip"])
            if m and m.get("user") and m.get("password"):
                creds.append({"user": m["user"], "password": m["password"],
                              "note": f"nakon machine account on {b['ip']}"})
        for box_type, entries in (self.file("box_vulns.json") or {}).items():
            for slug, varz in _iter_vuln_entries(entries):
                user = varz.get("USERNAME") or varz.get("USER") or varz.get("DB_USER")
                pw = (varz.get("PASSWORD") or varz.get("PASS") or varz.get("DB_PASS")
                      or varz.get("USER_PASS"))
                if user and pw is not None:
                    creds.append({"user": user, "password": pw,
                                  "note": f"planted by '{slug}' ({box_type})"})
        return creds

    def candidate_users(self):
        seen, out = set(), []
        for c in self.known_creds():
            if c["user"] not in seen:
                seen.add(c["user"])
                out.append(c["user"])
        return out or [self.box_username()]

    def candidate_passwords(self):
        seen, out = set(), []
        for c in self.known_creds():
            if c["password"] not in seen:
                seen.add(c["password"])
                out.append(c["password"])
        return out

    def candidate_pairs(self, ip=None):
        pairs = []
        state = self.file(".deploy_state.json") or {}
        box_pw = state.get("box_password")
        for b in self.boxes():
            if ip and b["ip"] != ip:
                continue
            primary = "Administrator" if b["windows"] else self.box_username()
            if box_pw:
                pairs.append((primary, box_pw))
            for user, pw in (state.get("box_creds") or {}).items():
                pairs.append((user, pw))
        # A pam_permit stack accepts ANY password for every local account. Strictly
        # per-box — spraying it anywhere else just burns attempts.
        if ip:
            machine = self._machine_for(ip)
            slugs = {slug for slug, _ in _iter_vuln_entries(
                (machine or {}).get("configurations"))}
            if slugs & {"pam-permit-auth", "pam-permit-empty"}:
                users = {u for u, _ in pairs} | {"root", self.box_username()}
                pairs += [(u, PAM_ANY_PASSWORD) for u in sorted(users)]
        return list(dict.fromkeys(pairs))

    # -- per-box facts -------------------------------------------------------

    def hints_for(self, ip):
        m = self._machine_for(ip)
        if not m:
            return {}
        vulns = []
        for slug, _ in _iter_vuln_entries(m.get("configurations")):
            if slug in SELF_HEALING_SLUGS:
                vulns.append(f"SELF-HEALING, {SELF_HEALING_SLUGS[slug]} — removing "
                             "the symptom regrows it; the mechanism is the real target")
                continue
            vulns.append(VULN_HINTS.get(slug, slug.replace("-", " ")))
        return {"vulns": vulns,
                "user": m.get("user"),
                "escalation": escalation_label(m),
                "windows": is_windows_local(m)}

    def _box_pins(self, ip):
        box = next((b for b in self.boxes() if b["ip"] == ip), None)
        if not box:
            return []
        return (self.file("box_services.json") or {}).get(box["box"], [])

    def services_for(self, ip):
        m = self._machine_for(ip)
        configured = []
        if m:
            configured = [e if isinstance(e, str) else e.get("name", "")
                          for e in m.get("configurations", [])
                          if _looks_like_service(e if isinstance(e, str)
                                                 else e.get("name", ""))]
        if configured:
            return configured
        # Stoppable scored services only: score_only pins have no unit to stop —
        # passing their raw names ("score/tcp") to takedown just burns a cycle.
        return [e if isinstance(e, str) else e.get("name", "")
                for e in self._box_pins(ip)
                if not (isinstance(e, dict) and (e.get("score_only") or e.get("plant_only")))]

    def port_checks_for(self, ip):
        """Scored pins that are pure port-open checks -> block_port targets."""
        return [{"display": e.get("display") or e.get("name", ""), "port": e.get("port")}
                for e in self._box_pins(ip)
                if isinstance(e, dict) and e.get("score_only")]


def escalation_label(machine):
    """The escalation label nakon's plants imply for this box, most capable first.

    Labels are the routing vocabulary `tlaloc.policy` and the `escalate` effect
    share: ``sudo`` (passwordless), ``sudo_pw`` (sudo with the known password),
    ``escape:<cmd>`` (a one-shot escape), ``capexec:<path>`` (capability/SUID
    binary), or ``system`` (Windows admin).
    """
    if is_windows_local(machine):
        return "system"
    slugs = {slug for slug, _ in _iter_vuln_entries(machine.get("configurations"))}
    if slugs & {"sudo-nopasswd", "passwordless-sudo", "user-sudo-nopasswd",
                "sudoers-dropin-world-admins", "writable-sudoers",
                "world-writable-cron-d", "world-writable-crond",
                "exploitable-cron-job", "sudoers-gtfobins-allowlist"}:
        return "sudo"
    if slugs & {"suid-find", "suid-vim", "suid-nano", "suid-python", "suid-ed",
                "suid-binary", "file-capability"}:
        return "capexec:pending-discovery"
    return None
