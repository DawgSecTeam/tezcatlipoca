"""Where a test folder lives and what it is called."""

import json
import time
from pathlib import Path

from estate import state as estate_state
from .constants import DIRNAME, MANIFEST_NAME, RUN_ID_RE


def artifacts_root(comp_dir):
    # Gitignored deliberately: harvested reports quote flags and credentials. Never
    # publish one into git — record recommendations in docs/known-issues.md instead.
    return Path(comp_dir) / DIRNAME


def test_dir(comp_dir, key):
    return artifacts_root(comp_dir) / key


def run_id_from_state(comp_dir):
    state = estate_state.read_flat(comp_dir) or {}
    run_id = state.get("run_id") or ""
    return run_id if RUN_ID_RE.match(run_id or "") else ""


def next_run_key(comp_dir, run_id=None):
    # Each harness run gets its own folder: one run's evidence never sits next to another's.
    base = test_key(comp_dir, run_id)
    if not (test_dir(comp_dir, base) / MANIFEST_NAME).exists():
        return base
    n = 2
    while (test_dir(comp_dir, f"{base}-run{n}") / MANIFEST_NAME).exists():
        n += 1
    return f"{base}-run{n}"


def run_folders(comp_dir):
    root = Path(comp_dir) / ".automated-tests"
    found = []
    for d in sorted(root.glob("*")):
        if d.is_dir() and (d / MANIFEST_NAME).exists():
            try:
                found.append((d.stat().st_mtime, d))
            except OSError:
                continue
    return [d for _, d in sorted(found, key=lambda t: t[0], reverse=True)]


def latest_run_key(comp_dir, run_id=None):
    # Teardown knows the run id, not which folder the harness minted, so resolve
    # from the folders; fall back to the base key.
    comp_dir = Path(comp_dir)
    want = run_id if run_id is not None else run_id_from_state(comp_dir)
    for d in run_folders(comp_dir):
        try:
            manifest = json.loads((d / MANIFEST_NAME).read_text())
        except (OSError, ValueError):
            continue
        if not want or manifest.get("run_id") == want:
            return d.name, manifest.get("run_id") or want
    return test_key(comp_dir, want), want


def test_key(comp_dir, run_id=None, *, now=None):
    # Teardown re-runs until clean, so the key must be stable per deploy: the untagged
    # fallback comes from state mtime, which destroy never writes and deploy/resume does.
    comp_dir = Path(comp_dir)
    run_id = run_id or run_id_from_state(comp_dir)
    if run_id:
        return run_id
    stamp = None
    for candidate in (estate_state.state_path(comp_dir), comp_dir / ".deploy-timings.jsonl"):
        try:
            stamp = candidate.stat().st_mtime
            break
        except OSError:
            continue
    if stamp is None:
        # No deploy ever wrote state: comp_dir mtime, still stable across teardown re-runs.
        try:
            stamp = comp_dir.stat().st_mtime
        except OSError:
            stamp = time.time()
    if now is not None:
        stamp = float(now)
    return "untagged-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(stamp))


def comp_name(comp_dir):
# The Compfile's own `<id>` (line 1) when present — it can differ from the dir name.
    try:
        first = (Path(comp_dir) / "Compfile").read_text().splitlines()[0]
    except (OSError, IndexError):
        return Path(comp_dir).name
    parts = first.split(None, 1)
    return parts[1].strip() if len(parts) == 2 and parts[0] == "name" else Path(comp_dir).name
