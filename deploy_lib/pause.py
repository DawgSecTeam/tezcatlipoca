"""Hold a deploy at a phase boundary when the run queue asks for one.

A boundary is the only consistent place to stop a deploy: a phase can have a terraform apply
or a VM build in flight, and cutting one open mid-call leaves terraform's view of the estate
behind reality — the mismatch the resume guards and the "already exists" resume hint exist
for. The run queue sets TEZ_PAUSE_FILE to a per-run control file: the file existing means
"hold at the next boundary", deleting it means "carry on", and the held phase goes into
.deploy_state.json so an observer (the dashboard) sees the hold without parsing stdout.
Unset — a manual deploy — is a no-op.
"""

import time
from pathlib import Path
from estate import config


def pause_at_boundary(ctx, phase):
    value = config.active().knobs.pause_file
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
