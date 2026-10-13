"""DeployContext: the one object the eight phases share (built once by prepare())."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config.config_ops import write_state
from common.constants import SCORING_ENGINE_VMID, ownership_tags


@dataclass
class DeployContext:
# Everything the eight deploy phases share, built once by prepare().
#
#     Before this existed the phases were `if` blocks inside a 940-line deploy() reading
#     ~160 locals, which made every phase boundary invisible and every phase untestable.
#     The context is deliberately a data holder: its only behaviour is the state-file pair
#     every phase needs (save_state/checkpoint) plus comp_tags, so a phase function is
#     ordinary code that takes this object.
#
#     Field order follows the pipeline: identity/config -> secrets/state -> placement and
#     environment -> generated configs and paths -> targets -> the terraform/SSH fields
#     phase 2 fills in (a fresh run only has terraform outputs after apply #1; a resume
#     finds them already there).

    # --- run identity + inputs ---
    comp_dir: Optional[Path] = None
    comp_name: str = ""
    state_path: Optional[Path] = None
    from_phase: int = 1
    resuming: bool = False
    assume_yes: bool = False
    # --- competition config (Compfile/users.json) ---
    name: str = ""
    scenario: str = ""
    difficulty: int = 0
    box_username: str = ""
    credlist_usernames: list = field(default_factory=list)
    nakon_jobs: int = 4
    apt_cache: bool = True
    injects: list = field(default_factory=list)
    packet_pw: Optional[dict] = None
    packet_credlists: dict = field(default_factory=dict)
    # --- secrets + persisted state ---
    previous_state: dict = field(default_factory=dict)
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
    # --- multi-node placement + Proxmox environment ---
    placement: Optional[dict] = None
    node: str = ""
    engine_vmid: int = SCORING_ENGINE_VMID
    engine_mgmt_ip: Optional[str] = None
    # --- generated configs + terraform workdir ---
    boxes: list = field(default_factory=list)
    boxes_by_name: dict = field(default_factory=dict)
    unbooted: set = field(default_factory=set)
    nakon_config_path: Optional[Path] = None
    golden_config_path: Optional[Path] = None
    repair_config_path: Optional[Path] = None
    final_config_path: Optional[Path] = None
    golden_inputs: dict = field(default_factory=dict)
    golden_hashes: dict = field(default_factory=dict)
    frozen_keep: set = field(default_factory=set)
    tf_dir: Optional[Path] = None
    tfvars_path: Optional[Path] = None
    tfvars: dict = field(default_factory=dict)
    ssh_key_abs: str = ""
    # --- enumerated targets ---
    all_targets: list = field(default_factory=list)
    managed_targets: list = field(default_factory=list)
    linux_targets: list = field(default_factory=list)
    windows_targets: list = field(default_factory=list)
    # --- filled in after terraform apply #1 (phase 2) ---
    tf_ctx: dict = field(default_factory=dict)
    scoring_user: str = ""
    scoring_ip: str = ""
    ssh_key: Optional[Path] = None
    # --- run identity (constants.ownership_tags) ---
    # run_id: stamped on everything this run creates. reclaim_run_id: the PRIOR
    # state's run id — the only id that authorizes destroying a pre-existing VM in
    # phase 1 / the rebuild gates. They differ only on a fresh deploy with no prior
    # state; a deploy over prior state always reuses its id.
    run_id: str = ""
    reclaim_run_id: str = ""

    def save_state(self):
    # Persist .deploy_state.json through the shared config_ops writer.
        # .deploy_state.json holds the only copy of the generated box and team
        # passwords: a torn write here bricks both resume and redeploy, and the old
        # write_text-then-chmod idiom also left the secret briefly world-readable.
        # config_ops.write_state is the shared os.replace + 0600-at-creation writer
        # (added 2026-10-01 alongside redeploy's two copies and the post-DB-wipe
        # recovery path) — one implementation, not four.
        write_state(self.state_path, self.state)

    def checkpoint(self, n):
        # Record phase n as completed — the resume guard's only source of truth.
        self.state["last_phase"] = n
        self.save_state()

    @property
    def comp_tags(self):
    # The tags every VM CREATED THIS RUN carries (constants.ownership_tags):
    #         comp tag + this run's run-id tag.
        return ownership_tags(self.comp_name, self.run_id)

    @property
    def reclaim_tags(self):
    # The tags that authorize destroying a PRE-EXISTING VM (phase 1, rebuild
    #         gates): the prior run's full ownership set. A same-comp VM without this tag
    #         belongs to a DIFFERENT worktree's run and is refused, not reclaimed
    #         (2026-10-02 near-miss).
        return ownership_tags(self.comp_name, self.reclaim_run_id or self.run_id)

    @property
    def reclaim_tag(self):
    # The run-id tag alone that phase-1-style reclamation requires.
        return self.reclaim_run_id or self.run_id
