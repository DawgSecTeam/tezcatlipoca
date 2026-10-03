#!/usr/bin/env python3
"""Per-run test artifacts under a competition directory.

The contract (design + rationale: docs/reports/automated-test-artifacts-plan-2026-10-03.md;
canonical prose lands in docs/automated-test-artifacts.md with step 7 of that plan):

    competitions/<comp>/.automated-tests/
      index.json                 roll-up of every test, newest first
      <key>/                     key = the deploy's run-<8hex>, else untagged-<ts>
        test.json                identity + intent + verdict + writeup status
        collection.json          what was pulled, from where, sha256, per-target status
        RED-TEAM.md              pulled from red01 (or an honest stub saying why not)
        BLUE-TEAM.md             blue's authored report, sealed (or a stub)
        REPORT.md                synthesis: incidents, run success, recommendations
        evidence/{red,engine,blue,harness}/

Why this is a module and not logic inside destroy-competition.py: **two callers must agree
exactly.** The scrim harness collects before `badauto destroy` erases red01; teardown collects
when the harness never got there (crash, kill, session death). A second copy of the pull logic
is how those two drift apart — tests/test_helper_dedup.py exists because that keeps happening.

Why the folder is keyed on the run id: it already exists (utils.mint_run_id), it is persisted in
.deploy_state.json, it is stamped on every VM as an ownership tag, it is reused across
resume/redeploy, and every destruction path already requires it (constants.ownership_tags). Two
worktrees running the same competition ID therefore cannot collide, and the artifact folder
correlates with VM identity for free. Older comp dirs have no run id — hence the stable
`untagged-<deploy timestamp>` fallback in test_key().

Statuses are deliberately a small closed set, because the thing this feature exists to prevent is
an empty folder that could mean anything:

    ok             fetched successfully
    sealed         local file copied, hashed, chmod 0600
    absent         the source was reachable and the file genuinely is not there
    failed         the source was reachable and the transfer failed
    unreachable    the source (VM/agent) could not be reached at all
    unrecoverable  the source is gone for good (VM already destroyed)
    skipped        not applicable to this run (e.g. no red agent in a plain deploy test)

Nothing here blocks a teardown. A missing report is printed loudly and recorded; destroying a
range is never held hostage by a dead guest box (operator decision, 2026-10-03).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from config_ops import write_state, write_text_atomic
from utils import record_degradation

REPO = Path(__file__).resolve().parent

SCHEMA = 1
DIRNAME = ".automated-tests"
INDEX_NAME = "index.json"
MANIFEST_NAME = "test.json"
COLLECTION_NAME = "collection.json"
REPORT_NAME = "REPORT.md"
KINDS = ("scrim", "deploy", "soak", "canary", "loadtest", "rehearsal")

# Side name -> canonical document in the test folder.
SIDES = {"red": "RED-TEAM.md", "blue": "BLUE-TEAM.md"}
EVIDENCE_DIRS = ("red", "engine", "blue", "harness")

OK = "ok"
SEALED = "sealed"
ABSENT = "absent"
FAILED = "failed"
UNREACHABLE = "unreachable"
UNRECOVERABLE = "unrecoverable"
SKIPPED = "skipped"
# A stub-only status, deliberately outside the collection vocabulary: the sources exist but
# nobody ran the collector, so "absent" (a fact about the box) would be a lie about the folder.
NOT_COLLECTED = "not-collected"
STATUSES = (OK, SEALED, ABSENT, FAILED, UNREACHABLE, UNRECOVERABLE, SKIPPED)
# "we wanted this and do not have it" — the only statuses allowed to raise a warning.
LOST = (FAILED, UNREACHABLE, UNRECOVERABLE)

RUN_ID_RE = re.compile(r"^run-[0-9a-f]{8}$")
TODO_MARK = "<!-- TODO(author)"


class Unreachable(Exception):
    """The source exists on paper but cannot be talked to (VM stopped, agent down).

    Distinct from OSError/FileNotFoundError so the collector records `unreachable` instead of
    blaming the file for a dead box."""


# ── paths and identity ──────────────────────────────────────────────────────────────────


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
    """The deploy's run id, or "" when the comp dir predates run ids.

    Every one of the seven on-disk .deploy_state.json files has no `run_id` today, so the empty
    case is the common case, not an edge case."""
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


def _iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S",
                         time.localtime(ts if ts is not None else time.time()))


def git_facts():
    """(rev, dirty) for this tree. Never raises: a report must not fail on git."""
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=10).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                capture_output=True, text=True, timeout=20).stdout
        return rev, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "", None


def in_worktree():
    """True when REPO is a linked worktree rather than the main checkout.

    Practice runs must happen in a throwaway worktree (AGENTS.md), and the competition dir — with
    every artifact in it — is per-worktree. The collector uses this to decide whether the
    finished test folder needs archiving somewhere durable before the worktree is removed."""
    try:
        git_dir = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=REPO,
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=REPO,
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not git_dir or not common:
        return False
    return Path(REPO, git_dir).resolve() != Path(REPO, common).resolve()


def comp_name(comp_dir):
    """The Compfile's own `<id>` (line 1) when present — it can differ from the dir name."""
    try:
        first = (Path(comp_dir) / "Compfile").read_text().splitlines()[0]
    except (OSError, IndexError):
        return Path(comp_dir).name
    parts = first.split(None, 1)
    return parts[1].strip() if len(parts) == 2 and parts[0] == "name" else Path(comp_dir).name


# ── manifest ────────────────────────────────────────────────────────────────────────────


