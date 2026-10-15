"""The state a redeploy threads through every mode.

One object replaces the `(targets, ctx, node, comp_dir, state, ...)` positional cluster
every mode function used to re-declare — the redeploy analogue of deploy_lib's
DeployContext. Per-invocation values that vary within a run (the nakon bundle a reset
prepares lazily, a rollback's snapshot) stay explicit arguments, not fields here.
"""

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
