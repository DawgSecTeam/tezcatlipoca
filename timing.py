"""Deploy timing instrumentation (M0.1): per-op JSONL sidecar for the baseline.

Every wrapped operation appends one line to competitions/<id>/.deploy-timings.jsonl
(gitignored). The sidecar is append-only and per-competition, so resumes keep
their history and the file never holds secrets — only op names, targets, and
durations. Appends from concurrent workers (M2) are serialized by a lock.
"""

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

TIMINGS_NAME = ".deploy-timings.jsonl"

_append_lock = threading.Lock()


def timings_path(comp_dir):
    return Path(comp_dir) / TIMINGS_NAME


@contextmanager
def timed(comp_dir, phase, op, target=""):
    """Time a block and append {ts, phase, op, target, seconds} on exit. Never raises."""
    start = time.monotonic()
    try:
        yield
    finally:
        seconds = round(time.monotonic() - start, 3)
        record = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": str(phase),
            "op": str(op),
            "target": str(target),
            "seconds": seconds,
        })
        try:
            with _append_lock:
                with open(timings_path(comp_dir), "a") as f:
                    f.write(record + "\n")
        except OSError:
            pass


def print_timing_summary(comp_dir, top=12):
    """Print total seconds per (phase, op), largest first. Never raises."""
    totals = {}
    try:
        with open(timings_path(comp_dir)) as f:
            for line in f:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                key = (record.get("phase", "?"), record.get("op", "?"))
                totals[key] = totals.get(key, 0.0) + float(record.get("seconds", 0))
    except OSError:
        return
    if not totals:
        return
    print(f"\n  Timing summary ({TIMINGS_NAME}, seconds per phase/op):")
    for (phase, op), seconds in sorted(totals.items(), key=lambda kv: -kv[1])[:top]:
        print(f"    [{phase}] {op:<28} {seconds:9.1f}s")
