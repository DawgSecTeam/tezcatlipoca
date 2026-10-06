"""Read-only subcommands: list, show, plan."""

import json
import sys
from pathlib import Path

import artifacts_ops as ao
from .cli_common import (COMPETITIONS_DIR, EXIT_FINDINGS, EXIT_OK, LIST_HEADERS, STUB_STATUS_RE,
                         agents_line, display_label, error, index_rows, list_row, locate_test,
                         print_table, resolve_comp, show, summary_line, unknown_comp, wants)


def cmd_list(args):
    if args.comp:
        comp_dir = resolve_comp(args.comp)
        if comp_dir is None:
            return unknown_comp(args.comp)
        try:
            rows = index_rows(comp_dir, refresh=args.refresh)
        except RuntimeError as e:
            return error(str(e), EXIT_FINDINGS)
        root = ao.artifacts_root(comp_dir)
        if args.json:
            print(json.dumps({"comp": comp_dir.name, "artifacts_root": str(root),
                              "tests": rows}, indent=2))
            return EXIT_OK
        print(f"Test artifacts for '{comp_dir.name}' — {root}")
        if rows:
            print_table(LIST_HEADERS, [list_row(row) for row in rows])
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
                scan[comp_dir.name] = index_rows(comp_dir, refresh=args.refresh)
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
            print_table(LIST_HEADERS, [list_row(row) for row in rows])
        else:
            print("  (the drop point exists but holds no test folder yet)")
    return EXIT_OK


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
    print(f"{indent}collection.json collected_at {show(collection.get('collected_at'))} "
          f"— {summary_line(collection)}")
    print(f"{indent}targets and files:")
    for target in collection.get("targets", []):
        files = target.get("files", [])
        print(f"{indent}  {show(target.get('name'))} — {show(target.get('route'))} — "
              f"{show(target.get('status'))} — {len(files)} file record(s)")
        for label, value in (("note", target.get("note")), ("reason", target.get("reason"))):
            if value:
                print(f"{indent}      {label}: {value}")
        for err in target.get("errors", []):
            print(f"{indent}      error: {err}")
        for item in files:
            # A collection record carries the label of what was wanted in `want`; the remote
            # path itself is not on the record, so neither the plan's helper nor the remote key
            # is the right source here.
            label = item.get("want") or display_label(item)
            line = f"{indent}      {show(label)} -> {show(item.get('status'))}"
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
    comp_dir, path, failure = locate_test(args)
    if failure is not None:
        return failure

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
    print(f"  competition : {show(manifest.get('comp_name'))} "
          f"({show(manifest.get('comp'))}) — {show(manifest.get('comp_dir'))}")
    print(f"  key         : {show(manifest.get('key') or path.name)}")
    print(f"  run id      : {show(manifest.get('run_id'))}"
          + ("" if manifest.get("run_id") else " (none — no deploy state)"))
    print(f"  kind        : {show(manifest.get('kind'))} "
          f"(label: {show(manifest.get('label'))})")
    print(f"  created     : {show(manifest.get('created_at'))} by "
          f"{show(created_by.get('script'))} @ {show(created_by.get('git_rev'))}, "
          f"worktree {show(created_by.get('worktree'))} "
          f"(dirty: {show(created_by.get('dirty'))})")
    print(f"  phase       : {show(manifest.get('phase'))} "
          f"({len(manifest.get('phases') or [])} marker(s) recorded)")
    print(f"  nodes       : {', '.join(manifest.get('nodes') or []) or '-'}")
    print(f"  endpoint    : {show(manifest.get('endpoint'))}")
    print(f"  teams/boxes : {show(manifest.get('teams'))} / "
          f"{', '.join(manifest.get('boxes') or []) or '-'}")
    print(f"  agents      : {agents_line(manifest)}")
    writeup = manifest.get("writeup") or {}
    print(f"  writeup     : {show(writeup.get('status'))} "
          f"(author {show(writeup.get('author'))}, "
          f"completed {show(writeup.get('completed_at'))})")

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


def cmd_plan(args):
    comp_dir, path, failure = locate_test(args)
    if failure is not None:
        return failure

    manifest = ao.load_manifest(path)
    targets = ao.plan_targets(manifest, comp_dir=comp_dir)
    print(f"Collection plan for {path}")
    print(f"  {_summary_line_for_plan(targets)} — derived from test.json only (plan_targets() "
          "does no I/O)")
    print_table(("target", "route", "status", "files wanted", "note"),
                 [(target.get("name"), target.get("route"),
                   target.get("status") or "planned", len(wants(target)),
                   target.get("note")) for target in targets])

    print("\nWanted items (what the collector would look for, and where it would land)")
    for target in targets:
        wanted = wants(target)
        if not wanted:
            continue
        destination = target.get("to") or target.get("root") or "-"
        print(f"  {target.get('name')} ({target.get('route')}) -> {destination}")
        for want in wanted:
            line = f"      {display_label(want)}"
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
