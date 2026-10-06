"""Engine flock + in-flight deploy detection (serializes deploys sharing one scoring engine)."""

import fcntl
import contextlib
import os
import re
import time

from constants import SCORING_ENGINE_VMID
from pathlib import Path


_ENGINE_LOCKS = {}  # lock path -> open fh (kept referenced so the flock survives)


def acquire_engine_lock(engine_vmid=SCORING_ENGINE_VMID):
    """Serialize deploys that share one scoring engine. Since M2.3 the staging slot is
    per-run, but the engine lock still guards the wider shared surface: one engine's
    boxes/engine are being actively reconfigured by whoever holds it.

    Keyed on (proxmox endpoint host, engine vmid) — both known before terraform
    apply — so two different competitions on the same host can't race the same
    engine. Because the key includes the per-competition
    engine vmid, two competitions with DIFFERENT engines (multi-tenant node) take
    distinct locks and run concurrently. Complements _deploy_owner_check (cross-host).
    Idempotent per process; the flock releases when the process exits or crashes."""
    endpoint = os.environ.get("TF_VAR_proxmox_endpoint", "unknown")
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0] or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}-{engine_vmid}")
    lock_path = Path.home() / ".tezcatlipoca" / "locks" / f"engine-{slug}.lock"
    if str(lock_path) in _ENGINE_LOCKS:
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"  ERROR: another deploy on this host already holds the scoring-engine lock "
            f"({lock_path.name}) — a different competition is targeting the same engine, and "
            "its nakon push would wipe this one's /opt/nakon staging dir. Wait for it (find it "
            "with pgrep -f 'create-competition|redeploy-competition')."
        )
    _ENGINE_LOCKS[str(lock_path)] = fh


def release_engine_lock():
    """Drop every engine flock this process holds. Needed whenever this process is
    about to spawn create-competition as a child (redeploy --reset-event's reseed):
    the child takes the lock itself, so a parent still holding it self-deadlocks
    the reseed (live-found 2026-10-01, cde-2026 reset-event)."""
    for path, fh in list(_ENGINE_LOCKS.items()):
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        del _ENGINE_LOCKS[path]


def held_lock_paths():
    """The lock files THIS process holds, so a concurrency scan can exclude them."""
    return set(_ENGINE_LOCKS)


def other_deploys_in_flight():
    """Locks held by another LIVE process — i.e. another deploy is running right now.

    Returns [(path, age_seconds), ...] sorted newest first. This is the one concurrency
    signal that cannot lie: a holder is a live process, and the flock is released by the
    kernel when that process dies, so a leftover `.lock` file is never a false positive
    (unlike a timestamp, a VM's existence, or a log's mtime).

    Why it matters (AGENTS.md, docs/environment-facts.md): two sessions driving one
    estate is the documented cause of the 13xx vmid races, the foreign-golden squat, and
    the over-broad sweep that destroyed two other competitions' engines and goldens.
    """
    locks_dir = Path.home() / ".tezcatlipoca" / "locks"
    if not locks_dir.is_dir():
        return []
    ours = held_lock_paths()
    in_flight = []
    for path in sorted(locks_dir.glob("*.lock")):
        if str(path) in ours:
            continue
        try:
            fh = open(path, "a")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Someone else is holding it: a deploy is live.
                try:
                    age = max(0.0, time.time() - path.stat().st_mtime)
                except OSError:
                    age = 0.0
                in_flight.append((str(path), age))
            else:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
    in_flight.sort(key=lambda item: item[1])
    return in_flight