def load_manifest(test_path):
    try:
        data = json.loads((Path(test_path) / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(test_path, manifest):
    manifest["schema"] = SCHEMA
    manifest["updated_at"] = _iso()
    write_state(Path(test_path) / MANIFEST_NAME, manifest)
    return Path(test_path) / MANIFEST_NAME


def ensure_test(comp_dir, *, kind, run_id=None, key=None, script=None, label=None,
                teams=None, boxes=None, endpoint=None, node=None, nodes=None, extra=None):
    """Open (or create) this run's test folder and return `(path, manifest)`.

    Idempotent by design: both callers may run, in either order, any number of times. A caller
    that knows less than whoever created the manifest must not erase what it knows, so existing
    truthy values win. Refuses to reuse a key that belongs to a different run id — that is the
    guard against two worktrees sharing one folder."""
    comp_dir = Path(comp_dir)
    key = key or test_key(comp_dir, run_id)
    run_id = run_id if run_id is not None else run_id_from_state(comp_dir)
    path = test_dir(comp_dir, key)
    (path / "evidence").mkdir(parents=True, exist_ok=True)
    for sub in EVIDENCE_DIRS:
        (path / "evidence" / sub).mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(path)
    if manifest.get("key") and manifest["key"] != key:
        raise RuntimeError(f"test folder {path} holds key {manifest['key']!r}, not {key!r}")
    if manifest.get("run_id") and run_id and manifest["run_id"] != run_id:
        raise RuntimeError(
            f"test folder {path} belongs to {manifest['run_id']}, not {run_id} — refusing to "
            "mix two runs' artifacts in one folder")

    if not manifest:
        rev, dirty = git_facts()
        manifest = {
            "schema": SCHEMA,
            "comp": comp_dir.name,
            "comp_name": comp_name(comp_dir),
            "comp_dir": str(comp_dir),
            "key": key,
            "run_id": run_id or None,
            "kind": kind,
            "label": label or kind,
            "created_at": _iso(),
            "created_by": {"script": script, "git_rev": rev, "worktree": str(REPO),
                           "dirty": dirty},
            "endpoint": endpoint,
            "node": node,
            "nodes": nodes or ([node] if node else []),
            "teams": teams,
            "boxes": list(boxes or []),
            "agents": {"red": {"present": False}, "blue": {"present": False}},
            "paths": {},
            "event": {"t0": None, "duration_min": None},
            "verify": {},
            "verdict": {},
            "writeup": {"status": "needs-writeup", "author": None, "completed_at": None},
            "teardown": {},
            "phases": [],
        }
    else:
        for field, value in (("kind", kind), ("label", label), ("teams", teams),
                             ("endpoint", endpoint), ("node", node)):
            if value is not None and not manifest.get(field):
                manifest[field] = value
        if boxes:
            manifest["boxes"] = list(boxes)
        if nodes:
            manifest["nodes"] = sorted(set(manifest.get("nodes") or []) | set(nodes))
    for field, value in (extra or {}).items():
        if value is not None:
            manifest[field] = value
    save_manifest(path, manifest)
    return path, manifest


def record_phase(test_path, phase, *, t0=None, **fields):
    """Append a phase marker to the manifest (mirrors the harness's run.json `phase`).

    This is the record that answers the question every crashed practice run raises: how far did
    it get before it died?"""
    manifest = load_manifest(test_path)
    manifest.setdefault("phases", []).append({"phase": phase, "at": _iso(), "t0": t0})
    manifest["phase"] = phase
    for field, value in fields.items():
        manifest[field] = value
    save_manifest(test_path, manifest)
    return manifest


def update_manifest(test_path, **fields):
    """Merge fields into the manifest without touching `phases` (the harness's hot path)."""
    manifest = load_manifest(test_path)
    for field, value in fields.items():
        if value is not None:
            manifest[field] = value
    save_manifest(test_path, manifest)
    return manifest


def _absolute(value):
    """Resolve a path against the writer's CWD, at write time.

    The manifest outlives the process that wrote it and is read from a different CWD (teardown
    runs from the repo root, the harness from its worktree root), so a relative path in here is
    a bug waiting for a reader in the wrong directory. `record_paths` is the moment the writer
    still knows what it meant."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(Path(item).resolve()) for item in value]
    return str(Path(value).resolve())


def record_paths(test_path, **paths):
    """Merge source paths into the manifest — the collector's map of where artifacts live.

    Merged rather than replaced (the harness knows the run dir and blue workdirs, teardown learns
    the engine capture location, and neither should erase the other's knowledge), and stored
    absolute (see `_absolute`)."""
    manifest = load_manifest(test_path)
    known = manifest.setdefault("paths", {})
    for field, value in paths.items():
        if value is not None:
            known[field] = _absolute(value)
    save_manifest(test_path, manifest)
    return known


def _resolve_recorded(value, comp_dir):
    """Best-effort absolute form of a path read back from a manifest.

    Older manifests (and hand-written ones) can hold a relative path; try it against the CWD and
    against the competition directory rather than silently reporting the source as gone."""
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    for base in (Path.cwd(), Path(comp_dir or ".")):
        candidate = base / path
        if candidate.exists():
            return candidate
    return path


# ── hashing and sealing ─────────────────────────────────────────────────────────────────


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_facts(path):
    path = Path(path)
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def seal_local_file(src, dest):
    """Copy `src` to `dest` with 0600 at creation and return its facts.

    0600-at-creation rather than chmod-after: these files quote credentials and flags, and the
    repo's own atomic writer documents why that window matters (config_ops.write_text_atomic)."""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o600)
    facts = file_facts(dest)
    facts["source"] = str(src)
    return facts


def write_bytes_atomic(path, data, mode=0o600):
    """Bytes variant of config_ops.write_text_atomic: mode applied at creation, never torn."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    return path


# ── target planning ─────────────────────────────────────────────────────────────────────


def plan_targets(manifest, *, comp_dir=None):
    """What this run's artifacts are, and where each one comes from.

    Pure: no I/O, no transport. The result is the collector's input, and the CLI can print it to
    answer "why is RED-TEAM.md missing?" without touching a VM.

    Severity is derived from the manifest rather than from a per-file `required` flag: if the
    manifest says a red agent was present, a missing red report is worth shouting about; if it
    does not, the same absence is simply the truth about a run that had no red agent."""
    agents = manifest.get("agents") or {}
    paths = manifest.get("paths") or {}
    targets = []

    red = agents.get("red") or {}
    if red.get("present"):
        targets.append({
            "name": "red01", "route": "scp-jump", "kind": "remote",
            "node": red.get("node"), "vmid": red.get("vmid"), "ssh": red.get("ssh") or {},
            "note": ("bad-auto's VM, not a tezcatlipoca box — `badauto destroy` removes it, so a "
                     "missing red report after a normal harness run is expected unless the "
                     "harness or teardown collected before that"),
            "want": [
                {"remote": "/var/lib/bad-auto/report-*.md", "to": "evidence/red/",
                 "canonical": SIDES["red"]},
                {"remote": "/var/lib/bad-auto/events.jsonl",
                 "local": "evidence/red/events.jsonl"},
                {"remote": "/var/lib/bad-auto/world.json", "local": "evidence/red/world.json"},
                {"remote": "/var/lib/bad-auto/report-secrets.md",
                 "local": "evidence/red/report-secrets.md"},
                {"cmd": "sudo -n journalctl -u bad-auto --no-pager",
                 "local": "evidence/red/bad-auto-journal.log"},
            ],
        })
    else:
        targets.append({"name": "red01", "route": "scp-jump", "kind": "remote", "want": [],
                        "status": SKIPPED, "note": "no red agent in this run"})

    run_dir = _resolve_recorded(paths.get("run_dir"), comp_dir)
    harness_want = [{"local_name": name} for name in
                    ("run.json", "T0.txt", "monitor.log", "watchdog.log", "scoreboard-state.jsonl",
                     "INTERACTION.md", "deploy.log", "evidence/alerts.jsonl",
                     "evidence/final-scoreboard.json", ".deploy-timings.jsonl")]
    if run_dir and Path(run_dir).is_dir():
        targets.append({"name": "harness", "route": "local", "kind": "local",
                        "root": str(run_dir), "to": "evidence/harness/",
                        "note": "the scrim harness run directory", "want": harness_want})
    else:
        targets.append({"name": "harness", "route": "local", "kind": "local", "root": run_dir,
                        "status": SKIPPED if not run_dir else UNRECOVERABLE, "want": [],
                        "note": ("no run dir recorded in the manifest" if not run_dir
                                 else f"recorded run dir {run_dir} no longer exists")})

    blue = agents.get("blue") or {}
    workdirs = [d for d in (_resolve_recorded(w, comp_dir)
                            for w in (paths.get("blue_workdirs") or []))
                if d and d.is_dir()]
    if blue.get("present") and workdirs:
        for workdir in workdirs:
            name = Path(workdir).name
            targets.append({
                "name": f"blue:{name}", "route": "local", "kind": "local",
                "root": str(workdir), "to": f"evidence/blue/{name}/",
                "note": "blue agent workdir (operator host, not a guest box)",
                "want": [
                    {"local_name": "REPORT.md", "canonical": SIDES["blue"]},
                    {"local_name": "FINDINGS.md"},
                    {"local_name": "LOG.md"}, {"local_name": "NOTEBOOK.md"},
                    {"local_name": "feed.log"},
                ],
                "want_globs": [{"pattern": "sub*.md"}, {"pattern": "sub*.txt"},
                               {"pattern": "cycles/*"}, {"pattern": "submissions/*"}],
            })
    else:
        targets.append({"name": "blue", "route": "local", "kind": "local", "want": [],
                        "status": SKIPPED,
                        "note": ("no blue agent in this run" if not blue.get("present")
                                 else "blue agent present but no workdir was recorded")})

    if comp_dir:
        targets.append({"name": "comp", "route": "local", "kind": "local",
                        "root": str(comp_dir), "to": "evidence/harness/",
                        "note": "per-competition deploy sidecar",
                        "want": [{"local_name": ".deploy-timings.jsonl"}]})

    engine = _resolve_recorded(paths.get("engine_evidence"), comp_dir)
    if engine and Path(engine).is_dir():
        targets.append({"name": "engine", "route": "local", "kind": "local", "root": str(engine),
                        "to": "evidence/engine/",
                        "note": "engine-side capture (scoreboard, injects, services)",
                        "want": [], "want_globs": [{"pattern": "*"}]})
    else:
        targets.append({"name": "engine", "route": "local", "kind": "local", "root": engine,
                        "want": [], "status": SKIPPED,
                        "note": ("no engine capture exists (the harness captures it before the "
                                 "scoring DB is destroyed)")})
    return targets


# ── transports ──────────────────────────────────────────────────────────────────────────


def default_transport():
    """route name -> fetch callable. Injected so the collector is testable with no estate."""
    return {"scp-jump": _scp_files, "guest-agent": _guest_files, "local": _local_files,
            "ssh-cmd": _ssh_capture}


def _scp_files(ssh, remote, dest_dir, timeout=120):
    """scp `remote` into dest_dir. Globs are expanded by the remote shell, so a
    `/var/lib/bad-auto/report-*.md` pattern needs no directory listing round-trip.

    Direct first, then through the engine jump: the control host reaches red01 either way
    depending on the estate, and the harness has used this two-attempt shape since the first
    scrim (run-agent-scrim.py:1906-1914). A failed attempt's partial file is always removed —
    a truncated events.jsonl that looks complete is worse than a loud failure."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    key, user, host = ssh.get("key"), ssh.get("user") or "sysadmin", ssh.get("host")
    if not host:
        raise Unreachable("no address recorded for this target")
    common = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
              "-o", "ConnectTimeout=15"]
    if key:
        common += ["-i", str(key)]
    jump = ssh.get("jump")
    before = {p.name for p in dest_dir.iterdir() if p.is_file()}
    last_error = ""
    for extra in ([], (["-o", jump] if jump else [])):
        cmd = ["scp"] + common + extra + [f"{user}@{host}:{remote}", f"{dest_dir}/"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_error = f"scp timed out after {timeout}s"
            continue
        fetched = [p for p in dest_dir.iterdir() if p.is_file() and p.name not in before]
        lowered = (proc.stderr or proc.stdout or "").lower()
        if proc.returncode == 0 and fetched:
            for path in fetched:
                os.chmod(path, 0o600)
            return sorted(fetched)
        for path in fetched:
            path.unlink(missing_ok=True)
        last_error = (proc.stderr or proc.stdout or "").strip()
        if "no such file" in lowered or "not found" in lowered:
            raise FileNotFoundError(f"{remote} on {host}")
    raise Unreachable(f"scp from {user}@{host} failed: {last_error}")


def _guest_files(source, remote, dest_dir, timeout=60):
    """Read one file from a team box over the guest-agent channel (Windows branch included).

    The agent channel is the only route that needs no network path (virtio-serial), which is why
    it is preferred for team boxes; `range_ops.guest_file_read` raises FileNotFoundError vs
    RuntimeError precisely so this collector can record `absent` vs `failed`."""
    if any(ch in remote for ch in "*?["):
        raise RuntimeError(
            f"the guest-agent route cannot expand {remote!r} — list the files with a shell exec "
            "first, or use the scp route for this target")
    import range_ops  # lazy: keeps this module importable without the Proxmox client stack

    dest = Path(dest_dir) / Path(remote).name
    data = range_ops.guest_file_read(source["node"], int(source["vmid"]), remote,
                                     timeout=timeout, windows=bool(source.get("windows")))
    return [write_bytes_atomic(dest, data)]


def _local_files(source, pattern, dest_dir):
    """Copy local files into the test folder. `pattern` is relative to the target root."""
    root, dest_dir = Path(source["root"]), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    matches = sorted(root.glob(pattern))
    if not matches:
        raise FileNotFoundError(str(root / pattern))
    written = []
    for src in matches:
        dest = dest_dir / src.name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
            for path in dest.rglob("*"):
                if path.is_file():
                    os.chmod(path, 0o600)
                    written.append(path)
            continue
        seal_local_file(src, dest)
        written.append(dest)
    return written


def _relative(test_path, path):
    return str(Path(path).resolve().relative_to(Path(test_path).resolve()))


def _want_label(want):
    """A stable id for a wanted item — the key the second teardown run matches on."""
    return (want.get("remote") or want.get("local_name") or want.get("cmd")
            or want.get("pattern"))


# Public alias: consumers that print a plan (test-artifacts.py) must not grow a second
# implementation that silently prints `?` when a new want shape is added.
want_label = _want_label


def _dest_dir(test_path, target, want):
    """Where this wanted item's bytes go, as a directory."""
    if want.get("local"):
        return (Path(test_path) / want["local"]).parent
    return Path(test_path) / (want.get("to") or target.get("to")
                              or f"evidence/{target['name'].split(':')[0]}/")


def _ssh_capture(ssh, cmd, dest_file, timeout=90):
    """Run a read-only command over ssh and save its stdout (red's journal, mainly).

    Same direct-then-engine-jump shape as the file pull. There is no `scp` equivalent for a
    service journal, and the journal is the only record of *why* the red LLM stopped mid-event,
    so it is worth one command channel."""
    dest_file = Path(dest_file)
    key, user, host = ssh.get("key"), ssh.get("user") or "sysadmin", ssh.get("host")
    if not host:
        raise Unreachable("no address recorded for this target")
    common = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
              "-o", "ConnectTimeout=15"]
    if key:
        common += ["-i", str(key)]
    jump = ssh.get("jump")
    last_error = ""
    for extra in ([], (["-o", jump] if jump else [])):
        try:
            proc = subprocess.run(["ssh"] + common + extra + [f"{user}@{host}", cmd],
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_error = f"ssh timed out after {timeout}s"
            continue
        if proc.returncode == 0:
            if not (proc.stdout or "").strip():
                raise FileNotFoundError(f"no output from {cmd!r}")
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(dest_file, proc.stdout, mode=0o600)
            return [dest_file]
        last_error = (proc.stderr or proc.stdout or "").strip()
    raise Unreachable(f"ssh to {user}@{host} failed: {last_error}")


def _fetch(target, want, test_path, timeout, transport):
    """Fetch one wanted item. Returns the list of local paths written."""
    if want.get("cmd"):
        fetcher = transport.get("ssh-cmd")
        if fetcher is None:
            raise RuntimeError("no transport for the ssh command channel")
        dest = Path(test_path) / (want.get("local") or f"evidence/{target['name']}/cmd.log")
        return fetcher(target.get("ssh") or {}, want["cmd"], dest, timeout=timeout)
    fetcher = transport.get(target.get("route"))
    if fetcher is None:
        raise RuntimeError(f"no transport for route {target.get('route')!r}")
    dest_dir = _dest_dir(test_path, target, want)
    if target.get("route") == "local":
        source = {"name": target["name"], "root": target["root"]}
        return fetcher(source, want.get("local_name") or want["pattern"], dest_dir)
    if target.get("route") == "scp-jump":
        return fetcher(target.get("ssh") or {}, want.get("remote") or want.get("pattern"),
                       dest_dir, timeout=timeout)
    if target.get("route") == "guest-agent":
        return fetcher(target, want.get("remote") or want.get("pattern"), dest_dir,
                       timeout=timeout)
    raise RuntimeError(f"unknown route {target.get('route')!r}")


# ── collection ──────────────────────────────────────────────────────────────────────────


def _previous_items(collection, target_name, label):
    """Items from an earlier collection run for this same wanted item."""
    for target in collection.get("targets", []):
        if target.get("name") == target_name:
            return [item for item in target.get("files", []) if item.get("want") == label]
    return []


def _still_good(test_path, items):
    """True when every previously collected item still exists and still hashes the same.

    This is what makes a re-run teardown idempotent *and* honest: an already-good artifact is
    re-verified rather than re-fetched, and one somebody edited by hand is re-collected instead
    of being silently trusted."""
    if not items:
        return False
    for item in items:
        if item.get("status") not in (OK, SEALED):
            return False
        path = Path(test_path) / item.get("local", "")
        if not path.exists() or not item.get("sha256"):
            return False
        if sha256_file(path) != item["sha256"]:
            return False
    return True


def _canonical_done(test_path, collection, canonical):
    for derived in collection.get("derived", []):
        if derived.get("path") != canonical:
            continue
        path = Path(test_path) / canonical
        if path.exists() and derived.get("sha256") == sha256_file(path):
            return True
    return False


def collect(test_path, targets, *, transport=None, skip_existing=True, dry_run=False,
            timeout=120, stop_on_unreachable=True):
    """Fetch/seal everything in `targets` into the test folder, then write collection.json.

    `transport` is injected (defaults to the real scp/guest-agent/local trio) so the whole
    collector is exercised offline by tests/test_artifacts_ops.py.

    `stop_on_unreachable` gives up on a target after its first unreachable transfer instead of
    burning the per-file timeout four more times: teardown runs this, and a dead red01 must not
    add ten minutes to a destroy that is already the slowest step.

    Never raises for a single unreachable or missing source: a partial collection recorded as
    partial is the entire point of the status vocabulary. Only programming errors (an unknown
    route) are hard failures."""
    test_path = Path(test_path)
    transport = transport or default_transport()
    unknown = sorted({t.get("route") for t in targets if t.get("route") and not t.get("status")}
                     - set(transport))
    if unknown:
        # A route with no transport is a wiring bug, not a missing artifact: recording it as
        # `failed` per file would let a silent typo look like a run that simply had no evidence.
        raise RuntimeError(f"no transport for route(s) {unknown} — refusing to record a "
                           "collection that cannot even be attempted")
    previous = load_collection(test_path)
    manifest = load_manifest(test_path)
    collected = {"schema": SCHEMA, "key": test_path.name, "run_id": manifest.get("run_id"),
                 "collected_at": _iso(), "targets": [], "derived": [], "summary": {}}
    counters = {status: 0 for status in STATUSES}

    for target in targets:
        entry = {"name": target.get("name"), "route": target.get("route"),
                 "node": target.get("node"), "vmid": target.get("vmid"),
                 "note": target.get("note"), "files": [], "errors": [], "status": OK,
                 # Which canonical documents this target was meant to produce. Recorded even
                 # when the target fails wholesale, because that is the case where "why is
                 # RED-TEAM.md missing?" has to be answerable from collection.json alone.
                 "canonical_documents": [w["canonical"] for w in (target.get("want") or [])
                                         if w.get("canonical")]}
        if target.get("status"):
            entry["status"] = target["status"]
            entry["reason"] = target.get("note")
            counters[target["status"]] += 1
            collected["targets"].append(entry)
            continue
        if target.get("route") == "scp-jump" and not (target.get("ssh") or {}).get("host"):
            entry["status"] = UNREACHABLE
            entry["errors"].append("no address recorded for this target")
            counters[UNREACHABLE] += 1
            collected["targets"].append(entry)
            continue
        if target.get("route") == "guest-agent" and not (target.get("node")
                                                         and target.get("vmid")):
            entry["status"] = UNREACHABLE
            entry["errors"].append("no node/vmid recorded for this target")
            counters[UNREACHABLE] += 1
            collected["targets"].append(entry)
            continue

        for want in list(target.get("want", [])) + list(target.get("want_globs", [])):
            label = _want_label(want)
            records = []
            already = (_previous_items(previous, entry["name"], label)
                       if skip_existing and not dry_run else [])
            if dry_run:
                records.append({"want": label, "status": SKIPPED, "reason": "dry run"})
            elif _still_good(test_path, already):
                # Carry the earlier records forward verbatim (they hold the sha256 provenance)
                # and mark them re-verified. Replacing them with a "skipped" stub would throw
                # away the hash record the second teardown run is supposed to confirm.
                records.extend({**item, "reverified_at": _iso()} for item in already)
                counters["reverified"] = counters.get("reverified", 0) + 1
            else:
                try:
                    written = _fetch(target, want, test_path, timeout, transport)
                    for path in written:
                        records.append({"want": label, "local": _relative(test_path, path),
                                        "status": OK, **file_facts(path)})
                    if not written:
                        records.append({"want": label, "status": ABSENT,
                                        "reason": "source reachable, nothing matched"})
                except FileNotFoundError as e:
                    records.append({"want": label, "canonical": want.get("canonical"),
                                    "status": ABSENT, "reason": str(e)})
                except (Unreachable, TimeoutError) as e:
                    records.append({"want": label, "canonical": want.get("canonical"),
                                    "status": UNREACHABLE, "reason": str(e)})
                except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                    records.append({"want": label, "canonical": want.get("canonical"),
                                    "status": FAILED, "reason": f"{type(e).__name__}: {e}"})
            if want.get("canonical") and not dry_run:
                records, derived = _derive_canonical(test_path, previous, want, records)
                if derived:
                    collected["derived"].append(derived)
            entry["files"].extend(records)
            for record in records:
                counters[record["status"]] = counters.get(record["status"], 0) + 1
            for record in records:
                if record["status"] in LOST:
                    entry["errors"].append(f"{label}: {record.get('reason')}")
            if stop_on_unreachable and any(r["status"] == UNREACHABLE for r in records):
                remaining = [w for w in (list(target.get("want", []))
                                         + list(target.get("want_globs", [])))
                             if _want_label(w) != label]
                entry["files"].extend({"want": _want_label(w), "canonical": w.get("canonical"),
                                       "status": SKIPPED,
                                       "reason": "not attempted: target unreachable"}
                                      for w in remaining)
                counters[SKIPPED] += len(remaining)
                break

        # Target status comes from what actually happened to its files, never from matching
        # strings in the error text: a target is only as good as its worst record, and an
        # all-absent target says so plainly.
        statuses = {record["status"] for record in entry["files"]}
        if UNREACHABLE in statuses:
            entry["status"] = UNREACHABLE
        elif FAILED in statuses:
            entry["status"] = FAILED
        elif statuses and statuses <= {ABSENT}:
            entry["status"] = ABSENT
        else:
            entry["status"] = OK
        collected["targets"].append(entry)

    collected["summary"] = {**counters,
                            "files": sum(len(t["files"]) for t in collected["targets"])}
    write_state(test_path / COLLECTION_NAME, collected)
    return collected


def _derive_canonical(test_path, previous, want, records):
    """Copy the newest fetched file to the canonical document name (e.g. RED-TEAM.md)."""
    canonical = want["canonical"]
    if _canonical_done(test_path, previous, canonical):
        for derived in previous.get("derived", []):
            if derived.get("path") == canonical:
                return records, {**derived, "reverified_at": _iso()}
        return records, None
    candidates = [Path(test_path) / r["local"] for r in records
                  if r.get("status") == OK and r.get("local")]
    if not candidates:
        return records, None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    facts = seal_local_file(newest, Path(test_path) / canonical)
    return records, {"path": canonical, "from": _relative(test_path, newest),
                     "method": "pulled", "sha256": facts["sha256"]}


def load_collection(test_path):
    try:
        data = json.loads((Path(test_path) / COLLECTION_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ingest_verdict(test_path, *, source="evidence/harness/INTERACTION.md"):
    """Fold scrim-report.py's verdict into test.json's machine verdict.

    Parsed rather than recomputed on purpose: scrim-report.py owns the interaction score and the
    docs/rehearsal-gates.md gate table, and a second implementation here would drift from it.
    Tolerant of a missing/partial file (returns {} and leaves the manifest alone) because
    INTERACTION.md is produced by a best-effort step."""
    test_path = Path(test_path)
    path = test_path / source
    if not path.exists():
        return {}
    text = path.read_text()
    verdict_section = re.search(r"^## Verdict\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    section = verdict_section.group(1) if verdict_section else text
    status = re.search(r"\*\*(GREEN|NOT READY|FAILED RUN|NO INTERACTION)", section)
    if not status:
        return {}
    score = re.search(r"interaction score (\d+)", section)
    gates = {"pass": 0, "fail": 0, "n/a": 0}
    gates_section = re.search(r"^## Gates.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if gates_section:
        for line in gates_section.group(1).splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("side", "---"):
                continue
            cell = cells[-1].lower()
            if cell in ("pass", "fail", "n/a"):
                gates[cell] += 1
    verdict = {"status": status.group(1), "score": int(score.group(1)) if score else None,
               "gates_passed": gates["pass"], "gates_failed": gates["fail"],
               "gates_na": gates["n/a"], "source": source, "at": _iso()}
    update_manifest(test_path, verdict=verdict)
    return verdict


def _document_state(test_path, filename, collection):
    """What state a canonical document is actually in: `present`, or the stub's own status.

    The distinction matters because a stub is a file that exists and says "not available" —
    warning only on a missing *file* would either miss every real gap (the stub exists) or
    ignore the document on disk entirely."""
    test_path = Path(test_path)
    path = test_path / filename
    for derived in collection.get("derived", []):
        if derived.get("path") == filename and path.exists():
            return "present"
    if not path.exists():
        return ABSENT
    match = re.search(r"^status:\s*(\S+)", path.read_text(errors="ignore"), re.M)
    return (match.group(1) if match else "present")


def _test_path_for(manifest, test_path=None):
    """The test folder for a manifest, when the caller did not already have it in hand."""
    if test_path:
        return Path(test_path)
    comp_dir, key = manifest.get("comp_dir"), manifest.get("key")
    return Path(comp_dir) / DIRNAME / key if comp_dir and key else None


def warn_summary(collection, manifest, test_path=None):
    """The lines teardown prints loudly when something that should be there is not.

    Severity comes from the manifest, not the collection: a run with no red agent is not missing
    anything when there is no red report. Nothing here blocks — the operator chose warn-and-
    proceed (2026-10-03) because a dead guest box must never wedge a teardown."""
    warnings = []
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("status") in LOST:
                warnings.append(f"{target['name']}: {item.get('want')} — {item['status']}"
                                + (f" ({item['reason']})" if item.get("reason") else ""))
        if target.get("status") in LOST and not target.get("errors"):
            warnings.append(f"{target['name']}: {target['status']}"
                            + (f" ({target.get('reason')})" if target.get("reason") else ""))
    agents = manifest.get("agents") or {}
    folder = _test_path_for(manifest, test_path)
    for side, filename in SIDES.items():
        if not (agents.get(side) or {}).get("present"):
            continue
        state = _document_state(folder, filename, collection) if folder else ABSENT
        if state != "present":
            warnings.append(f"{filename} is {state.upper()} although this run had a {side} "
                            "agent — that side's narrative is not in this folder")
    return warnings


# ── the three canonical documents ───────────────────────────────────────────────────────


def write_stub(test_path, side, status, reason, *, manifest=None):
    """Write the honest placeholder for a canonical document we could not obtain.

    An absent file is indistinguishable from a wiring bug; a stub naming the status and the
    reason is not. That is the whole reason the three documents have fixed filenames."""
    manifest = manifest or load_manifest(test_path)
    filename = SIDES[side]
    run = manifest.get("run_id") or manifest.get("key") or "unknown-run"
    body = ["---", f"id: {run}", f"side: {side}", "generated_by: artifacts_ops.py",
            f"status: {status}", f"generated: {_iso()}", "---", "",
            f"# {filename} — not available ({status})", ""]
    if status == SKIPPED:
        body += [f"This run had no {side} agent, so no {side} report exists. That is the truth "
                 "about the run, not a collection failure.", ""]
    elif status == NOT_COLLECTED:
        body += [f"The {side} report was never collected — the evidence it would come from is "
                 "still where the run left it, so this is recoverable. Collect it with:",
                 "",
                 f"```\npython3 test-artifacts.py collect "
                 f"{manifest.get('comp')} {manifest.get('key')}\n```", ""]
    elif status == UNRECOVERABLE:
        body += [f"The {side} source was already destroyed when artifacts were collected, so "
                 "nothing could be read from it. Evidence for that side is unrecoverable.", ""]
    else:
        body += [f"Collection could not obtain a {side} report.", ""]
    body += [f"- reason: {reason}", f"- competition: {manifest.get('comp')}",
             f"- key: {manifest.get('key')}", "",
             "Whatever evidence was collected is under `evidence/`; `collection.json` records "
             "exactly what was and was not obtained."]
    path = Path(test_path) / filename
    write_text_atomic(path, "\n".join(body) + "\n", mode=0o600)
    return path


def _verdict_block(test_path):
    """The machine verdict, lifted verbatim from INTERACTION.md's `## Verdict` section."""
    interaction = Path(test_path) / "evidence" / "harness" / "INTERACTION.md"
    if not interaction.exists():
        return None
    match = re.search(r"^## Verdict\s*$(.*?)(?=^## |\Z)", interaction.read_text(), re.M | re.S)
    block = (match.group(1).strip() if match else "")
    return block or None


def _timing_rows(test_path):
    """(per-phase totals, slowest ops) from the timing sidecar, when it was captured."""
    path = Path(test_path) / "evidence" / "harness" / ".deploy-timings.jsonl"
    if not path.exists():
        path = Path(test_path) / "evidence" / "harness" / "deploy-timings.jsonl"
    if not path.exists():
        return [], []
    totals, slowest = {}, []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        phase = row.get("phase") or "?"
        seconds = float(row.get("seconds") or 0.0)
        totals[phase] = totals.get(phase, 0.0) + seconds
        slowest.append((seconds, phase, row.get("op"), row.get("target")))
    slowest.sort(reverse=True)
    return sorted(totals.items(), key=lambda kv: -kv[1]), slowest[:5]


def _side_status(collection, side):
    filename = SIDES[side]
    for derived in collection.get("derived", []):
        if derived.get("path") == filename:
            return "present"
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("canonical") == filename and item.get("status") in LOST:
                return item["status"]
    return "absent"


def render_report_skeleton(test_path):
    """The synthesis document, with every machine section filled and judgement marked.

    Deliberately does NOT invent a success metric: the interaction score and the
    docs/rehearsal-gates.md table are the definition (scrim-report.py evaluates them), and a
    second opinion computed here would drift from them."""
    test_path = Path(test_path)
    manifest = load_manifest(test_path)
    collection = load_collection(test_path)
    run = manifest.get("run_id") or manifest.get("key")
    verdict = manifest.get("verdict") or {}
    lines = ["---", f"id: {run}", f"kind: {manifest.get('kind')}",
             f"comp: {manifest.get('comp')}", f"generated: {_iso()}",
             "generated_by: artifacts_ops.py",
             "author: null   # fill the judgement sections, then: test-artifacts.py seal", "---",
             "", f"# {manifest.get('kind', 'run')} report — {manifest.get('comp')} — {run}", "",
             "## Verdict", ""]
    if verdict.get("status"):
        detail = []
        if verdict.get("score") is not None:
            detail.append(f"interaction score {verdict['score']}")
        if verdict.get("gates_passed") is not None:
            detail.append(f"gates {verdict['gates_passed']} pass / "
                          f"{verdict.get('gates_failed')} fail / {verdict.get('gates_na')} n/a")
        lines.append(f"**{verdict['status']}**"
                     + (" — " + "; ".join(detail) if detail else "")
                     + f" (source: {verdict.get('source', 'evidence/harness/INTERACTION.md')})")
    else:
        block = _verdict_block(test_path)
        if block:
            lines.append(block)
        else:
            lines += ["_No machine verdict was captured for this run "
                      "(no INTERACTION.md in its evidence)._",
                      "", "<!-- machine: do not invent a verdict; say what was and was not "
                      "measured. -->"]
    lines += ["", "## Environment", "", "| field | value |", "|---|---|"]
    created_by = manifest.get("created_by") or {}
    for label, value in (
            ("competition", manifest.get("comp_name") or manifest.get("comp")),
            ("run id", manifest.get("run_id") or "(untagged — pre-run-id state)"),
            ("kind", manifest.get("kind")),
            ("node(s)", ", ".join(manifest.get("nodes") or []) or "?"),
            ("endpoint", manifest.get("endpoint")),
            ("teams", manifest.get("teams")),
            ("boxes", ", ".join(manifest.get("boxes") or []) or "?"),
            ("worktree", created_by.get("worktree")),
            ("git rev", created_by.get("git_rev")),
            ("created", manifest.get("created_at"))):
        lines.append(f"| {label} | {value if value is not None else '?'} |")
    agents = manifest.get("agents") or {}
    red, blue = agents.get("red") or {}, agents.get("blue") or {}
    lines += ["", "Agents: red "
              + (f"present ({red.get('ip') or '?'}" + (f", vmid {red['vmid']}"
                 if red.get("vmid") else "") + ")" if red.get("present") else "absent")
              + "; blue " + (f"present ({blue.get('teams') or '?'} team(s))"
                             if blue.get("present") else "absent") + "."]

    lines += ["", "## Timeline and cost", ""]
    phases = manifest.get("phases") or []
    if phases:
        lines += ["| phase | at |", "|---|---|"]
        lines += [f"| {p.get('phase')} | {p.get('at')} |" for p in phases]
    else:
        lines.append("_No phase markers recorded._")
    phase_rows, slowest = _timing_rows(test_path)
    if phase_rows:
        lines += ["", "Where the wall clock went (timing sidecar):", "",
                  "| phase | seconds |", "|---|---|"]
        lines += [f"| {phase} | {seconds:.0f} |" for phase, seconds in phase_rows]
        lines += ["", "Slowest operations:", ""]
        lines += [f"- {seconds:.0f}s — {phase} / {op} ({target or '-'})"
                  for seconds, phase, op, target in slowest]

    lines += ["", "## Incidents", "",
              "<!-- machine seeds these from alerts, degradations, verify failures and collection "
              "failures; an author must classify each and add what only a human saw. -->", "",
              "| when | what | impact | resolved | evidence |", "|---|---|---|---|---|"]
    alerts = Path(test_path) / "evidence" / "harness" / "evidence" / "alerts.jsonl"
    if not alerts.exists():
        alerts = Path(test_path) / "evidence" / "harness" / "alerts.jsonl"
    if alerts.exists():
        counts = {}
        for line in alerts.read_text().splitlines():
            try:
                kind = json.loads(line).get("kind") or "?"
            except ValueError:
                continue
            counts[kind] = counts.get(kind, 0) + 1
        for kind, count in sorted(counts.items()):
            lines.append(f"| (run) | alert `{kind}` x{count} | ? | ? | "
                         "`evidence/harness/alerts.jsonl` |")
    for entry in manifest.get("degradations") or []:
        lines.append(f"| (run) | degraded: {entry.get('what')} — {entry.get('detail')} | ? | ? | "
                     "`test.json` degradations |")
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("status") in LOST:
                lines.append(f"| (collection) | {target['name']}: {item.get('want')} "
                             f"{item['status']} | artifact missing | no | `collection.json` |")
    verify = manifest.get("verify") or {}
    if verify.get("exit_code") not in (None, 0):
        lines.append(f"| (verify) | verify-competition.py exit {verify['exit_code']} | gate "
                     f"failure | ? | `{verify.get('log', 'evidence/harness/')}` |")
    lines += ["", "<!-- TODO(author): classify the seeded rows, delete the ones that are not "
              "incidents, add anything only a human saw -->", ""]

    lines += ["## Red", ""]
    if red.get("present"):
        lines.append(f"Report: `{SIDES['red']}` ({_side_status(collection, 'red')}). Red metrics "
                     "live in the Verdict section; this section is the narrative.")
        lines.append("")
        lines.append("<!-- TODO(author): what red achieved, what it could not, and why -->")
    else:
        lines.append("No red agent took part in this run.")
    lines += ["", "## Blue", ""]
    if blue.get("present"):
        lines.append(f"Report: `{SIDES['blue']}` ({_side_status(collection, 'blue')}). Sealed "
                     "evidence (logs, cycles, submissions) is under `evidence/blue/`.")
        lines.append("")
        lines.append("<!-- TODO(author): what blue did, time-to-restore, inject outcomes, and "
                     "whether the defence was real or nominal -->")
    else:
        lines.append("No blue agent took part in this run.")

    lines += ["", f"## Recommendations — this competition "
              f"({manifest.get('comp_name') or manifest.get('comp')})", "",
              "<!-- TODO(author): what should change about THIS competition — scenario, pins, "
              "lineup, difficulty, packet, scoring -->", "",
              "## Recommendations — tezcatlipoca", "",
              "<!-- TODO(author): tool defects and gaps this run exposed. The fixed heading is "
              "the harvest path into docs/known-issues.md and a fixes plan. One bullet each: "
              "symptom -> evidence path -> suggested owner/file. -->", "",
              "## Not verified / caveats", ""]
    limitations = []
    for target in collection.get("targets", []):
        if target.get("status") == SKIPPED:
            limitations.append(f"{target['name']}: skipped ({target.get('reason') or 'n/a'})")
        elif target.get("status") in LOST:
            limitations.append(f"{target['name']}: {target['status']}")
    if verdict.get("gates_na"):
        limitations.append(f"{verdict['gates_na']} gate(s) n/a (legacy run data)")
    lines += [f"- {item}" for item in limitations] or ["- (none recorded)"]
    lines.append("")
    return "\n".join(lines)


def write_report_skeleton(test_path, *, force=False):
    """Write REPORT.md, never clobbering an authored one.

    A second teardown run must not destroy the write-up somebody did after the first: that would
    teach people to avoid re-running teardown, which is the one thing they must be willing to do
    (destroy-competition.py's own failure path says "re-run this command")."""
    test_path = Path(test_path)
    path = test_path / REPORT_NAME
    manifest = load_manifest(test_path)
    if path.exists() and not force:
        if (manifest.get("writeup") or {}).get("status") != "needs-writeup":
            return path
        if TODO_MARK not in path.read_text():
            return path  # an author has started (or finished) filling it in
    write_text_atomic(path, render_report_skeleton(test_path), mode=0o600)
    return path


def finalize(test_path, *, manifest=None):
    """Stubs for any missing canonical document, then the report skeleton, then the index.

    This is the step that turns a folder of bytes into a standard test artifact."""
    test_path = Path(test_path)
    manifest = manifest or load_manifest(test_path)
    collection = load_collection(test_path)
    agents = manifest.get("agents") or {}
    for side in ("red", "blue"):
        filename = SIDES[side]
        if (test_path / filename).exists():
            continue
        if any(d.get("path") == filename for d in collection.get("derived", [])):
            continue
        if not (agents.get(side) or {}).get("present"):
            write_stub(test_path, side, SKIPPED, f"no {side} agent in this run", manifest=manifest)
            continue
        status, reason = _why_missing(collection, side)
        write_stub(test_path, side, status, reason, manifest=manifest)
    report = write_report_skeleton(test_path)
    update_index(Path(manifest.get("comp_dir") or test_path.parent.parent))
    return {"report": str(report),
            "warnings": warn_summary(collection, manifest, test_path)}


def _why_missing(collection, side):
    """(status, reason) for a canonical document we wanted and did not get.

    Three genuinely different situations, and conflating them is how an artifact folder starts
    lying: nothing was attempted (recoverable — the sources are still out there), an attempt was
    made and the source was empty, unreachable, or already destroyed, or the run never had that
    side at all. A target that failed wholesale records no per-file status, so its own status is
    the honest answer for the document it was meant to carry."""
    filename = SIDES[side]
    attempted = False
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("canonical") == filename:
                attempted = True
                if item.get("status") in LOST + (ABSENT,):
                    return item["status"], item.get("reason") or "not obtained"
        if (target.get("status") in LOST
                and filename in (target.get("canonical_documents") or [])):
            attempted = True
            reason = (target.get("reason") or "; ".join(target.get("errors") or [])
                      or f"{target.get('name')} is {target['status']}")
            return target["status"], reason
    if not attempted:
        return (NOT_COLLECTED,
                "the evidence this report would come from was never collected")
    return ABSENT, "no source produced it"


def _guess_run_dir(comp_dir, test_path):
    """Where the harness's run dir is, for a harness that died before recording it.

    Two candidates, in order: the test folder itself (the harness writes its run dir inside the
    comp dir once that change lands), then the legacy default `<repo-parent>/scrim-runs/<comp>`
    (run-agent-scrim.py's old `--run-dir` default), which is what a pre-change run left."""
    test_path = Path(test_path)
    if (test_path / "run.json").exists() or (test_path / "T0.txt").exists():
        return test_path
    legacy = REPO.parent / "scrim-runs" / Path(comp_dir).name
    return legacy if legacy.is_dir() else None


def collect_for_teardown(comp_dir, *, run_id=None, teams=None, boxes=None, node=None,
                         nodes=None, script="destroy-competition.py", transport=None,
                         dry_run=False, timeout=45, echo=print):
    """Teardown's whole artifact step: the one call destroy-competition.py makes.

    Ordering is the entire point. destroy-competition.py calls this after the confirmation
    prompt and *before* its first destructive call (:487-491): `pre_stop_windows_boxes` hard-
    stops every clone, and a stopped guest's agent can no longer answer, so anything not read by
    then is gone. Red01 is worse — `badauto destroy` removes it before teardown even starts, so
    this is the safety net for the run whose harness died, not the primary path.

    Warns and proceeds; never blocks (operator decision 2026-10-03): a dead box must not wedge a
    teardown. The failure is recorded in collection.json, in the stubs, and in REPORT.md, which
    is where a reader will actually see it.

    Returns the collection dict."""
    comp_dir = Path(comp_dir)
    prior = load_manifest(test_dir(comp_dir, test_key(comp_dir, run_id)))
    path, manifest = ensure_test(
        comp_dir, kind=prior.get("kind") or "deploy", run_id=run_id, script=script,
        teams=len(teams) if hasattr(teams, "__len__") else teams,
        boxes=[b.get("name") for b in boxes] if boxes else None,
        node=node, nodes=nodes)
    if not (manifest.get("paths") or {}).get("run_dir"):
        guessed = _guess_run_dir(comp_dir, path)
        if guessed:
            record_paths(path, run_dir=str(guessed))
    update_manifest(path, teardown={"at": _iso(), "by": script, "dry_run": bool(dry_run),
                                   "collector_timeout_s": timeout})
    manifest = load_manifest(path)
    collection = collect(path, plan_targets(manifest, comp_dir=comp_dir), transport=transport,
                         dry_run=dry_run, timeout=timeout)
    # The verdict and gate table come from scrim-report.py, which reads only the run dir (no
    # network, no API). The harness normally runs it; teardown runs it for the run whose harness
    # died — otherwise a collected INTERACTION.md nobody parsed leaves REPORT.md verdict-less,
    # which is exactly the run that most needs a write-up.
    verdict = _ensure_verdict(path, echo=echo, dry_run=dry_run)
    result = finalize(path, manifest=load_manifest(path))
    summary = collection.get("summary", {})
    echo(f"  Artifacts  : {path}")
    echo("  Collected  : " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())
                                       if k != "files") + f" (files={summary.get('files', 0)})")
    if verdict:
        echo(f"  Verdict    : {verdict['status']}"
             + (f" — interaction score {verdict['score']}" if verdict.get("score") is not None
                else "") + f" ({verdict.get('gates_passed')} pass / "
             f"{verdict.get('gates_failed')} fail / {verdict.get('gates_na')} n/a)")
    for warning in result["warnings"]:
        echo(f"  WARNING: {warning}")
    if not result["warnings"]:
        echo("  No declared document is missing (any `absent` item is recorded in "
             "collection.json).")
    if in_worktree() and not dry_run:
        try:
            echo(f"  Archived   : {archive_test(comp_dir, path.name)}")
        except (OSError, RuntimeError) as e:
            echo(f"  WARNING: could not archive this test folder outside the worktree: {e}")
    return collection


def _ensure_verdict(test_path, *, echo=print, dry_run=False):
    """Run scrim-report.py when it has not run, then fold its verdict into the manifest.

    Best-effort throughout: a missing verdict downgrades REPORT.md to "no machine verdict was
    captured", which is honest, while a raise here would cost the operator a teardown."""
    test_path = Path(test_path)
    interaction = test_path / "evidence" / "harness" / "INTERACTION.md"
    run_dir = _resolve_recorded((load_manifest(test_path).get("paths") or {}).get("run_dir"),
                                test_path.parent.parent)
    if not interaction.exists() and run_dir and Path(run_dir).is_dir() and not dry_run:
        try:
            proc = subprocess.run(["python3", "scrim-report.py", str(run_dir)], cwd=REPO,
                                  capture_output=True, text=True, timeout=300)
            produced = Path(run_dir) / "INTERACTION.md"
            if proc.returncode == 0 and produced.exists():
                seal_local_file(produced, interaction)
            else:
                echo(f"  WARNING: scrim-report.py exited {proc.returncode} — no machine verdict "
                     f"({(proc.stderr or '').strip()[:120]})")
        except (OSError, subprocess.SubprocessError) as e:
            echo(f"  WARNING: could not run scrim-report.py ({type(e).__name__}: {e}) — no "
                 "machine verdict")
    return ingest_verdict(test_path)





# ── roll-up index ───────────────────────────────────────────────────────────────────────


def update_index(comp_dir):
    """Rewrite `<comp>/.automated-tests/index.json` from the test folders on disk.

    Derived, never authoritative: it can always be rebuilt, so a stale index is a cosmetic
    problem rather than a data-loss one."""
    root = artifacts_root(comp_dir)
    rows = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not (path / MANIFEST_NAME).exists():
                continue
            manifest = load_manifest(path)
            collection = load_collection(path)
            rows.append({
                "key": manifest.get("key") or path.name,
                "run_id": manifest.get("run_id"),
                "kind": manifest.get("kind"),
                "label": manifest.get("label"),
                "created_at": manifest.get("created_at"),
                "phase": manifest.get("phase"),
                "verdict": (manifest.get("verdict") or {}).get("status"),
                "agents": {side: bool((manifest.get("agents") or {}).get(side, {}).get("present"))
                           for side in ("red", "blue")},
                "writeup": (manifest.get("writeup") or {}).get("status"),
                "report": (path / REPORT_NAME).exists(),
                "warnings": len(warn_summary(collection, manifest, path)) if collection else 0,
            })
    rows.sort(key=lambda row: (row.get("created_at") or "", row["key"]), reverse=True)
    index = {"schema": SCHEMA, "comp": Path(comp_dir).name, "updated_at": _iso(), "tests": rows}
    root.mkdir(parents=True, exist_ok=True)
    write_state(root / INDEX_NAME, index)
    return root / INDEX_NAME


def list_tests(comp_dir):
    """Index rows, rebuilding the index when it is missing or unreadable."""
    index = artifacts_root(comp_dir) / INDEX_NAME
    if not index.exists():
        update_index(comp_dir)
    try:
        return json.loads(index.read_text()).get("tests", [])
    except (OSError, ValueError):
        update_index(comp_dir)
        return json.loads(index.read_text()).get("tests", [])


# ── verification, sealing, archiving ────────────────────────────────────────────────────


def verify_test(test_path):
    """Re-hash everything the collection claims, and report drift.

    Reports are evidence, so they get the same treatment as any other artifact: a hash that is
    checked, not a filename that is trusted."""
    test_path = Path(test_path)
    manifest = load_manifest(test_path)
    collection = load_collection(test_path)
    problems, checked = [], 0
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("status") not in (OK, SEALED):
                continue
            path = test_path / item.get("local", "")
            if not path.exists():
                problems.append(f"missing: {item.get('local')}")
                continue
            checked += 1
            if item.get("sha256") and sha256_file(path) != item["sha256"]:
                problems.append(f"hash drift: {item.get('local')}")
    for derived in collection.get("derived", []):
        path = test_path / derived["path"]
        if not path.exists():
            problems.append(f"missing canonical document: {derived['path']}")
            continue
        checked += 1
        if derived.get("sha256") and sha256_file(path) != derived["sha256"]:
            problems.append(f"canonical document changed after collection: {derived['path']}")
    for name in (MANIFEST_NAME, COLLECTION_NAME, REPORT_NAME):
        if not (test_path / name).exists():
            problems.append(f"missing {name}")
    todos = 0
    report = test_path / REPORT_NAME
    if report.exists():
        todos = report.read_text().count(TODO_MARK)
    return {"ok": not problems and todos == 0, "checked": checked, "problems": problems,
            "todos": todos, "writeup": (manifest.get("writeup") or {}).get("status")}


def seal_test(test_path, *, author=None, force=False):
    """Flip the write-up to done: refuse while judgement markers remain, then re-hash.

    The seal is what makes `writeup.status` mean something. Without it, "done" is a claim nobody
    checked — and the recommendations section is the part of this feature most likely to be
    skipped."""
    test_path = Path(test_path)
    manifest = load_manifest(test_path)
    report = test_path / REPORT_NAME
    if not report.exists():
        raise RuntimeError(f"no {REPORT_NAME} in {test_path} — nothing to seal")
    unfilled = report.read_text().count(TODO_MARK)
    if unfilled and not force:
        raise RuntimeError(
            f"{REPORT_NAME} still has {unfilled} unfilled section(s) marked TODO(author) — "
            "fill them, or seal with force if a section genuinely does not apply")
    manifest.setdefault("writeup", {})
    manifest["writeup"].update({"status": "done",
                                "author": author or os.environ.get("USER"),
                                "completed_at": _iso()})
    save_manifest(test_path, manifest)
    collection = load_collection(test_path)
    for derived in collection.get("derived", []):
        path = test_path / derived["path"]
        if path.exists():
            derived["sha256_after_writeup"] = sha256_file(path)
    if collection:
        write_state(test_path / COLLECTION_NAME, collection)
    comp_dir = Path(manifest.get("comp_dir") or test_path.parent.parent)
    update_index(comp_dir)
    # A finished write-up in a throwaway worktree is exactly what the teardown-time archive
    # holds only as a skeleton — refresh it now, or the archive keeps a blank REPORT.md.
    archived = None
    if in_worktree():
        try:
            archived = str(archive_test(comp_dir, manifest.get("key") or test_path.name))
        except (OSError, RuntimeError):
            archived = None
    return {"sealed": str(test_path), "author": manifest["writeup"]["author"],
            "archived": archived, "verify": verify_test(test_path)}


def default_archive_root():
    """Durable home for test folders whose worktree is about to be deleted.

    Same convention as the deploy locks (~/.tezcatlipoca/): outside the repo, so discarding a
    practice-run worktree cannot take a month of test history with it."""
    return Path(os.environ.get("TEZ_ARTIFACTS_ARCHIVE")
                or Path.home() / ".tezcatlipoca" / "automated-tests")


def archive_test(comp_dir, key, *, dest_root=None):
    """Copy a finished test folder outside the repo and verify every byte landed.

    Only needed when the comp dir lives in a linked worktree — but there the alternative is
    losing the artifact with the worktree, so the copy is verified by hash, not assumed."""
    comp_dir = Path(comp_dir)
    source = test_dir(comp_dir, key)
    if not source.is_dir():
        raise RuntimeError(f"no such test folder: {source}")
    dest = Path(dest_root or default_archive_root()) / comp_dir.name / key
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    drift = [str(path.relative_to(source)) for path in source.rglob("*") if path.is_file()
             and (not (dest / path.relative_to(source)).exists()
                  or sha256_file(dest / path.relative_to(source)) != sha256_file(path))]
    if drift:
        raise RuntimeError(f"archive verification failed for {len(drift)} file(s): {drift[:5]}")
    record_degradation("artifacts-archived", f"{source} -> {dest}")
    return dest
