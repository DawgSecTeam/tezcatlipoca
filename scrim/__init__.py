"""The agent-scrim harness, one module per responsibility.

`run-agent-scrim.py` (repo root) is the CLI entrypoint; `scrim.cli.main` is the driver.
Cross-module calls are module-qualified (`procs.run_tree`, `quotient_api.qget`, ...) on
purpose: there is exactly one binding per function, so a test patches it where it is defined.
See docs/scrim-harness.md for the module map.
"""
