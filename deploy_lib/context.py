"""DeployContext: the one object the eight phases share (built once by prepare())."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from common.constants import SCORING_ENGINE_VMID, ownership_tags
from estate import state as estate_state


def persist_state(ctx):
    # Holds the only copy of the generated passwords: a torn write bricks resume
    # and redeploy. The module-level seam is deliberate — tests stub persistence through it.
    document = ctx.document()
    # no comp_dir -> nowhere to persist (partial contexts in tests)
    if document is None:
        return
    document.absorb(ctx.state)
    # placement bypasses the flat state (the planner and the plant-time red01 selection write
    # it to the file directly): on-disk wins, re-deriving a deployed red01 address is forbidden.
    placement = estate_state.EstateState.load(ctx.comp_dir).data.get("placement")
    if placement is not None:
        document.data["placement"] = placement
    # Same lost-update hazard for red01: record_identity() writes the `red` section after
    # this context loaded its document copy, and the final save dropped it — teardown then
    # said "this run recorded no red01" and only the tag sweep removed the VM.
    red = estate_state.EstateState.load(ctx.comp_dir).data.get("red")
    if red:
        document.data["red"] = red
    document.save()

@dataclass
class DeployContext:
    comp_dir: Optional[Path] = None
    comp_name: str = ""
    state_path: Optional[Path] = None
    # the state document; `state` below is its flat view
    estate: object = None
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
        # contexts built outside prepare() bind their document on first use; no comp_dir -> none
        if self.estate is None:
            if self.comp_dir is None:
                return None
            self.estate = estate_state.EstateState.load(self.comp_dir)
            self.state_path = estate_state.state_path(self.comp_dir)
        return self.estate

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
