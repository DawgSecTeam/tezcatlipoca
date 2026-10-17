"""Redeploy gate: load a deployed range and refuse one this pipeline cannot safely drive."""

import json

from common.constants import PIPELINE_VERSION
from estate import state as estate_state


def load_deployed_range(comp_dir):
# Refuse a range written by an older pipeline: redeploy's ownership-tagged destroys cannot
#     be proven safe against it.
    teams_path = comp_dir / "teams.json"
    boxes_path = comp_dir / "boxes.json"
    state_path = estate_state.state_path(comp_dir)
    for p in (teams_path, boxes_path):
        if not p.exists():
            raise SystemExit(f"  ERROR: {p} is missing — this competition was never deployed.")

    teams = json.loads(teams_path.read_text())
    boxes = json.loads(boxes_path.read_text())
    state = estate_state.read_flat(comp_dir)
    if state is None:
        raise SystemExit(f"  ERROR: {state_path} is missing — this competition was never "
                         f"deployed.")
    if state.get("pipeline_version") != PIPELINE_VERSION or not state.get("run_id"):
        raise SystemExit(
            f"  ERROR: {state_path} was written by an older pipeline (this is "
            f"v{PIPELINE_VERSION}, with run ids) — redeploy can't safely drive that range. "
            f"Tear it down by hand and deploy fresh.")
    return teams, boxes, state, state_path
