"""Portal logins are Quotient's own logins, read from the event.conf Quotient reads."""

# Reads the conf directly instead of logging in, so a portal login cannot touch a live session.

import hmac
import os
import threading
import tomllib

# Automation, not people (quotient/setup.py _admin_accounts). [[inject]] accounts are
# likewise refused: only [[team]] and [[admin]] sections are ever read.
REFUSED_ADMIN_NAMES = frozenset({"scoring"})

# Compared against when the username is unknown, so a miss costs the same as a wrong password.
_DUMMY = "x" * 32


class EventConfAuth:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._mtime = None
        self._accounts = {}

    def _load(self):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            # A missing file means "nobody can log in", never stale accounts: the engine's
            # clean step removes event.conf; cached creds would outlive Quotient's own.
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
        with self._lock:
            self._load()
            return [n for n, (role, _pw) in self._accounts.items() if role == "team"]

    def check(self, username, password):
        with self._lock:
            self._load()
            role, want = self._accounts.get(str(username), (None, _DUMMY))
        ok = hmac.compare_digest(str(password).encode(), want.encode())
        if role is None or not ok:
            return None
        return role, str(username)
