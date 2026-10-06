#!/usr/bin/env python3
"""Interaction report for an agent-scrim run dir — writes INTERACTION.md next to FINDINGS.md.

Thin entrypoint: the report lives in the `scrim_report/` package (module map in
docs/scrim-harness.md); `scrim_report.cli` owns the flags.
"""

from scrim_report.cli import main

if __name__ == "__main__":
    main()
