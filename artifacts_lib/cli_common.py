"""Shared pieces of the test-artifacts.py CLI: comp/key resolution, index rows, formatting.

Imports the `artifacts_ops` facade (the stable public API), exactly as the CLI always did."""

import re
import sys
from pathlib import Path

import artifacts_ops as ao

COMPETITIONS_DIR = Path("competitions")
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2
# write_stub's frontmatter line: the stub's own status is the most precise answer to "why is this
# document not the real one?", so `show` reports it instead of paraphrasing it.
STUB_STATUS_RE = re.compile(r"^status:\s*(\S+)", re.M)


# ── resolving comps, keys and the index ────────────────────────────────────────────────


def resolve_comp(name):
    """The comp dir for `name`, or None.

    Two shapes are accepted because the same operator habit drives both: the bare id used by
    destroy-competition.py (`competitions/<id>`), and a path to a comp dir anywhere on disk (a
    practice run's worktree is a different checkout, so a path is sometimes all you have).
    A Compfile is required either way — it is what makes a directory a competition."""
    for candidate in (Path(name), COMPETITIONS_DIR / name):
        if (candidate / "Compfile").is_file():
            return candidate
    return None


def locate_test(args):
    """(comp_dir, test_path, None), or (None, None, exit_code) after printing why not.

    The shared preamble of every key-taking subcommand: unknown comp and unknown key are both
    usage errors with the same message."""
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return None, None, unknown_comp(args.comp)
    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return None, None, error(message, EXIT_USAGE)
    return comp_dir, path, None


def existing_keys(comp_dir):
    """Test folder names on disk — the truth `archive --all` and the miss message both use."""
    root = ao.artifacts_root(comp_dir)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir()
                  if (path / ao.MANIFEST_NAME).is_file())


def resolve_test(comp_dir, key):
    """(test_path, None) or (None, message). Every key-taking subcommand needs the same miss."""
    path = ao.test_dir(comp_dir, key)
    if path.is_dir():
        return path, None
    known = existing_keys(comp_dir)
    return None, (f"no test folder for key {key!r} under {ao.artifacts_root(comp_dir)} "
                  f"(keys present: {', '.join(known) if known else 'none'})")


def _index_is_stale(root):
    """True when a test folder is newer than the index that is supposed to summarize it.

    The index is derived, so being wrong here costs a reprinted table — but a stale verdict
    is the one thing an operator must not have to doubt, and the check is a few stat() calls."""
    try:
        index_mtime = (root / ao.INDEX_NAME).stat().st_mtime
    except OSError:
        return True
    for path in root.iterdir():
        if not (path / ao.MANIFEST_NAME).is_file():
            continue
        for candidate in (path, path / ao.MANIFEST_NAME, path / ao.COLLECTION_NAME,
                          path / ao.REPORT_NAME):
            try:
                if candidate.stat().st_mtime > index_mtime:
                    return True
            except OSError:
                continue
    return False


def index_rows(comp_dir, *, refresh=False):
    """Index rows for `comp_dir`, rebuilding a missing or stale index first.

    A comp with no `.automated-tests/` at all returns [] without calling update_index: `list` is
    a read, and creating the drop point for it would make the absence of artifacts invisible.
    `update_index` re-reads every manifest and stub, so one unreadable file raises there — that is
    reported as a finding instead of a traceback, because "the index is unreadable" is exactly the
    kind of thing this command exists to tell the operator."""
    root = ao.artifacts_root(comp_dir)
    if not root.is_dir():
        return []
    try:
        if refresh or _index_is_stale(root):
            ao.update_index(comp_dir)
        return ao.list_tests(comp_dir)
    except OSError as e:
        raise RuntimeError(f"could not read or rebuild {root / ao.INDEX_NAME}: {e}") from e




def error(message, code=EXIT_USAGE):
    """Usage-level failure: one clear line on stderr, non-zero exit, no traceback."""
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def unknown_comp(name):
    found = sorted(path.name for path in COMPETITIONS_DIR.iterdir() if path.is_dir()) \
        if COMPETITIONS_DIR.is_dir() else []
    return error(
        f"unknown competition {name!r} — looked for {COMPETITIONS_DIR / name} and {Path(name)}, "
        f"both needing a Compfile. Competitions present: {', '.join(found) or 'none'}", EXIT_USAGE)


def show(value):
    return "-" if value in (None, "") else str(value)


def print_table(headers, rows, indent="  "):
    """Fixed-width table. A missing cell prints as `-` so a gap is visibly a gap."""
    cells = [[show(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print((indent + "  ".join(h.ljust(w) for h, w in zip(headers, widths))).rstrip())
    print(indent + "  ".join("-" * width for width in widths))
    for row in cells:
        print((indent + "  ".join(c.ljust(w) for c, w in zip(row, widths))).rstrip())


def display_label(want):
    """A wanted item's human id: remote path, local filename, glob, or capture destination.

    Wants come in three shapes — a remote/local file, a glob, and a read-only command whose
    stdout is captured (`cmd` + `local`). The label prefers a path in every case: `?` tells the
    operator nothing about which item is missing, and a command string is the least useful name
    for something whose result lands in a named file."""
    return (want.get("remote") or want.get("local_name") or want.get("pattern")
            or (Path(want["local"]).name if want.get("local") else None)
            or want.get("cmd") or "?")


def wants(target):
    """Every wanted item of a planned target: files plus globs."""
    return list(target.get("want") or []) + list(target.get("want_globs") or [])


def agents_label(agents):
    present = [side for side in ("red", "blue") if (agents or {}).get(side)]
    return "+".join(present) if present else "-"


def agents_line(manifest):
    """`red present (10.0.0.198, vmid 999); blue absent` — who took part, from the manifest."""
    agents = manifest.get("agents") or {}
    parts = []
    for side in ("red", "blue"):
        info = agents.get(side) or {}
        if not info.get("present"):
            parts.append(f"{side} absent")
            continue
        detail = []
        if info.get("ip"):
            detail.append(str(info["ip"]))
        if info.get("vmid"):
            detail.append(f"vmid {info['vmid']}")
        if side == "blue" and info.get("teams"):
            detail.append(f"{info['teams']} team(s)")
        parts.append(f"{side} present" + (f" ({', '.join(detail)})" if detail else ""))
    return "; ".join(parts)


def summary_line(collection):
    """One line for collection.json's counters — the file record total and every status."""
    summary = collection.get("summary") or {}
    if not summary:
        return "no summary recorded"
    head = (f"{summary['files']} file record(s)" if summary.get("files") is not None
            else "no file count")
    counts = [f"{status} {summary[status]}" for status in ao.STATUSES if summary.get(status)]
    for extra in ("reverified",):
        if summary.get(extra):
            counts.append(f"{extra} {summary[extra]}")
    return head + (" — " + ", ".join(counts) if counts else "")


def list_row(row):
    return (row.get("key"), row.get("run_id"), row.get("kind"), row.get("created_at"),
            row.get("phase"), row.get("verdict"), agents_label(row.get("agents")),
            row.get("writeup"), row.get("warnings"), "yes" if row.get("report") else "-")


LIST_HEADERS = ("key", "run id", "kind", "created", "phase", "verdict", "agents", "writeup",
                "warn", "report")
