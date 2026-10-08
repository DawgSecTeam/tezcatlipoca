"""Where a test folder lives and what it is called."""

import json
import time
from pathlib import Path

from .constants import DIRNAME, MANIFEST_NAME, RUN_ID_RE


def artifacts_root(comp_dir):
    """`<comp_dir>/.automated-tests` — the drop point. Gitignored, deliberately: box-pulled
    reports quote flags and credentials, and this repo has already leaked secrets twice by
    tracking a file nobody re-checked (.gitignore's own header says so). There is no publish
    path on purpose (operator decision, 2026-10-03): harvest a recommendation by writing it into
    docs/known-issues.md or a fixes plan, never by copying the artifact into git."""
    return Path(comp_dir) / DIRNAME


def test_dir(comp_dir, key):
    return artifacts_root(comp_dir) / key


def run_id_from_state(comp_dir):
    """The deploy's run id, or "" when the comp dir has no (valid) deploy state yet."""
    try:
        state = json.loads((Path(comp_dir) / ".deploy_state.json").read_text())
    except (OSError, ValueError):
        return ""
    run_id = state.get("run_id") or ""
    return run_id if RUN_ID_RE.match(run_id or "") else ""


def next_run_key(comp_dir, run_id=None):
    """The key a FRESH run should use.

    The base key (the deploy's run id) when it has not been used yet, else
    `<base>-run2`, `-run3`… — so each harness run gets its own folder and one
    run's evidence can never sit next to another's."""
    base = test_key(comp_dir, run_id)
    if not (test_dir(comp_dir, base) / MANIFEST_NAME).exists():
        return base
    n = 2
    while (test_dir(comp_dir, f"{base}-run{n}") / MANIFEST_NAME).exists():
        n += 1
    return f"{base}-run{n}"


def run_folders(comp_dir):
    """Existing test folders for this competition, newest first.

    Each harness run mints its own folder (see latest_run_key): one run per folder
    means one run's evidence per folder, so a stale REPORT.md can never sit next to
    a fresh run's artifacts the way it did when every run reused the deploy-keyed
    folder (2026-10-08: october-7 reports were still in the folder a new run wrote into)."""
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
    """(key, run_id) of the newest folder that belongs to `run_id`.

    Teardown's target: it knows the deploy's run id, not which per-run folder the
    harness minted, so it asks the folders themselves. Falls back to the base key."""
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
    """The test folder name: the run id when there is one, else a *stable* untagged key.

    Stability matters because teardown is expected to be re-run until clean
    (destroy-competition.py says so): minting a fresh timestamp per invocation would split one
    run's evidence across several folders. The untagged fallback is therefore derived from
    `.deploy_state.json`'s mtime — which destroy never writes and deploy/resume does — so it is
    fixed for the life of a deploy and changes when a new one writes state."""
    comp_dir = Path(comp_dir)
    run_id = run_id or run_id_from_state(comp_dir)
    if run_id:
        return run_id
    stamp = None
    for candidate in (comp_dir / ".deploy_state.json", comp_dir / ".deploy-timings.jsonl"):
        try:
            stamp = candidate.stat().st_mtime
            break
        except OSError:
            continue
    if stamp is None:
        # No deploy ever wrote state here. comp_dir mtime is the last resort; still stable
        # across repeated teardown invocations, which is what this key has to guarantee.
        try:
            stamp = comp_dir.stat().st_mtime
        except OSError:
            stamp = time.time()
    if now is not None:
        stamp = float(now)
    return "untagged-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(stamp))


def comp_name(comp_dir):
    """The Compfile's own `<id>` (line 1) when present — it can differ from the dir name."""
    try:
        first = (Path(comp_dir) / "Compfile").read_text().splitlines()[0]
    except (OSError, IndexError):
        return Path(comp_dir).name
    parts = first.split(None, 1)
    return parts[1].strip() if len(parts) == 2 and parts[0] == "name" else Path(comp_dir).name
