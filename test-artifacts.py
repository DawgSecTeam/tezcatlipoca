#!/usr/bin/env python3
"""test-artifacts.py — read, inspect and repair per-run test artifact folders.

`artifacts_ops.py` owns the contract: a run leaves `competitions/<comp>/.automated-tests/<key>/`
holding `test.json` (identity, phases, verdict, write-up status), `collection.json` (what was
obtained, from where, with sha256), the three canonical documents (`RED-TEAM.md`,
`BLUE-TEAM.md`, `REPORT.md`) and `evidence/`. This script is the operator's surface over that
folder — which runs have artifacts, why `RED-TEAM.md` is missing, whether a write-up is really
finished — and it is deliberately a thin wrapper: it prints and repairs what the module recorded,
and never grows a second copy of the collection, hashing or verification logic (the harness and
teardown import the module for exactly that reason).

    list [<comp>] [--json]          index rows for one comp, or a comp-grouped scan
    show <comp> <key> [--json]      identity, per-target/file statuses, warnings, drift problems
    plan <comp> <key>               what the collector would attempt now, target by target
    verify <comp> <key> [--seal]    re-hash everything, count TODO(author); --seal closes it
    collect <comp> <key>            retry the pull (talks to the estate; --dry-run does not)
    archive <comp> <key>|--all      copy a finished folder out of a worktree that is going away

`collect`, `archive` and `verify --seal` are the commands that change a test folder; `list` may
rewrite the derived `index.json`. Nothing here creates, starts, stops or destroys a VM: `collect`
is the one command that talks to the estate, and it only reads. Reports can quote credentials, so
`show` prints paths and sha256 and never file contents.

Comps resolve as `competitions/<id>` (destroy-competition.py's convention), or as any path that
holds a `Compfile`. Exit codes follow verify-competition.py: 0 clean, 1 findings or a refusal,
2 usage error (unknown competition or key). `show` and `plan` are views and exit 0 once they have
printed; `verify` is the command whose exit code answers "is this folder finished?".
"""

import argparse
import json
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


def _existing_keys(comp_dir):
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
    known = _existing_keys(comp_dir)
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


def _index_rows(comp_dir, *, refresh=False):
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


# ── small printing helpers ─────────────────────────────────────────────────────────────


def _error(message, code=EXIT_USAGE):
    """Usage-level failure: one clear line on stderr, non-zero exit, no traceback."""
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def _unknown_comp(name):
    found = sorted(path.name for path in COMPETITIONS_DIR.iterdir() if path.is_dir()) \
        if COMPETITIONS_DIR.is_dir() else []
    return _error(
        f"unknown competition {name!r} — looked for {COMPETITIONS_DIR / name} and {Path(name)}, "
        f"both needing a Compfile. Competitions present: {', '.join(found) or 'none'}", EXIT_USAGE)


def _show(value):
    return "-" if value in (None, "") else str(value)


