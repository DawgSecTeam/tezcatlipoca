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
import sys

import artifacts_ops as ao
from artifacts_lib.cli_common import EXIT_USAGE
from artifacts_lib.cli_read import cmd_list, cmd_plan, cmd_show
from artifacts_lib.cli_write import cmd_archive, cmd_collect, cmd_verify


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
