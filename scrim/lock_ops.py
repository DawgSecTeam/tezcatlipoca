"""Range-level flock: one scrim per resolved competition workspace."""

import fcntl
import os
import re
from pathlib import Path


_LOCKS = {}  # lock path -> open fh (kept referenced so the flock survives)


def scrim_lock_path(comp):
    """The lock file for a resolved competition workspace."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", str(Path(comp).resolve()))
    return Path.home() / ".tezcatlipoca" / "locks" / f"scrim-{slug}.lock"


def acquire_scrim_lock(comp):
    """Take this range's scrim flock, or refuse naming the holder; idempotent per process.

    Two scrims of one range collide on everything the harness touches: the fire test (each
    stops nginx, so the second reads the first's DOWN), red01 (a second bad-auto deploy
    reconfigures the first's live red), and the Quotient sessions (every qlogin evicts the
    other harness's cookie — seen with two concurrent scrims on one range, one of them
    cancelled). The flock releases on process exit
    or crash, exactly like `nakonops.nakon_lock_ops.acquire_engine_lock`.
    """
    lock_path = scrim_lock_path(comp)
    if str(lock_path) in _LOCKS:
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # a+ never truncates on open, so a refusal can still read the holder's line
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = (fh.read() or "").strip()
        fh.close()
        raise SystemExit(
            f"  ERROR: another scrim of this range is already running — {lock_path.name}"
            + (f" held by {holder}" if holder else "")
            + ". Two scrims of one range collide on the fire test, on red01, and on the "
              "Quotient team sessions. Wait for it (find it with "
              "pgrep -f run-agent-scrim.py), or scrim a different range."
        )
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()} — {Path(comp).resolve()}")
    fh.flush()
    _LOCKS[str(lock_path)] = fh
