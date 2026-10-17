"""State threaded through every redeploy mode; the redeploy analogue of deploy_lib's DeployContext."""

from dataclasses import dataclass
from typing import Any


@dataclass
class RedeployContext:
    comp_dir: Any
    name: str
    comp_name: str
    node: str
    teams: Any
    boxes: Any
    difficulty: Any
    state: dict
    state_path: Any
    ctx: dict  # engine context (scoring_engine_ip, ssh_key_path, ...)
