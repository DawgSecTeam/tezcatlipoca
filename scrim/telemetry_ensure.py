"""Per-comp telemetry ensure hook, run by scrim while the red director is stopped (A4).

A competition may ship `telemetry.ensure.sh` — an idempotent script (any language; it
runs on the operator host with TEZ_COMP_DIR/TEZ_RUN_DIR exported) that installs or
repairs the feeds the telemetry gate measures. Goldens bake forwarders when they can;
this hook is for comps that wire telemetry AFTER phase 8. Placement is deliberate:
scrim calls it between `stop_red_director` and the verifies, because run-14ac7f14
provisioned forwarders while a pre-T0 tlaloc was still cycling — and lost them.
A declared script that fails FAILS the staging: feeds the gate would measure don't
exist, and starting anyway just spends the event discovering that."""

import os
from pathlib import Path

from common import procs
from scrim import core
from scrim.core import log


def run_if_declared(args, comp):
    script = Path(comp) / "telemetry.ensure.sh"
    if not script.exists():
        return None
    log(f"telemetry ensure: {script.name} — running with red stopped; the pre-T0 gate "
        f"measures what this leaves behind")
    env = dict(os.environ, TEZ_COMP_DIR=str(comp), TEZ_RUN_DIR=str(args.run_dir or ""))
    r = procs.run(["bash", str(script)], cwd=core.REPO, env=env, timeout=1800,
                  check=False, tail=15)
    if r.returncode != 0:
        raise RuntimeError(
            f"telemetry ensure script failed (rc={r.returncode}): "
            f"{(r.stderr or r.stdout or '')[-300:]} — the telemetry gate cannot pass "
            f"without it; fix {script.name} or remove the declaration")
    log("telemetry ensure: complete")
    return r
