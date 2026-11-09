"""Slot reservations: make "this vmid is mine" survive the race between preflight and clone.

Preflight reads Proxmox; between that read and the first clone another deploy can claim a
just-freed vmid (2026-10-26: a destroyed range's vmid 2000 was taken in the gap and the next
deploy was refused on a slot it had itself released). The ledger is a flock-guarded JSON file,
so two deploys cannot both see the same slot free. Entries expire, and they are keyed by comp
dir so a resume re-claims its own slots instead of colliding with itself.

Host-local by design: the only processes competing for slots are the deploys this host runs.
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

TTL = 6 * 3600


def _path():
    explicit = os.environ.get("TEZ_SLOT_LEDGER")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "tezcatlipoca-engine" / "slot-reservations.json"


@contextmanager
def _transaction():
    """Exclusive access to the live ledger; writes it back unless fn() raised."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            entries = json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            entries = {}
        now = time.time()
        entries = {r: e for r, e in entries.items() if e.get("expires", 0) > now}
        yield entries, now
        tmp = path.with_name(f"{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(entries, indent=1, sort_keys=True))
        os.replace(tmp, path)


def claim(owner, resources, run_id="", ttl=TTL):
    """Reserve `resources` for `owner`; returns [(resource, holder)] conflicts, empty when claimed."""
    with _transaction() as (entries, now):
        conflicts = [(r, entries[r]) for r in map(str, resources)
                     if r in entries and entries[r].get("owner") != str(owner)]
        if conflicts:
            return conflicts
        for resource in map(str, resources):
            entries[resource] = {"owner": str(owner), "run_id": str(run_id), "expires": now + ttl}
    return []


def release(owner):
    """Drop every reservation `owner` holds."""
    with _transaction() as (entries, _now):
        for resource in [r for r, e in entries.items() if e.get("owner") == str(owner)]:
            del entries[resource]
