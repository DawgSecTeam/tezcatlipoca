"""Interaction report for an agent-scrim run dir — writes INTERACTION.md next to FINDINGS.md."""

import argparse
import sys
from pathlib import Path

from scrim_report import gate_table
from scrim_report import render


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing INTERACTION.md")
    ap.add_argument("--self-test", action="store_true",
                    help="assert the pinned agent-scrim-2026-09-17c numbers")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"no such run dir: {run_dir}")
    report, summary = render.build_report(run_dir)
    if args.stdout:
        print(report)
    else:
        out = run_dir / "INTERACTION.md"
        out.write_text(report)
        print(f"wrote {out}")
    print(f"interaction score: {summary['score']} "
          f"({summary['gates_failed']} gate(s) failed)")
    if args.self_test:
        sys.exit(gate_table.self_test(run_dir))