def _print_table(headers, rows, indent="  "):
    """Fixed-width table. A missing cell prints as `-` so a gap is visibly a gap."""
    cells = [[_show(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print((indent + "  ".join(h.ljust(w) for h, w in zip(headers, widths))).rstrip())
    print(indent + "  ".join("-" * width for width in widths))
    for row in cells:
        print((indent + "  ".join(c.ljust(w) for c, w in zip(row, widths))).rstrip())


def _want_label(want):
    """A wanted item's human id: remote path, local filename, glob, or capture destination.

    Wants come in three shapes — a remote/local file, a glob, and a read-only command whose
    stdout is captured (`cmd` + `local`). The label prefers a path in every case: `?` tells the
    operator nothing about which item is missing, and a command string is the least useful name
    for something whose result lands in a named file."""
    return (want.get("remote") or want.get("local_name") or want.get("pattern")
            or (Path(want["local"]).name if want.get("local") else None)
            or want.get("cmd") or "?")


def _agents_label(agents):
    present = [side for side in ("red", "blue") if (agents or {}).get(side)]
    return "+".join(present) if present else "-"


def _agents_line(manifest):
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


def _summary_line(collection):
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


def _list_row(row):
    return (row.get("key"), row.get("run_id"), row.get("kind"), row.get("created_at"),
            row.get("phase"), row.get("verdict"), _agents_label(row.get("agents")),
            row.get("writeup"), row.get("warnings"), "yes" if row.get("report") else "-")


LIST_HEADERS = ("key", "run id", "kind", "created", "phase", "verdict", "agents", "writeup",
                "warn", "report")


# ── list ───────────────────────────────────────────────────────────────────────────────


def cmd_list(args):
    if args.comp:
        comp_dir = resolve_comp(args.comp)
        if comp_dir is None:
            return _unknown_comp(args.comp)
        try:
            rows = _index_rows(comp_dir, refresh=args.refresh)
        except RuntimeError as e:
            return _error(str(e), EXIT_FINDINGS)
        root = ao.artifacts_root(comp_dir)
        if args.json:
            print(json.dumps({"comp": comp_dir.name, "artifacts_root": str(root),
                              "tests": rows}, indent=2))
            return EXIT_OK
        print(f"Test artifacts for '{comp_dir.name}' — {root}")
        if rows:
            _print_table(LIST_HEADERS, [_list_row(row) for row in rows])
        else:
            print("  no test folders yet — one appears when a run collects artifacts "
                  "(harness, teardown, or `collect`)")
        return EXIT_OK

    scan = {}
    if COMPETITIONS_DIR.is_dir():
        for comp_dir in sorted(path for path in COMPETITIONS_DIR.iterdir() if path.is_dir()):
            if not ao.artifacts_root(comp_dir).is_dir():
                continue
            try:
                scan[comp_dir.name] = _index_rows(comp_dir, refresh=args.refresh)
            except RuntimeError as e:
                # One broken comp must not hide every other comp's rows from the scan.
                print(f"WARNING: {comp_dir.name}: {e}", file=sys.stderr)
    if args.json:
        print(json.dumps({"competitions": scan}, indent=2))
        return EXIT_OK
    if not scan:
        print(f"No competition under {COMPETITIONS_DIR}/ has a {ao.DIRNAME}/ folder yet.")
        return EXIT_OK
    for name, rows in scan.items():
        print(f"\n{name} — {len(rows)} test folder(s)")
        if rows:
            _print_table(LIST_HEADERS, [_list_row(row) for row in rows])
        else:
            print("  (the drop point exists but holds no test folder yet)")
    return EXIT_OK


# ── show ───────────────────────────────────────────────────────────────────────────────


def _document_rows(test_path, collection):
    """The three canonical documents: presence, sha256 from collection.json, and its provenance.

    A document with no collection entry was written locally — REPORT.md by its author, the two
    side documents by `write_stub` when collection could not obtain them — so the line says that
    rather than implying a provenance that does not exist."""
    derived = {entry.get("path"): entry for entry in collection.get("derived", [])}
    rows = []
    for name in (*ao.SIDES.values(), ao.REPORT_NAME):
        path = Path(test_path) / name
        entry = derived.get(name) or {}
        if entry.get("sha256"):
            sha = entry["sha256"]
            provenance = " — ".join(part for part in (
                f"collection.json: {entry.get('method') or 'collected'}",
                f"from {entry['from']}" if entry.get("from") else "",
                f"re-verified {entry['reverified_at']}" if entry.get("reverified_at") else "",
            ) if part)
        elif path.exists():
            try:
                sha, note = ao.sha256_file(path), _local_note(name, path)
            except OSError as e:
                sha, note = "-", f"unreadable: {e}"
            provenance = f"computed now — not in collection.json ({note})"
        else:
            sha, provenance = "-", "not present"
        rows.append({"document": name, "present": path.exists(), "sha256": sha,
                     "provenance": provenance})
    return rows


def _local_note(name, path):
    """What a document with no collection entry is: a stub (with its own status) or authored.

    Only the frontmatter `status:` line is read here — contents are evidence, and this command
    prints hashes and paths, never what a report says."""
    match = STUB_STATUS_RE.search(path.read_text(errors="ignore"))
    if match:
        return f"locally written stub: {match.group(1)}"
    return "authored synthesis" if name == ao.REPORT_NAME else "locally written"


def _print_collection(collection, indent="  "):
    """Per-target and per-file statuses in full: this is the 'why is it missing?' answer."""
    if not collection:
        print(f"{indent}no {ao.COLLECTION_NAME} — nothing was collected for this run "
              "(`collect` or teardown writes it)")
        return
    print(f"{indent}collection.json collected_at {_show(collection.get('collected_at'))} "
          f"— {_summary_line(collection)}")
    print(f"{indent}targets and files:")
    for target in collection.get("targets", []):
        files = target.get("files", [])
        print(f"{indent}  {_show(target.get('name'))} — {_show(target.get('route'))} — "
              f"{_show(target.get('status'))} — {len(files)} file record(s)")
        for label, value in (("note", target.get("note")), ("reason", target.get("reason"))):
            if value:
                print(f"{indent}      {label}: {value}")
        for error in target.get("errors", []):
            print(f"{indent}      error: {error}")
        for item in files:
            # A collection record carries the label of what was wanted in `want`; the remote
            # path itself is not on the record, so neither the plan's helper nor the remote key
            # is the right source here.
            label = item.get("want") or _want_label(item)
            line = f"{indent}      {_show(label)} -> {_show(item.get('status'))}"
            if item.get("bytes") is not None:
                line += f", {item['bytes']} B"
            if item.get("sha256"):
                line += f", sha256 {item['sha256'][:16]}…"
            print(line)
            if item.get("local"):
                print(f"{indent}        local: {item['local']}")
            if item.get("reason"):
                print(f"{indent}        reason: {item['reason']}")
            if item.get("reverified_at"):
                print(f"{indent}        re-verified at {item['reverified_at']}")


def cmd_show(args):
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return _unknown_comp(args.comp)
    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return _error(message, EXIT_USAGE)

    manifest = ao.load_manifest(path)
    collection = ao.load_collection(path)
    try:
        check = ao.verify_test(path)
    except OSError as e:
        # show is the read surface: an unreadable artifact is a finding to print, never a
        # traceback that hides the identity and statuses the operator came for.
        check = {"ok": False, "checked": 0, "problems": [f"could not re-hash: {e}"], "todos": 0,
                 "writeup": (manifest.get("writeup") or {}).get("status")}
    warnings = ao.warn_summary(collection, manifest)
    documents = _document_rows(path, collection)

    if args.json:
        print(json.dumps({"path": str(path), "identity": manifest, "collection": collection,
                          "documents": documents, "warnings": warnings,
                          "problems": check["problems"], "todos": check["todos"]}, indent=2))
        return EXIT_OK

    created_by = manifest.get("created_by") or {}
    print(f"Test artifacts — {path}")
    print("Identity (test.json)")
    print(f"  competition : {_show(manifest.get('comp_name'))} "
          f"({_show(manifest.get('comp'))}) — {_show(manifest.get('comp_dir'))}")
    print(f"  key         : {_show(manifest.get('key') or path.name)}")
    print(f"  run id      : {_show(manifest.get('run_id'))}"
          + ("" if manifest.get("run_id") else " (untagged — pre-run-id deploy state)"))
    print(f"  kind        : {_show(manifest.get('kind'))} "
          f"(label: {_show(manifest.get('label'))})")
    print(f"  created     : {_show(manifest.get('created_at'))} by "
          f"{_show(created_by.get('script'))} @ {_show(created_by.get('git_rev'))}, "
          f"worktree {_show(created_by.get('worktree'))} "
          f"(dirty: {_show(created_by.get('dirty'))})")
    print(f"  phase       : {_show(manifest.get('phase'))} "
          f"({len(manifest.get('phases') or [])} marker(s) recorded)")
    print(f"  nodes       : {', '.join(manifest.get('nodes') or []) or '-'}")
    print(f"  endpoint    : {_show(manifest.get('endpoint'))}")
    print(f"  teams/boxes : {_show(manifest.get('teams'))} / "
          f"{', '.join(manifest.get('boxes') or []) or '-'}")
    print(f"  agents      : {_agents_line(manifest)}")
    writeup = manifest.get("writeup") or {}
    print(f"  writeup     : {_show(writeup.get('status'))} "
          f"(author {_show(writeup.get('author'))}, "
          f"completed {_show(writeup.get('completed_at'))})")

    print("\nCollection (collection.json)")
    _print_collection(collection)

    print("\nCanonical documents")
    for row in documents:
        print(f"  {row['document']:<14} {'present' if row['present'] else 'MISSING':<8} "
              f"sha256 {row['sha256']}  ({row['provenance']})")

    print("\nWarnings (warn_summary — what was wanted and is not here)")
    for warning in warnings:
        print(f"  WARNING: {warning}")
    if not warnings:
        print("  none")

    print("\nVerify (drift and completeness)")
    print(f"  {check['checked']} collected file(s) re-hashed; "
          f"{check['todos']} unfilled TODO(author) marker(s) in {ao.REPORT_NAME}")
    for problem in check["problems"]:
        print(f"  problem: {problem}")
    if not check["problems"]:
        print("  no problems")
    print("\n(show is the read surface and exits 0 once it has printed — "
          "run `verify` for the findings-driven exit code)")
    return EXIT_OK


# ── plan ───────────────────────────────────────────────────────────────────────────────


def _wants(target):
    return list(target.get("want") or []) + list(target.get("want_globs") or [])


def cmd_plan(args):
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return _unknown_comp(args.comp)
    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return _error(message, EXIT_USAGE)

    manifest = ao.load_manifest(path)
    targets = ao.plan_targets(manifest, comp_dir=comp_dir)
    print(f"Collection plan for {path}")
    print(f"  {_summary_line_for_plan(targets)} — derived from test.json only (plan_targets() "
          "does no I/O)")
    _print_table(("target", "route", "status", "files wanted", "note"),
                 [(target.get("name"), target.get("route"),
                   target.get("status") or "planned", len(_wants(target)),
                   target.get("note")) for target in targets])

    print("\nWanted items (what the collector would look for, and where it would land)")
    for target in targets:
        wants = _wants(target)
        if not wants:
            continue
        destination = target.get("to") or target.get("root") or "-"
        print(f"  {target.get('name')} ({target.get('route')}) -> {destination}")
        for want in wants:
            line = f"      {_want_label(want)}"
            if want.get("local"):
                line += f"  ->  {want['local']}"
            elif want.get("to"):
                line += f"  ->  {want['to']}"
            if want.get("canonical"):
                line += f"  [canonical: {want['canonical']}]"
            print(line)

    print("\n  This is what the collector would attempt NOW: plan_targets() reads the manifest")
    print("  and the recorded paths, and contacts nothing. Run `collect` to make the attempt.")
    if ao.in_worktree():
        print("  This checkout is a LINKED WORKTREE: the competition dir — with this test")
        print("  folder — is removed with the worktree. Archive it before `git worktree remove`:")
        print(f"      python3 test-artifacts.py archive {args.comp} {args.key}")
    else:
        print("  This checkout is the MAIN worktree (not a linked one), so this test folder")
        print("  survives a normal cleanup (`archive` is still how a folder leaves the repo).")
    return EXIT_OK


def _summary_line_for_plan(targets):
    planned = sum(1 for target in targets if not target.get("status"))
    skipped = [target["name"] for target in targets if target.get("status")]
    line = f"{len(targets)} target(s): {planned} to attempt"
    if skipped:
        line += f", {len(skipped)} already settled/not applicable ({', '.join(skipped)})"
    return line


# ── verify / seal ──────────────────────────────────────────────────────────────────────


def cmd_verify(args):
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return _unknown_comp(args.comp)
    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return _error(message, EXIT_USAGE)

    try:
        check = ao.verify_test(path)
    except OSError as e:
        return _error(f"could not re-hash {path}: {e}", EXIT_FINDINGS)
    print(f"Verifying test artifacts — {path}")
    print(f"  checked      : {check['checked']} collected file(s) re-hashed")
    print(f"  writeup      : {_show(check['writeup'])}")
    print(f"  TODO(author) : {check['todos']} unfilled marker(s) in {ao.REPORT_NAME}")
    if check["problems"]:
        print(f"  problems     : {len(check['problems'])}")
        for problem in check["problems"]:
            print(f"    - {problem}")
    else:
        print("  problems     : none")

    if not args.seal:
        print("RESULT: PASS — nothing drifted and no TODO(author) marker remains."
              if check["ok"] else
              "RESULT: FAIL — see the problems/TODO count above.")
        return EXIT_OK if check["ok"] else EXIT_FINDINGS

    try:
        sealed = ao.seal_test(path, author=args.author, force=args.force)
    except RuntimeError as e:
        # seal_test's own refusal (unfilled TODO markers, or no REPORT.md at all) — the operator
        # decides: fill the sections, or waive them explicitly with --force.
        print(f"SEAL REFUSED: {e}", file=sys.stderr)
        return EXIT_FINDINGS
    except OSError as e:
        # The refusal above is a decision; this is the filesystem saying no. Distinct wording so
        # an operator does not go looking for a TODO marker that is not the problem.
        print(f"SEAL FAILED: could not write to {path}: {e}", file=sys.stderr)
        return EXIT_FINDINGS
    post = sealed.get("verify") or {}
    print(f"  SEALED      : writeup done by {_show(sealed.get('author'))} "
          f"(recorded in {ao.MANIFEST_NAME})")
    if sealed.get("archived"):
        # seal_test refreshes the durable copy itself when this checkout is a linked worktree;
        # saying where it went is the whole point of the archive being automatic.
        print(f"  archived    : {sealed['archived']} (the archive no longer holds a skeleton)")
    print(f"  post-seal   : {post.get('checked', 0)} file(s) re-hashed, "
          f"{len(post.get('problems') or [])} problem(s)")
    if post.get("todos"):
        # --force waives the markers; it does not resolve them, and nothing in test.json records
        # the waiver — so say out loud that a later `verify` will still count them.
        print(f"  waived      : {post['todos']} TODO(author) marker(s) remain — sealed with "
              "--force, so they were waived, not filled; `verify` without --force still "
              "reports them")
    if post.get("problems"):
        for problem in post["problems"]:
            print(f"    - {problem}")
        print("RESULT: FAIL — the seal was written, but the folder still has problems.")
        return EXIT_FINDINGS
    print("RESULT: PASS — sealed and clean.")
    return EXIT_OK


# ── collect ────────────────────────────────────────────────────────────────────────────


def cmd_collect(args):
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return _unknown_comp(args.comp)
    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return _error(message, EXIT_USAGE)

    manifest = ao.load_manifest(path)
    targets = ao.plan_targets(manifest, comp_dir=comp_dir)
    wanted = sum(len(_wants(target)) for target in targets)
    print(f"Collecting into {path}")
    if args.dry_run:
        print("  DRY RUN — nothing is fetched and no box is contacted; every wanted item is")
        print("  recorded as `skipped` so the plan is visible in collection.json.")
    else:
        print("  LIVE COLLECTION — this reads from the estate: scp to red01 through the jump")
        print("  host, guest-agent reads, and local copies from the recorded run dir / blue")
        print("  workdirs. It changes nothing on any box, and an unreachable source is recorded")
        print("  as such rather than retried.")
    print(f"  {len(targets)} target(s), {wanted} wanted item(s) — from plan_targets()")

    try:
        collection = ao.collect(path, targets, dry_run=args.dry_run)
    except RuntimeError as e:
        # A route with no transport is a wiring bug (artifacts_ops refuses to record it), so this
        # is the one collect failure that is not a per-file status.
        return _error(f"collection could not run: {e}", EXIT_FINDINGS)
    except OSError as e:
        return _error(f"collection started but could not write into {path}: {e}", EXIT_FINDINGS)

    warnings = ao.warn_summary(collection, manifest)
    print(f"\nCollection summary: {_summary_line(collection)}")
    for warning in warnings:
        prefix = ("WARNING: [dry run — expected, nothing was fetched]"
                  if args.dry_run else "WARNING:")
        print(f"  {prefix} {warning}")
    if args.dry_run:
        print("RESULT: DRY RUN — nothing was attempted, so the exit code says nothing about "
              "the estate.")
        return EXIT_OK
    if warnings:
        print(f"RESULT: INCOMPLETE — {len(warnings)} warning(s); collection.json records exactly "
              "which wanted items are missing, and `show` explains each one.")
        return EXIT_FINDINGS
    print("RESULT: COMPLETE — every wanted item was obtained or truthfully skipped.")
    return EXIT_OK


# ── archive ────────────────────────────────────────────────────────────────────────────


def cmd_archive(args):
    comp_dir = resolve_comp(args.comp)
    if comp_dir is None:
        return _unknown_comp(args.comp)
    if bool(args.all) == bool(args.key):
        return _error("archive needs exactly one of <key> or --all "
                      "(a key archives one folder, --all archives every key in the comp)",
                      EXIT_USAGE)
    dest_root = Path(args.dest) if args.dest else None
    default_root = dest_root or ao.default_archive_root()

    if args.all:
        keys = _existing_keys(comp_dir)
        if not keys:
            return _error(f"no test folders under {ao.artifacts_root(comp_dir)} to archive",
                          EXIT_USAGE)
        failures = []
        for key in keys:
            try:
                dest = ao.archive_test(comp_dir, key, dest_root=dest_root)
            except (OSError, RuntimeError) as e:
                # archive_test verifies every byte by hash; a mismatch is reported per key and
                # the rest still run, because losing one folder must not cost the others.
                failures.append(key)
                print(f"  FAILED  {key}: {e}")
                continue
            print(f"  archived {key} -> {dest}")
        if failures:
            return _error(f"{len(failures)} of {len(keys)} folder(s) failed to archive "
                          f"({', '.join(failures)}); the rest are under {default_root}",
                          EXIT_FINDINGS)
        print(f"{len(keys)} test folder(s) archived under {default_root}")
        return EXIT_OK

    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return _error(message, EXIT_USAGE)
    try:
        dest = ao.archive_test(comp_dir, args.key, dest_root=dest_root)
    except (OSError, RuntimeError) as e:
        # RuntimeError is archive_test's own hash-verification failure; OSError is the copy
        # itself failing. Both mean the folder is NOT safely outside the repo — say so loudly.
        return _error(f"archive failed for {args.key} (no verified copy was made): {e}",
                      EXIT_FINDINGS)
    print(f"archived {args.key} -> {dest}")
    if ao.in_worktree():
        print("  this checkout is a LINKED WORKTREE — the copy is now outside the repo, so the "
              "worktree can be removed without losing the run's artifacts")
    return EXIT_OK


# ── entry point ────────────────────────────────────────────────────────────────────────


def build_parser():
    parser = argparse.ArgumentParser(
        prog="test-artifacts.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Read, inspect and repair per-run test artifacts\n"
            f"(competitions/<comp>/{ao.DIRNAME}/<key>/, written by artifacts_ops.py).\n"
            "Comps resolve as competitions/<id>, or as a path holding a Compfile."),
        epilog=(
            "artifact statuses used by the collector: " + ", ".join(ao.STATUSES) + "\n"
            "warnings come from: " + ", ".join(ao.LOST) + ", plus a canonical document that a "
            "present agent should have produced\n"
            "exit codes: 0 clean, 1 findings/refusal, 2 usage error (unknown comp or key)"))
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("list", help="index rows for one comp, or a comp-grouped scan")
    p.add_argument("comp", nargs="?", help="competition id under competitions/ (or a path "
                                          "containing a Compfile); omit to scan every competition")
    p.add_argument("--json", action="store_true", help="machine-readable rows")
    p.add_argument("--refresh", action="store_true",
                   help="rebuild index.json from the test folders even when it looks current")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="identity, target/file statuses, warnings and drift")
    p.add_argument("comp")
    p.add_argument("key", help="test folder name (the run id, or untagged-<stamp>)")
    p.add_argument("--json", action="store_true", help="machine-readable manifest + collection")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("plan", help="what the collector would attempt now, target by target")
    p.add_argument("comp")
    p.add_argument("key")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("verify", help="re-hash the folder, count TODO(author), optionally seal")
    p.add_argument("comp")
    p.add_argument("key")
    p.add_argument("--seal", action="store_true",
                   help="after verifying, flip the write-up to done (refused while TODO(author) "
                        "markers remain)")
    p.add_argument("--author", metavar="NAME", help="name recorded as the write-up's author")
    p.add_argument("--force", action="store_true",
                   help="with --seal: seal even though TODO(author) markers remain — an explicit "
                        "waiver, printed, not a silent one. Structural problems still fail.")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("collect", help="re-run the collector against an existing test folder")
    p.add_argument("comp")
    p.add_argument("key")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="plan only: record every wanted item as skipped and contact no box")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("archive", help="copy a test folder outside the repo (verified by hash)")
    p.add_argument("comp")
    p.add_argument("key", nargs="?",
                   help="test folder name; omit when using --all")
    p.add_argument("--all", action="store_true", help="archive every key in this competition")
    p.add_argument("--dest", metavar="DIR",
                   help="destination root (default: artifacts_ops.default_archive_root(), i.e. "
                        "$TEZ_ARTIFACTS_ARCHIVE or "
                        f"{ao.default_archive_root()}) — each folder lands at "
                        "<root>/<comp>/<key>/")
    p.set_defaults(func=cmd_archive)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
