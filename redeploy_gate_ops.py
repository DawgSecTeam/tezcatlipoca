"""Redeploy gates: load a deployed range and refuse one this pipeline cannot safely drive.

The state-version / run-id gate lives here, once, and runs before main() activates the
placement or touches Proxmox (snapshots, clones, destroys). Mirrors destroy_gate_ops on
the teardown side."""

import json

from constants import PIPELINE_VERSION


def load_deployed_range(comp_dir):
# Return (teams, boxes, state, state_path) for a deployed competition, or exit.
#
#     Refuses a range written by an older pipeline (no matching PIPELINE_VERSION or no run
#     id): redeploy's ownership-tagged destroys cannot be proven safe against it.
    teams_path = comp_dir / "teams.json"
    boxes_path = comp_dir / "boxes.json"
    state_path = comp_dir / ".deploy_state.json"
    for p in (teams_path, boxes_path):
        if not p.exists():
            raise SystemExit(f"  ERROR: {p} is missing — this competition was never deployed.")

    teams = json.loads(teams_path.read_text())
    boxes = json.loads(boxes_path.read_text())
    if not state_path.exists():
        raise SystemExit(f"  ERROR: {state_path} is missing — this competition was never deployed.")
    state = json.loads(state_path.read_text())
    if state.get("pipeline_version") != PIPELINE_VERSION or not state.get("run_id"):
        raise SystemExit(
            f"  ERROR: {state_path} was written by an older pipeline (this is "
            f"v{PIPELINE_VERSION}, with run ids) — redeploy can't safely drive that range. "
            f"Tear it down by hand and deploy fresh.")
    return teams, boxes, state, state_path
