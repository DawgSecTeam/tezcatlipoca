"""Central constants for vmid math, snapshots, budgets, naming, and the nakon path."""

from pathlib import Path

MAX_BOXES_PER_TEAM = 10

MAX_TEAMS = 154

SCORING_ENGINE_VMID = 1000

SNAP_BASE = "tz-base"
SNAP_READY = "tz-ready"

PER_MACHINE_NAKON_BUDGET = 2400

SLOW_SERVICES = ("splunk", "roundcube")

DISRUPTIVE_CONFIGS = {"resolv-conf-null-dns", "apt-sources-empty", "apt-hold-all-packages", "dpkg-broken-hold-state"}

WINDOWS_ADMIN_USER = "Administrator"

NAKON_DIR = Path("vendor/nakon")
