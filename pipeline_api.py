"""Stable import surface for the hyphenated entry-point scripts."""

from common.constants import PER_MACHINE_NAKON_BUDGET
from guests.domain_ops import deploy_domain_configs
from engine.engine_health_ops import ensure_nat_forwarding
from golden.golden_target_ops import unbooted_golden_boxes
from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
from guests.service_fixup_ops import fix_services_on_boxes
from guests.ubuntu_auth_ops import setup_ubuntu_auth
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_config_ops import generate_stage_configs, os_to_platform
from nakonops.nakon_run_ops import run_nakon
from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
from estate.engine_setup import engine_ctx
from infra.lifecycle import take_snapshot
from common.utils import env_summary
from common.windows_ops import bootstrap_windows_box

# Re-exports, not unused imports: __all__ suppresses pyflakes and is the contract.
__all__ = [
    "PER_MACHINE_NAKON_BUDGET",
    "bootstrap_windows_box",
    "build_nakon_bundle",
    "deploy_domain_configs",
    "ensure_nat_forwarding",
    "env_summary",
    "fix_dns_on_boxes",
    "prep_apt_on_boxes",
    "take_snapshot",
    "fix_services_on_boxes",
    "generate_stage_configs",
    "os_to_platform",
    "engine_ctx",
    "run_nakon",
    "setup_ubuntu_auth",
    "unbooted_golden_boxes",
    "wait_for_boxes_ssh",
    "wait_for_cloud_init",
]
