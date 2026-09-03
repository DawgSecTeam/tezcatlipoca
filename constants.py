"""Central constants for vmid math, snapshots, budgets, naming, and nakon path.
VM IDs use stride 10 (mirrored in terraform/main.tf); defines MAX_TEAMS, SNAP_*.
Shared by range_ops, deploy, redeploy, and verify.
"""

from pathlib import Path

MAX_BOXES_PER_TEAM = 10

MAX_TEAMS = 154

SCORING_ENGINE_VMID = 1000

SNAP_BASE = "tz-base"
SNAP_READY = "tz-ready"

PER_MACHINE_NAKON_BUDGET = 2400

SLOW_SERVICES = ("splunk", "roundcube")  # excluded from auto randomize

DISRUPTIVE_CONFIGS = {"resolv-conf-null-dns", "apt-sources-empty", "apt-hold-all-packages", "dpkg-broken-hold-state"}

WINDOWS_ADMIN_USER = "Administrator"

REBOOTS_BOX_CONFIGS = {"ADDS", "Domain Join"}

NAKON_DIR = Path("vendor/nakon")
