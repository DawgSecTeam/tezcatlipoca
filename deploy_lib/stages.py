"""The staged records prepare() fills in, one dataclass per step.

Each prepare() step owns one of these and fills it in, so the sequencer in prepare.py reads
as the ordered steps it is; assemble_deploy_context() (prepare.py) is the single place that
flattens them onto DeployContext (context.py), whose field order follows the pipeline.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from constants import SCORING_ENGINE_VMID


@dataclass
class RunIdentity:
    """The run's engine identity, settled before anything else keys on it.

    prepare() resolves the scoring-engine VMID first because the engine lock is keyed on
    it and phase-1 cleanup destroys by it. On a resume it is authoritative from
    .deploy_state.json; on a fresh deploy it comes from the --scoring-vmid flag/arg
    (default SCORING_ENGINE_VMID)."""

    engine_vmid: int = SCORING_ENGINE_VMID


@dataclass
class CompetitionSpec:
    """The competition's static definition: Compfile + users.json + boxes.json.

    nakon_jobs and apt_cache are Compfile knobs materialised here so every step that
    consumes them (the nakon config, the golden-hash inputs) reads the spec instead of
    re-parsing the file."""

    name: str = ""
    scenario: str = ""
    difficulty: int = 0
    box_username: str = ""
    credlist_usernames: list = field(default_factory=list)
    nakon_jobs: int = 4
    apt_cache: bool = True
    comp_name: str = ""
    boxes: list = field(default_factory=list)


@dataclass
class PriorDeployState:
    """What the previous run left in .deploy_state.json, plus the resume decision.

    previous_state is the raw dict the secrets step reuses (box_password, the packet
    credentials' fallback); resuming is from_phase > 1. The file is read BEFORE the
    guard that refuses a resume without one, because that guard's message names the
    path it looked for. The pipeline-version refusal also lives here: it must fire before
    anything resumes."""

    state_path: Optional[Path] = None
    previous_state: dict = field(default_factory=dict)
    resuming: bool = False


@dataclass
class CompetitionInputs:
    """Per-competition inputs loaded after the resume guards: injects and the packet.

    The packet-published credentials (compile-packet.py -> passwords.json) are the one
    input that outranks both state and a fresh mint, so they are materialised here as a
    unit; packet_credlists is the derived view the secrets step reads."""

    injects: list = field(default_factory=list)
    packet_pw: Optional[dict] = None
    packet_credlists: dict = field(default_factory=dict)


@dataclass
class CompetitionSecrets:
    """Every secret the run needs, minted or reused once by the secrets step.

    state is the .deploy_state.json dict itself (mutated as the run progresses and
    checkpointed by DeployContext); the rest are its decoded pieces. A resume fills them
    from state, a fresh deploy mints them, and the packet/carried-password overrides are
    applied before state is written (never after a golden consumes box_password)."""

    state: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)
    number_of_teams: int = 0
    admin_password: str = ""
    # A SECOND admin account, for automation only — see build_event_conf. Quotient
    # allows one session per ACCOUNT, so a watchdog that logs in as `scoring` cannot
    # evict the operator's/harness's `admin` cookie.
    scoring_password: str = ""
    postgres_password: str = ""
    redis_password: str = ""
    box_password: str = ""
    box_creds: dict = field(default_factory=dict)
    domain_creds: Optional[dict] = None
    inject_password: Optional[str] = None
    # Per-deploy identity tag (utils.mint_run_id): stamped on every VM this run
    # creates and required by every destruction guard. Minted once per competition
    # directory — a resume, a crash-loop re-run and a redeploy all reuse it, so
    # kept templates keep matching the ownership set.
    run_id: str = ""


@dataclass
class EnginePlacement:
    """Where the competition's VMs live (None = single-node), resolved once.

    Resolving it is also when the endpoint-keyed engine lock is taken, so `placement` is
    read by every later node-scoped step and by terraform's satellite inputs."""

    placement: Optional[dict] = None


@dataclass
class GeneratedConfigs:
    """The generated stage configs and the golden hash/gate results.

    unbooted is the domain-controller set the stage configs key the repair stage on;
    golden_inputs/golden_hashes feed phase 1's waves and the phase-4 rebuild gate;
    frozen_keep names the goldens a code-only freeze must not rebuild."""

    unbooted: set = field(default_factory=set)
    nakon_config_path: Optional[Path] = None
    golden_config_path: Optional[Path] = None
    repair_config_path: Optional[Path] = None
    final_config_path: Optional[Path] = None
    golden_inputs: dict = field(default_factory=dict)
    golden_hashes: dict = field(default_factory=dict)
    frozen_keep: set = field(default_factory=set)


@dataclass
class TerraformInputs:
    """The per-competition terraform workdir and its authoritative tfvars.

    terraform.tfvars.json outranks TF_VAR_* env, so `tfvars` is the copy terraform
    actually reads; engine_mgmt_ip is both a tfvars value and the exported
    TF_VAR_engine_mgmt_ip that template_ops builds the engine VM with (build-VM
    ipconfig0). The ssh key path is absolute because it was ../proxmox-relative to the
    now-deeper per-competition workdir."""

    tf_dir: Optional[Path] = None
    tfvars_path: Optional[Path] = None
    tfvars: dict = field(default_factory=dict)
    ssh_key_abs: str = ""
    engine_mgmt_ip: Optional[str] = None


@dataclass
class DeployTargets:
    """Every VM this deploy touches, split into the work lists the phases consume.

    node is the engine's host (read from the env after placement selected it);
    all_targets keeps positional vmids for every box including unmanaged ones, while
    the Linux/Windows lists are what plant/repair/fix_services iterate."""

    node: str = ""
    all_targets: list = field(default_factory=list)
    managed_targets: list = field(default_factory=list)
    linux_targets: list = field(default_factory=list)
    windows_targets: list = field(default_factory=list)
