"""Where a test folder lives and what it is called."""

import json
import time
from pathlib import Path

from .constants import DIRNAME, RUN_ID_RE


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
