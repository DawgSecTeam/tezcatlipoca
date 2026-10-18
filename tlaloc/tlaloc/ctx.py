"""Ctx: the typed execution context every effect receives.

the earlier agent's ctx was an untyped dict whose dry-run flag was read two
different ways (``ctx["dry_run"]`` vs ``ctx["cfg"]["run"]["dry_run"]``) — so the
could silently bypass a runtime toggle. One dataclass, one dry_run field, read
one way, everywhere.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Ctx:
    cfg: dict                    # frozen config (never mutated mid-run)
    world: Any                   # state.world.World
    log: Any                     # events.EventLog
    intel: Any                   # intel.base.Intel
    gate: Any                    # exec.gate.Gate
    ssh: Any                     # exec.ssh.SSH
    c2: Any = None               # c2.mythic.Mythic | None
    sensors: Any = None          # sensors.Sensors | None
    policy: Any = None           # policy.Policy | None (ROE bounds this cycle)
    decider: Any = None          # decide.Decider | None (classification helpers)
    payload_manifest: Any = None  # frozen C2 payload manifest, when comp-prep ran
    dry_run: bool = False        # THE dry-run flag — single source
    comp: str = ""
    rng: random.Random = field(default_factory=random.Random)
    now: Callable[[], float] = time.time   # injectable clock (tests/replay)

    def os_of(self, ip):
        """Target OS name for gate linting: world target record -> intel."""
        rec = self.world.target_record(ip) or {}
        if rec.get("os"):
            return rec["os"]
        team_box = self.intel.box_by_ip(ip)
        if team_box:
            return "windows" if team_box.get("windows") else "linux"
        fh = self.world.foothold(ip)
        if fh:
            return "windows" if fh.get("windows") else "linux"
        return "linux"

    def is_windows(self, ip):
        return self.os_of(ip) == "windows"
