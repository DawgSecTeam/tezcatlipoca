"""Central constants for vmid math, snapshots, budgets, and naming.

VM IDs: 200 + identifier*10 + box_index (stride 10, mirrored in terraform/main.tf).
Snapshots, budgets, and naming conventions live here so range_ops, create-competition,
redeploy, and verify import from one place.
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
