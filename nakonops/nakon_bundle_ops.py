"""Build the nakon bundle on the operator host."""

import json
import subprocess
import sys
import threading

from common.constants import NAKON_DIR
from pathlib import Path


# M2.4: concurrent domain passes build their single-machine bundles on the operator host.
# The bundle dirs are content-addressed so parallel *different* configs don't collide, but
# the nakon build subprocess itself isn't audited for concurrent temp-state safety — one
# build at a time, deploys still run fully in parallel.
_BUNDLE_BUILD_LOCK = threading.Lock()


def build_nakon_bundle(config_path):
# Build (or reuse) the content-addressed Nakon bundle for this competition.
    with _BUNDLE_BUILD_LOCK:
        result = subprocess.run(
            [sys.executable, "-m", "nakon", "build",
             "--config", str(Path(config_path).resolve()),
             "--out", "bundles",
             "--json"],
            cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=900,
        )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon build failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine. Check vendor/nakon/.env."
        )

    info = json.loads(result.stdout.strip().splitlines()[-1])
    state = "cached" if info["cached"] else "fresh"
    print(f"  Nakon bundle {info['bundle_id'][:12]} ({state}, {info['plans']} plan(s), "
          f"{info['machines']} machine(s))")
    bundle_path = NAKON_DIR / info["path"]
    return bundle_path
