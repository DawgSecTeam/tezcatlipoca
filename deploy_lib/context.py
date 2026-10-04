"""DeployContext: the one object the eight phases share (built once by prepare())."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config_ops import write_state
from constants import ownership_tags
from deploy_lib.gates import CHECKPOINT_GATES, guard_resume_existence


@dataclass
class DeployContext:
    """Everything the eight deploy phases share, built once by prepare().

    Before this existed the phases were `if` blocks inside a 940-line deploy() reading
    ~160 locals, which made every phase boundary invisible and every phase untestable.
    The context is deliberately a data holder: its only behaviour is the state-file pair
    every phase needs (save_state/checkpoint) plus comp_tags, so a phase function is
    ordinary code that takes this object.

    Field order follows the pipeline: identity/config -> secrets/state -> placement and
    environment -> generated configs and paths -> targets -> the terraform/SSH fields
    phase 2 fills in (a fresh run only has terraform outputs after apply #1; a resume
    finds them already there)."""

    # --- run identity + inputs ---
    comp_dir: Path
    comp_name: str
    state_path: Path
    from_phase: int
    resuming: bool
    assume_yes: bool
    # --- competition config (Compfile/users.json) ---
    name: str
    scenario: str
    box_username: str
    credlist_usernames: list
    nakon_jobs: int
    apt_cache: bool
    injects: list
    packet_pw: Optional[dict]
    # --- secrets + persisted state ---
    state: dict
    teams: dict
    number_of_teams: int
    admin_password: str
    scoring_password: str
    postgres_password: str
    redis_password: str
    box_password: str
    box_creds: dict
    domain_creds: Optional[dict]
    inject_password: Optional[str]
    # --- multi-node placement + Proxmox environment ---
    placement: Optional[dict]
    node: str
    engine_vmid: int
    engine_mgmt_ip: str
    # --- generated configs + terraform workdir ---
    boxes: list
    boxes_by_name: dict
    unbooted: set
    nakon_config_path: Path
    golden_config_path: Path
    repair_config_path: Path
    final_config_path: Path
    golden_inputs: dict
    golden_hashes: dict
    frozen_keep: set
    tf_dir: Path
    tfvars_path: Path
    tfvars: dict
    ssh_key_abs: str
    # --- enumerated targets ---
    all_targets: list
    managed_targets: list
    linux_targets: list
    windows_targets: list
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
        """Persist .deploy_state.json through the shared config_ops writer."""
        # .deploy_state.json holds the only copy of the generated box and team
        # passwords: a torn write here bricks both resume and redeploy, and the old
        # write_text-then-chmod idiom also left the secret briefly world-readable.
        # config_ops.write_state is the shared os.replace + 0600-at-creation writer
        # (added 2026-10-01 alongside redeploy's two copies and the post-DB-wipe
        # recovery path) — one implementation, not four.
        write_state(self.state_path, self.state)

    def checkpoint(self, n):
        """Record phase n as completed — the resume guard's only source of truth.

        Which is exactly why a checkpoint must not be written on the strength of a phase
        merely finishing. In the scale8 soak a strict=False repair sweep "completed"
        against 32 nonexistent machines and stamped last_phase=5; the next resume then
        read that stamp as proof the machines existed. So the phases that produce
        infrastructure verify their own output before claiming it — the same checks a
        resume would run (guard_resume_existence), which is defense in depth for the run
        after this one rather than a substitute for the gate."""
        self._verify_phase_artifacts(n)
        self.state["last_phase"] = n
        self.save_state()

    def _verify_phase_artifacts(self, n):
        """Phase n's own existence gate: refuse to stamp a claim the range cannot back.

        Only the phases that build infrastructure are gated, using the same checks a
        resume would run — "if I resume from n+1, is that resume honest?". A phase with
        nothing independently checkable (cleanup, template build, sweeps, seeding) is
        not gated, because inventing a proxy check for it would refuse good deploys.

        --force-from-phase deliberately does NOT bypass this: that flag is a statement
        about skipping work, not a licence to record work that never happened."""
        if n not in CHECKPOINT_GATES:
            return
        comp_dir = self.comp_dir
        try:
            guard_resume_existence(n + 1, comp_dir, self.state)
        except SystemExit as e:
            raise SystemExit(
                f"  ERROR: phase {n} reported success but its output does not exist, so "
                f"last_phase={n} will NOT be recorded:\n{e}\n"
                f"        A checkpoint that claims work never done is what sent the scale8 "
                f"soak's later phases chasing machines that were never built."
            )

    @property
    def comp_tags(self):
        """The tags every VM CREATED THIS RUN carries (constants.ownership_tags):
        comp tag + this run's run-id tag."""
        return ownership_tags(self.comp_name, self.run_id)

    @property
    def reclaim_tags(self):
        """The tags that authorize destroying a PRE-EXISTING VM (phase 1, rebuild
        gates): the prior run's full ownership set. A same-comp VM without this tag
        belongs to a DIFFERENT worktree's run and is refused, not reclaimed
        (2026-10-02 near-miss)."""
        return ownership_tags(self.comp_name, self.reclaim_run_id or self.run_id)

    @property
    def reclaim_tag(self):
        """The run-id tag alone that phase-1-style reclamation requires."""
        return self.reclaim_run_id or self.run_id
