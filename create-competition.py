# Shim — actual implementation now in focused modules.
# Keeps `import create-competition` via importlib working for redeploy.
# All logic lives in deploy.py / nakon_ops.py / etc.; this file only re-exports.

from config_ops import (
    _prompt_difficulty,
    _prompt_int,
    _prompt_optional_int,
    collect_boxes,
    collect_teams,
    collect_users_config,
    confirm_deploy,
    destroy_bridge_if_exists,
    list_proxmox_templates,
    load_boxes,
    load_injects,
    load_previous_competitions,
    random_password,
    resolve_inject_times,
    update_env,
)
from constants import (
    DISRUPTIVE_CONFIGS,
    MAX_BOXES_PER_TEAM,
    MAX_TEAMS,
    NAKON_DIR,
    PER_MACHINE_NAKON_BUDGET,
    REBOOTS_BOX_CONFIGS,
    SCORING_ENGINE_VMID,
    SLOW_SERVICES,
    SNAP_BASE,
    SNAP_READY,
    WINDOWS_ADMIN_USER,
)
from clone_ops import clone_team_boxes
from deploy import deploy, main
from domain_ops import deploy_domain_configs
from engine_ops import bootstrap_scoring_engine, ensure_nat_forwarding, install_range_healthcheck, push_event_conf
from hardening_ops import fix_dns_on_boxes, fix_services_on_boxes, setup_ubuntu_auth
from nakon_ops import _nakon_randomize, _run_single_nakon_config, build_nakon_bundle, generate_nakon_config, is_windows_template, os_to_platform, run_nakon
from range_ops import (
    destroy_vm_if_exists,
    diagnose_unreachable_box,
    enumerate_targets,
    guest_agent_exec_root,
    guest_agent_exec_windows,
    proxmox_api,
    stop_vm,
    take_snapshot,
    vm_id_for,
    wait_for_guest_agent,
    wait_for_proxmox_task,
)
from ssh_ops import (
    is_windows_template as _ssh_is_windows_template,
    read_terraform_ctx,
    ssh_on_gateway,
    ssh_to_engine,
    ssh_via_gateway,
    wait_for_boxes_ssh,
    wait_for_cloud_init,
    wait_for_http,
    wait_for_ssh,
)
from utils import BOX_USERNAME_DEFAULT, DNS_FIX_CMD, load_compfile, load_users_config, pick_competition
from windows_ops import bootstrap_windows_box, dns_repoint_windows_box, is_windows_template as win_is_windows_template, wait_for_dc_dns, wait_for_windows_sshd

# Re-export is_windows_template (all three definitions agree: "win" in lower)
# Ensure driver.is_windows_template resolves (used by redeploy box_platform).
# windows_ops / ssh_ops / nakon_ops each define it; expose one.
# The imported `is_windows_template` from nakon_ops is already in scope as is_windows_template.
# Also ensure bootstrap_windows_box etc. are at module scope for driver.* access.

# For backwards compat, expose ENV_PATH like the original did (Path(".env"))
from pathlib import Path
ENV_PATH = Path(".env")

if __name__ == "__main__":
    main()
