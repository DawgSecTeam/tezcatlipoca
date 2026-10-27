"""Hold a deploy at a phase boundary when the run queue asks for one.

A boundary is the only consistent place to stop a deploy: a phase can have an API burst
or a VM build in flight, and cutting one open mid-call leaves the recorded estate
behind reality — the mismatch the resume guards and the "already exists" resume hint exist
for. The run queue sets TEZ_PAUSE_FILE to a per-run control file: the file existing means
"hold at the next boundary", deleting it means "carry on", and the held phase goes into
the state document so an observer (the dashboard) sees the hold without parsing stdout.
Unset — a manual deploy — is a no-op.
"""

import os
import time
from pathlib import Path


def pause_at_boundary(ctx, phase):
    # Deliberately NOT a config key: this is the run queue's per-run handoff, not operator
    # estate config (see docs/architecture.md § Secrets and state).
    value = os.environ.get("TEZ_PAUSE_FILE", "")
    path = Path(value) if value else None
    if path is None or not path.exists():
        return
    ctx.state["paused_after_phase"] = phase
    ctx.save_state()
    print(f"\n  [||] Paused after phase {phase}/8 of '{ctx.comp_name}' — holding at the "
          f"boundary. Resume the run from the dashboard (or delete {path}) to continue.")
    while path.exists():
        time.sleep(1)
    ctx.state.pop("paused_after_phase", None)
    ctx.save_state()
    print(f"  [>>] Resumed after phase {phase}/8.")
