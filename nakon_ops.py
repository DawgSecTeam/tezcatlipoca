"""Facade: nakon config generation, bundle building and deployment via the scoring engine.
The code lives in nakon_pin_ops, nakon_lock_ops, nakon_bundle_ops, nakon_config_ops and
nakon_run_ops; every name stays importable from here (is_windows_template is re-exported too)."""

from nakon_pin_ops import (  # noqa: F401
    _config_name,
    _pin_key,
    _cross_box_ip,
    _is_identity_kind,
    _identity_banned_configs,
    _fill_identity_vars,
    _validate_pin_vars,
    _validate_known_broken_pins,
    _bare_unplantable_reason,
    _drop_unplantable_bare,
)
from nakon_lock_ops import (  # noqa: F401
    _ENGINE_LOCKS,
    acquire_engine_lock,
    release_engine_lock,
    held_lock_paths,
    other_deploys_in_flight,
)
from nakon_bundle_ops import (  # noqa: F401
    _BUNDLE_BUILD_LOCK,
    _SHELL_VAR_WHITELIST,
    _BASH_DEFAULT_RE,
    _BASH_VAR_RE,
    build_nakon_bundle,
    _lint_bundle_vars,
)
from nakon_config_ops import (  # noqa: F401
    os_to_platform,
    _nakon_randomize,
    generate_nakon_config,
    _golden_stage_machines,
    generate_slot_golden_config,
    generate_stage_configs,
)
from nakon_run_ops import (  # noqa: F401
    NakonResult,
    _deploy_owner_check,
    _nothing_answered,
    _reconcile_zero_step_machines,
    run_nakon,
    _run_single_nakon_config,
)
from constants import REQUIRED_VARS  # noqa: F401
from windows_ops import is_windows_template  # noqa: F401

__all__ = [
    'REQUIRED_VARS',
    'is_windows_template',
    '_config_name',
    '_pin_key',
    '_cross_box_ip',
    '_is_identity_kind',
    '_identity_banned_configs',
    '_fill_identity_vars',
    '_validate_pin_vars',
    '_validate_known_broken_pins',
    '_bare_unplantable_reason',
    '_drop_unplantable_bare',
    '_ENGINE_LOCKS',
    'acquire_engine_lock',
    'release_engine_lock',
    'held_lock_paths',
    'other_deploys_in_flight',
    '_BUNDLE_BUILD_LOCK',
    '_SHELL_VAR_WHITELIST',
    '_BASH_DEFAULT_RE',
    '_BASH_VAR_RE',
    'build_nakon_bundle',
    '_lint_bundle_vars',
    'os_to_platform',
    '_nakon_randomize',
    'generate_nakon_config',
    '_golden_stage_machines',
    'generate_slot_golden_config',
    'generate_stage_configs',
    'NakonResult',
    '_deploy_owner_check',
    '_nothing_answered',
    '_reconcile_zero_step_machines',
    'run_nakon',
    '_run_single_nakon_config',
]
