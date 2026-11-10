"""One key for a scored service: the engine names it `<box>-<short>`, intel names the package."""

import re

_ALIASES = {"ldap": "adds", "http": "nginx", "https": "nginx",
            "apache": "nginx", "httpd": "nginx",   # cde-2026's web01 intel
            "smtp": "postfix", "imap": "dovecot", "sql": "mysql",
            "mariadb": "mysql", "mysqld": "mysql",
            "ftp": "iisftp", "winrm": "enablewinrm"}


def service_key(name):
    key = re.sub(r"[^a-z0-9]", "", str(name).lower().removesuffix(".service"))
    return _ALIASES.get(key, key)


def scored_key(name):
    return service_key(str(name).rsplit("-", 1)[-1])
