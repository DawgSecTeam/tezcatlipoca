"""State primitives: timestamps, atomic writes, the schema version.

world.json is written 0600 (it holds credentials by design), atomically
(mkstemp + chmod + replace) and behind an flock. A lesson from the earlier agent:
the director's stale in-memory copy clobbered an operator re-seed's artifact
records — a bare atomic write is not enough with two writers; the flock +
reload-then-mutate-then-save contract is the whole concurrency story, there is
deliberately NO merge.
"""

import calendar
import os
import tempfile
import time

# Bump ONLY with a deliberate schema change; an existing world.json with a
# different version is refused (fresh state dir per comp — the earlier agent's
# --reset-event cross-event leak has no place to happen).
SCHEMA = 2


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def parse_ts(ts):
    """Epoch seconds for a now_iso() timestamp; 0 (ancient) on garbage."""
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except (TypeError, ValueError):
        return 0


def atomic_write(path, text, mode=0o600):
    path = __import__("pathlib").Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
