#!/usr/bin/env python3
"""Deploy, verify, and run an agent-manned red-vs-blue scrim end to end.

Thin entrypoint: the harness lives in the `scrim/` package (module map in
docs/scrim-harness.md); `scrim.cli` owns the flags and the stage sequence.
"""

from scrim.cli import main

if __name__ == "__main__":
    main()
