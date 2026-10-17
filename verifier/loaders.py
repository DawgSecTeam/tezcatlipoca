"""Readers for the competition directory files every gate consumes (teams, boxes, inject count)."""

import json

from estate import state as estate_state
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
    path = comp_dir / "credentials.txt"
    return path.read_text().splitlines() if path.exists() else None


def read_deploy_state(comp_dir):
# The deploy's flat state view, or None when there is no state or it is unreadable.
    return estate_state.read_flat(comp_dir)


def load_boxes(comp_dir):
    path = comp_dir / "nakon-config.json"
    if not path.exists():
        raise CheckError(f"nakon-config.json not found: {path}")
    try:
        return json.loads(path.read_text()).get("machines", [])
    except json.JSONDecodeError as e:
        raise CheckError(f"{path} is not valid JSON: {e}")


def count_local_injects(comp_dir):
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return None
    return sum(1 for sub in injects_dir.iterdir() if sub.is_dir() and (sub / "inject.json").exists())
