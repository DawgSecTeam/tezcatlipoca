"""Deploy timing instrumentation: per-op JSONL sidecar."""

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

TIMINGS_NAME = ".deploy-timings.jsonl"

_append_lock = threading.Lock()


def timings_path(comp_dir):
    return Path(comp_dir) / TIMINGS_NAME


def read_timings(path):
# `ts` is the end stamp and `seconds` the duration, so every row's span is derivable.
    rows = []
    try:
        fh = open(path)
    except OSError:
        return rows
    with fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            try:
                seconds = float(record.get("seconds") or 0.0)
            except (TypeError, ValueError):
                seconds = 0.0
            try:
                end = time.mktime(time.strptime(record["ts"], "%Y-%m-%dT%H:%M:%S"))
            except (KeyError, TypeError, ValueError):
                end = None
            rows.append({"phase": record.get("phase", "?"), "op": record.get("op", "?"),
                         "target": record.get("target", ""), "seconds": seconds,
                         "start": None if end is None else end - seconds})
    return rows


def top_level(rows):
# Parent and child are peers in the file, so summing every row double-counts wall clock.
    return [row for i, row in enumerate(rows)
            if row["start"] is None or not any(
                other["start"] is not None
                and other["start"] <= row["start"] + 1e-6
                and other["start"] + other["seconds"] >= row["start"] + row["seconds"] - 1e-6
                and (other["seconds"] > row["seconds"]
                     or (other["seconds"] == row["seconds"] and j < i))
                for j, other in enumerate(rows))]


@contextmanager
def timed(comp_dir, phase, op, target=""):
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
    totals = {}
    for record in top_level(read_timings(timings_path(comp_dir))):
        key = (record["phase"], record["op"])
        totals[key] = totals.get(key, 0.0) + record["seconds"]
    if not totals:
        return
    print(f"\n  Timing summary ({TIMINGS_NAME}, seconds per phase/op):")
    for (phase, op), seconds in sorted(totals.items(), key=lambda kv: -kv[1])[:top]:
        print(f"    [{phase}] {op:<28} {seconds:9.1f}s")
