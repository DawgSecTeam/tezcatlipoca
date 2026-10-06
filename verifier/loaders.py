"""Readers for the competition directory files every gate consumes (teams, boxes, inject count)."""

import json

from verifier.model import CheckError


def load_teams(comp_dir):
    path = comp_dir / "teams.json"
    if not path.exists():
        raise CheckError(f"teams.json not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise CheckError(f"teams.json is not valid JSON: {e}")


def read_credentials_lines(comp_dir):
    """credentials.txt as a list of lines, or None when the file does not exist."""
    path = comp_dir / "credentials.txt"
    return path.read_text().splitlines() if path.exists() else None


def read_deploy_state(comp_dir):
    """.deploy_state.json as a dict, or None when absent, unreadable, or not an object.

    One definition of "what the deploy recorded" for every gate and the SUMMARY: each
    caller decides for itself what None means (SKIP, fail-closed, or "pre-tally")."""
    try:
        state = json.loads((comp_dir / ".deploy_state.json").read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def load_boxes(comp_dir):
    """Load boxes from per-competition nakon-config.json."""
    path = comp_dir / "nakon-config.json"
    if not path.exists():
        raise CheckError(f"nakon-config.json not found: {path}")
    try:
        return json.loads(path.read_text()).get("machines", [])
    except json.JSONDecodeError as e:
        raise CheckError(f"{path} is not valid JSON: {e}")


def count_local_injects(comp_dir):
    """Number of inject subdirectories (each carrying an inject.json). 0 if no injects/ dir."""
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return None
    return sum(1 for sub in injects_dir.iterdir() if sub.is_dir() and (sub / "inject.json").exists())
