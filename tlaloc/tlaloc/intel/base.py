"""Shared intel plumbing: scope, boxes, accounts, heuristics (leaf of intel)."""

import json
import re
from pathlib import Path

INTEL_FILES = ("teams.json", "boxes.json", "users.json", "box_services.json",
               "box_vulns.json", "nakon-config.json", ".deploy_state.json",
               "credentials.txt")

# keyword (service slug / Quotient ServiceName) -> candidate systemd units, most likely first.
# Blind guesses; nakon narrows via box_services.json.
UNIT_HINTS = {
    "nginx": ["nginx"], "apache": ["apache2"], "http": ["nginx", "apache2"],
    "web": ["nginx", "apache2"], "dovecot": ["dovecot"], "imap": ["dovecot"],
    "postfix": ["postfix"], "smtp": ["postfix"], "mail": ["postfix", "dovecot"],
    "bind": ["named", "bind9"], "dns": ["named", "bind9"],
    "vsftpd": ["vsftpd"], "ftp": ["vsftpd"], "mysql": ["mysql", "mariadb"],
    "mariadb": ["mariadb"], "sql": ["mysql", "mariadb"],
    "postgres": ["postgresql"], "ssh": ["ssh", "sshd"],
    "telnet": ["telnetd", "inetd"], "roundcube": ["apache2", "php8.3-fpm", "nginx"],
    "splunk": ["splunk"], "winrm": ["winrm"],
}


class Intel:
    source_name = "base"

    def __init__(self, directory):
        self.dir = Path(directory)
        self._files = {}
        for name in INTEL_FILES:
            path = self.dir / name
            if path.exists():
                self._files[name] = (json.loads(path.read_text(encoding="utf-8"))
                                     if path.suffix == ".json"
                                     else path.read_text(encoding="utf-8"))

    def file(self, name):
        return self._files.get(name)

    def teams(self):
        """[{team, identifier, subnet}] — the ROE allowlist."""
        out = []
        for team, data in (self.file("teams.json") or {}).items():
            ident = str(data["identifier"])
            out.append({"team": team, "identifier": ident,
                        "subnet": data.get("subnet") or f"192.168.{ident}.0/24"})
        return out

    def subnets(self):
        return [t["subnet"] for t in self.teams()]

    def boxes(self):
        """every target; unmanaged hosts are excluded HERE, no consumer re-implements it."""
        out = []
        for team in self.teams():
            for box in (self.file("boxes.json") or []):
                if box.get("unmanaged"):
                    continue
                octet = box.get("last_octet")
                ip = box.get("ip") or f"192.168.{team['identifier']}.{octet}"
                out.append({"team": team["team"], "identifier": team["identifier"],
                            "box": box["name"], "ip": ip,
                            "windows": "win" in (box.get("template") or "").lower(),
                            "template": box.get("template")})
        return out

    def box_by_ip(self, ip):
        return next((b for b in self.boxes() if b["ip"] == ip), None)

    def team_of(self, ip):
        box = self.box_by_ip(ip)
        return box["team"] if box else None

    def box_username(self):
        users = self.file("users.json")
        if isinstance(users, dict):
            return users.get("box_username", "ubuntu")
        return "ubuntu"

    def credlist_users(self):
        users = self.file("users.json")
        if isinstance(users, dict) and users.get("credlist_usernames"):
            return list(users["credlist_usernames"])
        state = self.file(".deploy_state.json") or {}
        return list((state.get("box_creds") or {}).keys())

    def quotient_admin_password(self):
        return (self.file(".deploy_state.json") or {}).get("admin_password")

    def discover_engine_ip(self):
        """Engine mgmt IP from credentials.txt ('Scoreboard: http://10.0.0.92')."""
        text = self.file("credentials.txt")
        if isinstance(text, str):
            match = re.search(r"https?://(\d+\.\d+\.\d+\.\d+)", text)
            if match:
                return match.group(1)
        return None

    def unit_candidates(self, service_name):
        name = (service_name or "").lower()
        for keyword, units in UNIT_HINTS.items():
            if keyword in name:
                return units
        slug = re.sub(r"[^a-z0-9]", "", name)
        return [slug] if slug else []

    def candidate_users(self):
        raise NotImplementedError

    def candidate_passwords(self):
        raise NotImplementedError

    def candidate_pairs(self, ip=None):
        return [(u, p) for u in self.candidate_users()
                for p in self.candidate_passwords()]

    def known_creds(self):
        """ground truth; NakonIntel fills this, BlindIntel leaves []."""
        return []

    def hints_for(self, ip):
        return {}

    def services_for(self, ip):
        return []

    def port_checks_for(self, ip):
        """score_only pins: firewall-block targets."""
        return []
