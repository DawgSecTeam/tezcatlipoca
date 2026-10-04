"""Stable import surface for the hyphenated entry-point scripts.

`create-competition.py` and its companions cannot be imported under their own names (the
hyphen makes them invalid module names), and the old workaround was to load
create-competition.py through importlib and reach whatever the shim happened to have
imported as `driver.<symbol>`. That hid redeploy's true dependency set: the shim
re-exported all 74 names it imported for its own use, so a consumer could start using a
new symbol without anyone noticing, and the shim's surface could change silently under it.

This module is the deliberate replacement. Every name here is a symbol a hyphenated
script actually calls (17 of them, measured by grepping `driver.` out of
redeploy-competition.py), imported from the module that owns it rather than through
another re-export. Keep it exactly that: add a name only when a consumer needs it. An
unused re-export here is invisible again by construction, which is the bug we removed.
"""

# Owned by constants.
from constants import PER_MACHINE_NAKON_BUDGET

# Owned by config_ops.
from config_ops import list_proxmox_templates

# Owned by domain_ops.
from domain_ops import deploy_domain_configs

# Owned by engine_ops.
from engine_ops import ensure_nat_forwarding

# Owned by golden_ops.
from golden_ops import unbooted_golden_boxes

# Owned by hardening_ops.
from hardening_ops import fix_dns_on_boxes, fix_services_on_boxes, setup_ubuntu_auth

# Owned by nakon_ops.
from nakon_ops import (build_nakon_bundle, generate_nakon_config, generate_stage_configs,
                       os_to_platform, run_nakon)

# Owned by ssh_ops.
from ssh_ops import read_terraform_ctx, wait_for_boxes_ssh, wait_for_cloud_init

# Owned by utils.
from utils import env_summary

# Owned by windows_ops.
from windows_ops import bootstrap_windows_box

# The names above are re-exports, not private imports: pyflakes otherwise reports every
# one of them as unused, which is exactly the noise that made the old shim's real
# surface unreadable. __all__ is both the suppression and the documented contract.
__all__ = [
    "PER_MACHINE_NAKON_BUDGET",
    "bootstrap_windows_box",
    "build_nakon_bundle",
    "deploy_domain_configs",
    "ensure_nat_forwarding",
    "env_summary",
    "fix_dns_on_boxes",
    "fix_services_on_boxes",
    "generate_nakon_config",
    "generate_stage_configs",
    "list_proxmox_templates",
    "os_to_platform",
    "read_terraform_ctx",
    "run_nakon",
    "setup_ubuntu_auth",
    "unbooted_golden_boxes",
    "wait_for_boxes_ssh",
    "wait_for_cloud_init",
]
