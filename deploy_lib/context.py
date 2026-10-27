"""DeployContext: the one object the eight phases share (built once by prepare())."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from common.constants import SCORING_ENGINE_VMID, ownership_tags
from estate import state as estate_state


def persist_state(ctx):
    # Holds the only copy of the generated passwords: a torn write bricks resume
    # and redeploy. The module-level seam is deliberate — tests stub persistence through it.
    if ctx.comp_dir is None:
        return
    # Always reload: `templates`, `roster`, `placement`, `red` and the resource inventory are
    # written straight to the file by their owning modules, and a cached document here would
    # overwrite them with a stale copy (the lost-update bug this replaced).
    document = estate_state.EstateState.load(ctx.comp_dir)
    document.absorb(ctx.state)
    document.save()


@dataclass
class DeployContext:
    comp_dir: Optional[Path] = None
    comp_name: str = ""
    state_path: Optional[Path] = None
    from_phase: int = 1
    resuming: bool = False
    assume_yes: bool = False
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
    previous_state: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    teams: dict = field(default_factory=dict)
    number_of_teams: int = 0
    admin_password: str = ""
    # second admin for automation only: Quotient allows one session per ACCOUNT, so a
    # scoring watchdog can't evict the operator's admin cookie (see build_event_conf)
    scoring_password: str = ""
    postgres_password: str = ""
    redis_password: str = ""
    box_password: str = ""
    box_creds: dict = field(default_factory=dict)
    domain_creds: Optional[dict] = None
    inject_password: Optional[str] = None
    placement: Optional[dict] = None
    node: str = ""
    engine_vmid: int = SCORING_ENGINE_VMID
    engine_mgmt_ip: Optional[str] = None
    boxes: list = field(default_factory=list)
    boxes_by_name: dict = field(default_factory=dict)
    unbooted: set = field(default_factory=set)
    nakon_config_path: Optional[Path] = None
    golden_config_path: Optional[Path] = None
    predomain_config_path: Optional[Path] = None
    final_config_path: Optional[Path] = None
    late_config_path: Optional[Path] = None
    golden_inputs: dict = field(default_factory=dict)
    # MAC of each engine NIC after net0, in create order: the netplan pins interfaces by it.
    engine_nic_macs: list = field(default_factory=list)
    golden_hashes: dict = field(default_factory=dict)
    ssh_key_abs: str = ""
    all_targets: list = field(default_factory=list)
    managed_targets: list = field(default_factory=list)
    linux_targets: list = field(default_factory=list)
    windows_targets: list = field(default_factory=list)
    tf_ctx: dict = field(default_factory=dict)
    scoring_user: str = ""
    scoring_ip: str = ""
    ssh_key: Optional[Path] = None
    # run_id is stamped on everything this run creates; reclaim_run_id (the PRIOR state's
    # id) is the only id authorized to destroy a pre-existing VM. A resume reuses the prior id.
    run_id: str = ""
    reclaim_run_id: str = ""

    def document(self):
        """A fresh handle on the state document: load, mutate, save. Never cached."""
        if self.comp_dir is None:
            return None
        self.state_path = estate_state.state_path(self.comp_dir)
        return estate_state.EstateState.load(self.comp_dir)

    def save_state(self):
        persist_state(self)

    def checkpoint(self, n):
        # Record phase n as completed — the resume guard's only source of truth.
        self.state["last_phase"] = n
        self.save_state()

    @property
    def comp_tags(self):
        # tags every VM CREATED THIS RUN carries: comp tag + this run's run_id
        return ownership_tags(self.comp_name, self.run_id)

    @property
    def reclaim_tags(self):
        # proves ownership of a PRE-EXISTING VM (rebuild gates, phase 1 marker check): a
        # same-comp VM lacking the prior run's tag belongs to another worktree and is refused
        return ownership_tags(self.comp_name, self.reclaim_run_id or self.run_id)

    @property
    def reclaim_tag(self):
        return self.reclaim_run_id or self.run_id
