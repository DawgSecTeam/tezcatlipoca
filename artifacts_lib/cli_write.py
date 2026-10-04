"""Subcommands that change a test folder or talk to the estate: verify, collect, archive."""

import sys
from pathlib import Path

import artifacts_ops as ao
from .cli_common import (EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, error, existing_keys, locate_test,
                         resolve_comp, resolve_test, show, summary_line, unknown_comp, wants)


def cmd_verify(args):
    comp_dir, path, failure = locate_test(args)
    if failure is not None:
        return failure

    try:
        check = ao.verify_test(path)
    except OSError as e:
        return error(f"could not re-hash {path}: {e}", EXIT_FINDINGS)
    print(f"Verifying test artifacts — {path}")
    print(f"  checked      : {check['checked']} collected file(s) re-hashed")
    print(f"  writeup      : {show(check['writeup'])}")
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
    print(f"  SEALED      : writeup done by {show(sealed.get('author'))} "
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
    comp_dir, path, failure = locate_test(args)
    if failure is not None:
        return failure

    manifest = ao.load_manifest(path)
    targets = ao.plan_targets(manifest, comp_dir=comp_dir)
    wanted = sum(len(wants(target)) for target in targets)
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
        return error(f"collection could not run: {e}", EXIT_FINDINGS)
    except OSError as e:
        return error(f"collection started but could not write into {path}: {e}", EXIT_FINDINGS)

    warnings = ao.warn_summary(collection, manifest)
    print(f"\nCollection summary: {summary_line(collection)}")
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
        return unknown_comp(args.comp)
    if bool(args.all) == bool(args.key):
        return error("archive needs exactly one of <key> or --all "
                      "(a key archives one folder, --all archives every key in the comp)",
                      EXIT_USAGE)
    dest_root = Path(args.dest) if args.dest else None
    default_root = dest_root or ao.default_archive_root()

    if args.all:
        keys = existing_keys(comp_dir)
        if not keys:
            return error(f"no test folders under {ao.artifacts_root(comp_dir)} to archive",
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
            return error(f"{len(failures)} of {len(keys)} folder(s) failed to archive "
                          f"({', '.join(failures)}); the rest are under {default_root}",
                          EXIT_FINDINGS)
        print(f"{len(keys)} test folder(s) archived under {default_root}")
        return EXIT_OK

    path, message = resolve_test(comp_dir, args.key)
    if path is None:
        return error(message, EXIT_USAGE)
    try:
        dest = ao.archive_test(comp_dir, args.key, dest_root=dest_root)
    except (OSError, RuntimeError) as e:
        # RuntimeError is archive_test's own hash-verification failure; OSError is the copy
        # itself failing. Both mean the folder is NOT safely outside the repo — say so loudly.
        return error(f"archive failed for {args.key} (no verified copy was made): {e}",
                      EXIT_FINDINGS)
    print(f"archived {args.key} -> {dest}")
    if ao.in_worktree():
        print("  this checkout is a LINKED WORKTREE — the copy is now outside the repo, so the "
              "worktree can be removed without losing the run's artifacts")
    return EXIT_OK
