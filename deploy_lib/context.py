"""DeployContext: the one object the eight phases share (built once by prepare())."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from common.constants import SCORING_ENGINE_VMID, ownership_tags
from estate import state as estate_state


def persist_state(ctx):
# Write the competition's monolithic state document.
#
#     The file holds the only copy of the generated box and team passwords: a torn write
#     here bricks both resume and redeploy. `absorb` routes the flat keys the phases use
#     into their sections and the estate writer is the shared os.replace +
#     0600-at-creation one. The module-level seam is deliberate: tests stub persistence
#     through it (tests/_deploy_patch.dpatch) without touching a real competition dir.
    document = ctx.document()
    # A context with no comp_dir has nowhere to persist (partial contexts in tests).
    if document is None:
        return
    document.absorb(ctx.state)
    # `placement` is not part of the flat state: the planner writes it straight to the file
    # when the placement resolves, and the plant-time red01 selection updates it there, while
    # this context has held its own copy of the document since load_prior_deploy_state. Saving
    # that copy back therefore dropped the record — state documents written before this gate
    # read `placement: None` even though the run's own log said "Placement recorded in <that
    # file>". The record is what makes red01's selected address durable ("the recorded value is
    # never re-derived"), so the on-disk one wins over the stale in-memory one.
    placement = estate_state.EstateState.load(ctx.comp_dir).data.get("placement")
    if placement is not None:
        document.data["placement"] = placement
    document.save()

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
#     environment -> generated configs and paths -> targets -> the engine/SSH fields
#     phase 2 fills in (a fresh run only has engine coordinates after the engine is created; a resume
#     finds them already there).

    # --- run identity + inputs ---
    comp_dir: Optional[Path] = None
    comp_name: str = ""
    state_path: Optional[Path] = None
    # The monolithic per-competition state document; `state` above is its flat view.
    estate: object = None
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
    # --- generated configs + the per-competition state document ---
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
    # --- enumerated targets ---
    all_targets: list = field(default_factory=list)
    managed_targets: list = field(default_factory=list)
    linux_targets: list = field(default_factory=list)
    windows_targets: list = field(default_factory=list)
    # --- filled in after the engine is created (phase 2) ---
    tf_ctx: dict = field(default_factory=dict)
    scoring_user: str = ""
    scoring_ip: str = ""
    ssh_key: Optional[Path] = None
    # --- run identity (constants.ownership_tags) ---
    # run_id: stamped on everything this run creates. reclaim_run_id: the PRIOR
    # state's run id — the only id that authorizes destroying a pre-existing VM in the
    # rebuild gates and in phase 1's interrupted-clone check. They differ only on a
    # fresh deploy with no prior state; a deploy over prior state always reuses its id.
    run_id: str = ""
    reclaim_run_id: str = ""

    def document(self):
        # A context built outside prepare() (phase/sequencer unit tests, recovery paths)
        # owns no document yet: bind it on first use rather than requiring a caller to.
        # No comp_dir means nowhere to persist, so there is no document to bind.
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
    # The tags every VM CREATED THIS RUN carries (constants.ownership_tags):
    #         comp tag + this run's run-id tag.
        return ownership_tags(self.comp_name, self.run_id)

    @property
    def reclaim_tags(self):
    # The tags that prove ownership of a PRE-EXISTING VM (the rebuild gates,
    #         phase 1's marker check): the prior run's full ownership set. A same-comp VM
    #         without this tag
    #         belongs to a DIFFERENT worktree's run and is refused, not reclaimed
    #         (a near-miss).
        return ownership_tags(self.comp_name, self.reclaim_run_id or self.run_id)

    @property
    def reclaim_tag(self):
    # The run-id tag alone, for callers that need only the id.
        return self.reclaim_run_id or self.run_id
