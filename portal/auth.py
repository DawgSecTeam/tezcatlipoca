"""Portal logins are Quotient's own logins, read from the event.conf Quotient reads.

The portal never authenticates by logging into Quotient: Quotient allows ONE session per
account (quotient/setup.py _admin_accounts), so a portal login that did would evict the team's
scoreboard session. Instead it reads the same /opt/quotient/config/event.conf (mounted
read-only) and compares against its [[team]] and [[admin]] entries. The file is re-read when its
mtime changes, so a password an operator rotates in Quotient's config is picked up without a
portal restart.

Roles: `team` (one per [[team]] entry) and `admin` (white team). The automation admin
(`scoring`) and the [[inject]] account are refused — neither belongs to a person at a keyboard.
"""

import hmac
import os
import threading
import tomllib

# Admin-list entries that are automation, not people (quotient/setup.py _admin_accounts).
REFUSED_ADMIN_NAMES = frozenset({"scoring"})

# Compared against when the username is unknown, so a miss costs the same as a wrong password.
_DUMMY = "x" * 32


class EventConfAuth:
    """Credential checks against a Quotient event.conf, reloaded on mtime change."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._mtime = None
        self._accounts = {}

    def _load(self):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            # A missing file means "nobody can log in", never "keep the stale accounts":
            # the engine's clean step removes event.conf, and a cached copy outliving it
            # would keep honouring credentials Quotient itself no longer knows.
            self._mtime, self._accounts = None, {}
            return
        if mtime == self._mtime:
            return
        with open(self.path, "rb") as f:
            conf = tomllib.load(f)
        accounts = {}
        for entry in conf.get("team") or []:
            if entry.get("name") and entry.get("pw") is not None:
                accounts[str(entry["name"])] = ("team", str(entry["pw"]))
        for entry in conf.get("admin") or []:
            name = str(entry.get("name") or "")
            if name and name not in REFUSED_ADMIN_NAMES and entry.get("pw") is not None:
                accounts[name] = ("admin", str(entry["pw"]))
        self._mtime, self._accounts = mtime, accounts

    def teams(self):
        """Team names Quotient knows, in event.conf order."""
        with self._lock:
            self._load()
            return [n for n, (role, _pw) in self._accounts.items() if role == "team"]

    def check(self, username, password):
        """(role, name) for valid credentials, else None. Constant-time per attempt."""
        with self._lock:
            self._load()
            role, want = self._accounts.get(str(username), (None, _DUMMY))
        ok = hmac.compare_digest(str(password).encode(), want.encode())
        if role is None or not ok:
            return None
        return role, str(username)
