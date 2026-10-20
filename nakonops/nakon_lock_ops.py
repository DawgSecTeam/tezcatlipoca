"""Engine flock + in-flight deploy detection (serializes deploys sharing one scoring engine)."""

import fcntl
import contextlib
import os
import re

from common.constants import SCORING_ENGINE_VMID
from pathlib import Path
from estate import config


_ENGINE_LOCKS = {}  # lock path -> open fh (kept referenced so the flock survives)


def acquire_engine_lock(engine_vmid=SCORING_ENGINE_VMID):
    # Serialize deploys that share one scoring engine: whoever holds it is reconfiguring that
    # engine's boxes. Keyed on (endpoint host, engine vmid), so two comps with DIFFERENT engines on
    # one node take distinct locks. Idempotent per process; the flock releases on process exit.
    endpoint = (config.active().proxmox.endpoint or "unknown")
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0] or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}-{engine_vmid}")
    lock_path = Path.home() / ".tezcatlipoca" / "locks" / f"engine-{slug}.lock"
    if str(lock_path) in _ENGINE_LOCKS:
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" never truncates a holder's pid: opening with "w" before the flock erased it,
    # so a refusal could not name the process to wait for.
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        with contextlib.suppress(OSError):
            fh.seek(0)
            holder = (fh.read() or "").strip()
        raise SystemExit(
            f"  ERROR: another deploy on this host already holds the scoring-engine lock "
            f"({lock_path.name} held by pid {holder or 'unknown'}) — a different competition "
            "is targeting the same engine, and its nakon push would wipe this one's "
            "/opt/nakon staging dir. Wait for it (find it with pgrep -f "
            "'create-competition|redeploy-competition')."
        )
    with contextlib.suppress(OSError):
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    _ENGINE_LOCKS[str(lock_path)] = fh


def release_engine_lock():
    # Drop every engine flock this process holds: a parent still holding it self-deadlocks the
    # child it is about to spawn (redeploy --reset-event's reseed), which takes the lock itself.
    for path, fh in list(_ENGINE_LOCKS.items()):
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        del _ENGINE_LOCKS[path]
