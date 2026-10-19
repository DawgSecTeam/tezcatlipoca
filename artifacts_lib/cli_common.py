"""Shared pieces of the test-artifacts.py CLI: comp/key resolution, index rows, formatting."""

import re
import sys
from pathlib import Path

from artifacts_lib import artifacts_ops as ao
from common.comp_paths import comps_root
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2
# A stub's own status line answers "why is this document not the real one?", so `show`
# reports it instead of paraphrasing it.
STUB_STATUS_RE = re.compile(r"^status:\s*(\S+)", re.M)


def resolve_comp(name):
# Two shapes are accepted because the same operator habit drives both: the bare id used
    # by destroy-competition.py, and a path to a comp dir anywhere on disk (a practice
    # run's worktree is a different checkout). A Compfile is required either way.
    for candidate in (Path(name), comps_root() / name):
        if (candidate / "Compfile").is_file():
            return candidate
    return None


def locate_test(args):
# Shared preamble of every key-taking subcommand: unknown comp and unknown key are both
    # usage errors.
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return None, None, unknown_comp(args.comp)
    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return None, None, error(message, EXIT_USAGE)
    return comp_dir, path, None


def existing_keys(comp_dir):
    root = ao.artifacts_root(comp_dir)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir()
                  if (path / ao.MANIFEST_NAME).is_file())


def resolve_test(comp_dir, key):
    path = ao.test_dir(comp_dir, key)
    if path.is_dir():
        return path, None
    known = existing_keys(comp_dir)
    return None, (f"no test folder for key {key!r} under {ao.artifacts_root(comp_dir)} "
                  f"(keys present: {', '.join(known) if known else 'none'})")


def _index_is_stale(root):
# The index is derived, but a stale verdict is the one thing an operator must not have to
    # doubt, and the check is a few stat() calls.
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


def index_rows(comp_dir):
# A comp with no `.automated-tests/` returns [] without calling update_index: creating the
    # drop point would make the absence of artifacts invisible.
    root = ao.artifacts_root(comp_dir)
    if not root.is_dir():
        return []
    try:
        if _index_is_stale(root):
            ao.update_index(comp_dir)
        return ao.list_tests(comp_dir)
    except OSError as e:
        raise RuntimeError(f"could not read or rebuild {root / ao.INDEX_NAME}: {e}") from e




def error(message, code=EXIT_USAGE):
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def unknown_comp(name):
    root = comps_root()
    found = sorted(path.name for path in root.iterdir() if path.is_dir()) \
        if root.is_dir() else []
    return error(
        f"unknown competition {name!r} — looked for {root / name} and {Path(name)}, "
        f"both needing a Compfile. Competitions present: {', '.join(found) or 'none'}", EXIT_USAGE)


def show(value):
    return "-" if value in (None, "") else str(value)


def print_table(headers, rows, indent="  "):
    cells = [[show(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print((indent + "  ".join(h.ljust(w) for h, w in zip(headers, widths))).rstrip())
    print(indent + "  ".join("-" * width for width in widths))
    for row in cells:
        print((indent + "  ".join(c.ljust(w) for c, w in zip(row, widths))).rstrip())


def display_label(want):
# Prefers a path in every shape of want; `?` tells the operator nothing and a command string
    # is the least useful name for output that lands in a named file.
    return (want.get("remote") or want.get("local_name") or want.get("pattern")
            or (Path(want["local"]).name if want.get("local") else None)
            or want.get("cmd") or "?")


def wants(target):
    return list(target.get("want") or []) + list(target.get("want_globs") or [])


def agents_line(manifest):
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
    present = [side for side in ("red", "blue") if (row.get("agents") or {}).get(side)]
    return (row.get("key"), row.get("run_id"), row.get("kind"), row.get("created_at"),
            row.get("phase"), row.get("verdict"), "+".join(present) if present else "-",
            row.get("writeup"), row.get("warnings"), "yes" if row.get("report") else "-")


LIST_HEADERS = ("key", "run id", "kind", "created", "phase", "verdict", "agents", "writeup",
                "warn", "report")
