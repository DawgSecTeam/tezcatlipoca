"""Orchestrator: seven-phase deploy and CLI."""

import fcntl
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import urllib3
from dotenv import load_dotenv

from config_ops import (
    _prompt_difficulty,
    collect_boxes,
    collect_teams,
    collect_users_config,
    confirm_deploy,
    load_boxes,
    load_injects,
    load_packet_passwords,
    load_previous_competitions,
    preflight_gates,
    random_password,
    update_env,
    write_state,
    write_text_atomic,
)
from constants import (
    DEFAULT_ENGINE_MGMT_GW,
    DEFAULT_ENGINE_MGMT_IP,
    GOLDEN_TAG,
    MAX_BOXES_PER_TEAM,
    MAX_TEAMS,
    MIN_LOAD_FREE_TIMEOUT,
    RESUME_ATTEMPT_LIMIT,
    SCORING_ENGINE_VMID,
    ownership_tags,
)
from golden_ops import (_is_template, _template_vmid_map, build_golden_set,
                        unbooted_golden_boxes)
from hardening_ops import _APT_PREP_BODY, _apt_prep_script
from nakon_ops import (acquire_engine_lock, build_nakon_bundle, generate_nakon_config,
                       generate_stage_configs)
from nodes_ops import (activate_placement, golden_vmid_for_slot, resolve_placement,
                       satellite_routes_for, satellite_tfvars)
from range_ops import (destroy_vm_if_exists, ensure_terraform_workdir, enumerate_targets,
                       persist_targets, wait_for_node_load)
from template_ops import (
    code_hash,
    engine_template_vmid,
    frozen_code_drift,
    frozen_gate,
    frozen_state,
    golden_freeze_gate,
    golden_hash_inputs,
    golden_payload_hash,
    hash_from_inputs,
    stored_template_hash,
)
from utils import (compfile_flag, is_unmanaged, load_compfile, load_users_config,
                   mint_run_id, valid_comp_name)
from windows_ops import is_windows_template

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_DEPLOY_LOCKS = {}  # path -> open fh (keep referenced so flock survives)

# Code-level, not run-level state: the .deploy_state.json resume guard and the fresh
# state writer must record the same version, and both now live in different steps.
PIPELINE_VERSION = 2


def _record_coverage(state, stage_machines, result):
    """Record per-machine unplanted configs in state (M4 plant-coverage source).

    The expectation side is implicit — nakon-config.json is verify's source of truth.
    This records only failures: machine -> [config names whose step reported rc != 0],
    or every config when a machine died before reporting any step. The golden plant is
    strict (a failure aborts the deploy), so only the lenient post-clone passes record
    here; verify maps '{box}-golden'-style failures onto every team copy of the box.

    A machine whose stage reported NO failed steps drops its stale entry: a recovered
    config (replanted by a later sweep) must not keep the coverage gate red forever
    (amongus-cde-2026 2026-09-30: SMB v1 stayed 'failed' across three green replants)."""
    if result is None or not getattr(result, "machines", None):
        return  # no --json outcome (older nakon) — coverage falls back to the tally
    failed = result.failed_configs()
    cov = state.setdefault("plant_coverage_failed", {})
    if not failed:
        for m in stage_machines:
            cov.pop(m["name"], None)
        return
    for m in stage_machines:
        bad = failed.get(m["name"])
        if not bad:
            cov.pop(m["name"], None)
            continue
        if bad == {"<machine failed before any step>"}:
            bad = {(c if isinstance(c, str) else c["name"]) for c in m["configurations"]}
        cov[m["name"]] = sorted(set(cov.get(m["name"]) or []) | set(bad))


def record_stage_coverage(state, stage_machines, result, save_state):
    """Record one post-clone stage's coverage AND persist it, unconditionally.

    _record_coverage's stale-entry clearing only counts once it reaches disk. Phase 6
    used to guard its save on the nakon tally (`if state["nakon_failed_steps"]`), so a
    final pass that was fully green yet popped a stale entry lost the pop at process
    exit and verify's coverage gate stayed red — re-creating exactly the bug ff9b19f
    fixed (amongus-cde-2026 2026-09-30: "SMB v1 stayed 'failed' across three green
    replants"). The window is narrow but real: phase-5 tally empty, final pass fully
    green, and the stale entry belongs to a final-only machine (one carrying just
    systemd-system-masked / hosts-redirect-linux, so repair_machines never names it).
    save_state is injected so the always-persist invariant is testable without a real
    state file."""
    _record_coverage(state, stage_machines, result)
    save_state()


def guard_resume_from_phase(from_phase, last_phase, state_path, force=False):
    """Refuse a --from-phase that skips phases .deploy_state.json never saw complete.

    checkpoint() writes `last_phase` after each phase, so the only safe resume target is
    last_phase + 1 (re-run the phase that died) or earlier. `last_phase` was written but
    never read, so `--from-phase 6` on a range whose last checkpoint was 3 passed both
    existing guards (state exists, pipeline_version matches) and then ran the
    domain/final/beacon/tz-ready chain against machines that were never built, burying
    the real failure in confusing downstream errors. A missing/unusable `last_phase`
    (hand-made or pre-checkpoint state) reads as 0 — the conservative choice.
    --force-from-phase is the escape hatch for an operator who knows the checkpoint is
    stale (e.g. the process died after a phase finished but before its checkpoint
    landed); the default must stay refusing."""
    if force:
        return
    last = last_phase if isinstance(last_phase, int) and last_phase >= 0 else 0
    if from_phase <= last + 1:
        return
    raise SystemExit(
        f"  ERROR: --from-phase {from_phase} skips phases {last + 1}-{from_phase - 1}, but "
        f"{state_path} records phase {last} as the last one that completed. Those phases "
        f"never ran, so the later phases would target machines that do not exist yet. "
        f"Resume at --from-phase {last + 1} (re-runs the phase that died), or pass "
        f"--force-from-phase to skip ahead deliberately."
    )


def failure_signature(exc):
    """A stable identity for "the same failure again", for the resume budget.

    Raw messages differ every run (vmids, uuids, temp paths, elapsed seconds), so a
    naive signature would never match and the guard could never fire. Normalise the
    volatile parts and keep the shape. Erring toward *matching* is deliberate: the
    guard only ever refuses a resume, and --force-from-phase overrides it.
    """
    text = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    text = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<uuid>", text)
    text = re.sub(r"\b[0-9a-fA-F]{12,}\b", "<hex>", text)
    text = re.sub(r"\d+", "N", text)
    return re.sub(r"\s+", " ", text).strip()[:160]


def record_failure(state, phase, exc):
    """Count consecutive failures of the same phase with the same signature.

    Returns the new count. A different signature at the same phase is a *new*
    failure, not a repeat, so the budget resets — that is why the streak is keyed on
    the signature and not just the phase.
    """
    signature = failure_signature(exc)
    previous = state.get("failure_streak") or {}
    repeated = (previous.get("phase") == phase
                and previous.get("signature") == signature)
    count = int(previous.get("count") or 0) + 1 if repeated else 1
    state["failure_streak"] = {"phase": phase, "signature": signature, "count": count}
    return count


def clear_failure_streak(state):
    """Drop the streak after a phase run that made it through."""
    return state.pop("failure_streak", None)


def guard_resume_streak(from_phase, state, limit=RESUME_ATTEMPT_LIMIT, force=False):
    """Refuse the (limit+1)-th consecutive resume of a phase failing the same way.

    `docs/e2e-testing.md` has always said "max 2 repair-resume cycles; a third
    consecutive resume is not a repair, it's a resume-loop", but that lived only in
    prose and cde-2026 burned eleven attempts (deploy6 -> deploy16, 2026-09-29/30)
    on a single Windows golden. This is the enforceable version. It refuses *before*
    any infrastructure work, and names the path that actually helps.
    """
    if force:
        return
    streak = state.get("failure_streak") or {}
    count = int(streak.get("count") or 0)
    if streak.get("phase") != from_phase or count < limit:
        return
    raise SystemExit(
        f"  ERROR: phase {from_phase} has now failed {count} time(s) in a row with the "
        f"same error:\n"
        f"    {streak.get('signature')}\n"
        f"  That is a resume-loop, not a repair (docs/e2e-testing.md: max 2 repair-resume "
        f"cycles). Resuming again would repeat it. Either fix the underlying cause and "
        f"pass --force-from-phase once you have, or tear the range down and redeploy:\n"
        f"    python3 destroy-competition.py <competition>\n"
        f"    python3 create-competition.py --competition <competition> --teams N --yes"
    )


def golden_hash_entries(boxes, base_template_ids, golden_machines_by_box, golden_bundle,
                        box_password, box_username, ssh_public_key, apt_cache, unbooted=()):
    """golden_inputs/golden_hashes for every box in boxes.json — unmanaged included.

    Extracted from deploy() (the pure part of the M4 hash loop) so the all-boxes
    invariant is testable offline: phase 1's wave logic, the phase-4 rebuild gate and
    `.template-hashes.json` all key off these dicts, and all three iterate the FULL
    lineup, not just the planted boxes.

    Every box MUST get an entry. The unmanaged (pfSense/appliance) case is the one that
    used to be unreachable: 8cb755c3 (2026-09-30) added an early `continue` ABOVE
    e91a5039's compensating unmanaged-inputs block, so the block went dead and the slot
    carried no hash. The merge-order accident did not fix the KeyError, it moved it into
    the phase-4 gate (live-found 2026-09-29, cyberfield hdrives-zfs: the first
    pfsense-carried bundle through the M4 hash path). Unmanaged is checked FIRST now so
    the entry exists no matter where a later branch continues from."""
    golden_inputs, golden_hashes = {}, {}
    for b in boxes:
        if is_unmanaged(b):
            # No golden machine, no golden plant: terraform clones it straight from
            # its own base template. The slot still needs a stable hash entry so the
            # rebuild gate and phase 1's wave logic never subscript it into a KeyError.
            inputs = {"config": {"base_template_vmid": base_template_ids.get(b["template"]),
                                 "disk_gb": b.get("disk_gb"), "golden": "unmanaged"},
                      "code": {"build_golden_set": code_hash(build_golden_set)}}
            golden_inputs[b["name"]] = inputs
            golden_hashes[b["name"]] = hash_from_inputs(inputs)
            continue
        if b["name"] in unbooted:
            inputs = {"config": {"base_template_vmid": base_template_ids.get(b["template"]),
                                  "disk_gb": b.get("disk_gb"), "golden": "unbooted"},
                      "code": {"build_golden_set": code_hash(build_golden_set)}}
            golden_inputs[b["name"]] = inputs
            golden_hashes[b["name"]] = hash_from_inputs(inputs)
            continue
        inputs = golden_hash_inputs(
            b, golden_machines_by_box[b["name"]],
            golden_payload_hash(golden_bundle, b["name"]),
            box_password, box_username, ssh_public_key, apt_cache)
        inputs["config"]["base_template_vmid"] = base_template_ids.get(b["template"])
        inputs["code"]["build_golden_set+apt_prep"] = code_hash(
            build_golden_set, _APT_PREP_BODY, _apt_prep_script)
        golden_inputs[b["name"]] = inputs
        golden_hashes[b["name"]] = hash_from_inputs(inputs)
    return golden_inputs, golden_hashes


def golden_rebuild_gate(comp_dir, destroy_node, slot, boxes, engine_vmid, stored,
                        golden_hashes, golden_inputs, comp_name, run_tag=None):
    """Destroy each golden template in `slot` whose stored M4 hash no longer matches.

    A slot with no hash/inputs entry is SKIPPED, not subscripted. The unconditional
    `golden_hashes[b["name"]]` here raised KeyError out of phase 4 for an
    unmanaged-carried lineup (whose box had no entry — see golden_hash_entries) on a
    --from-phase 4 resume or a box flipped to `unmanaged: true` under a live golden,
    the exact case deploy() anticipates when it maps an unmanaged slot to its base
    template (live-found 2026-09-29, cyberfield hdrives-zfs). With no computed hash
    there is nothing to compare the stored one against, so skipping is correct; the
    old code moved the KeyError here instead of fixing it."""
    for i, b in enumerate(boxes):
        vid = golden_vmid_for_slot(engine_vmid, slot, i)
        if not _is_template(destroy_node, vid):
            continue
        box_hash = golden_hashes.get(b["name"])
        box_inputs = golden_inputs.get(b["name"])
        if box_hash is None or box_inputs is None:
            continue
        if stored_template_hash(destroy_node, vid) == box_hash:
            continue
        entry = stored.get("golden", {}).get(b["name"]) or {}
        if frozen_gate(comp_dir, entry.get("inputs"), box_inputs,
                       f"golden template for '{b['name']}'"):
            print(f"  golden-{b['name']} hash differs — rebuilding...")
            destroy_vm_if_exists(destroy_node, vid, expect_tags=ownership_tags(
                comp_name, run_tag, GOLDEN_TAG),
                legacy_name=f"golden-{b['name']}")


def phase1_destroy_waves(node_vms, all_targets, legacy_clones, engine_vmid, boxes, comp_tags,
                         is_template, stored_hashes, golden_hashes, frozen_keep=(), slot=0,
                         extra_destroy=None):
    """Phase 1's teardown decision, pure so the teardown→redeploy loop is testable offline.

    Wave 1: every team box (the computed set covers ALL teams now that terraform builds
    them), legacy API clones from a pre-golden range, and stranded clones from a PREVIOUS
    run with more teams — comp-tagged team boxes the current (smaller) team set no longer
    enumerates. Left behind they keep a linked-clone hold on the goldens, so wave 2 can't
    rebuild them (winad-testrun 2026-09-25: 3-team run then 2-team redeploy). Linked
    clones must die BEFORE their templates.
    Wave 2: the engine (a linked clone of the engine template — it dies and re-clones
    cheaply every run; slot 0 only), then every golden template that is missing,
    hash-mismatched, or a stale slot beyond the current lineup. MATCHING golden templates
    survive — that is the whole test-run reuse (build once per competition, reuse across
    its test runs). A frozen competition's code-only-drifted goldens (names in
    frozen_keep, as classified by the pre-phase-1 golden_freeze_gate) also survive: the
    gate already said "proceeding on the frozen template", so destroying the golden here
    would rebuild it and silently break freeze semantics (internals "frozen-gate golden
    keep").
    slot>0 (multi-node satellite): this node's golden span (slot-shifted vmids), no
    engine, plus extra_destroy (the satellite's jump VM) — the caller runs one wave
    pair per hosting node with that node's own targets."""
    wave1 = {t["vmid"]: t["vm_name"] for t in all_targets}
    for vmid, vm_name in legacy_clones.items():
        wave1.setdefault(vmid, vm_name)
    n_slots = max(len(boxes), MAX_BOXES_PER_TEAM)
    keep_vmids = {golden_vmid_for_slot(engine_vmid, slot, i) for i in range(n_slots)}
    if slot == 0:
        keep_vmids |= {engine_vmid, engine_template_vmid(engine_vmid)}
    for vm in node_vms:
        vid = vm["vmid"]
        if vid in wave1 or vid in keep_vmids:
            continue
        raw = str(vm.get("tags") or "")
        tags = {t.strip() for t in raw.replace(";", ",").split(",") if t.strip()}
        if comp_tags <= tags:
            wave1[vid] = f"{vm.get('name') or vid} (stranded clone)"
    wave2 = {engine_vmid: f"engine-{engine_vmid}"} if slot == 0 else {}
    for i in range(n_slots):
        vid = golden_vmid_for_slot(engine_vmid, slot, i)
        b = boxes[i] if i < len(boxes) else None
        if b is None:
            wave2[vid] = f"golden-slot-{i} (stale)"
            continue
        if is_unmanaged(b):
            # Unmanaged boxes have no golden; anything templated in the slot is a
            # stale leftover from an earlier lineup and nothing references it.
            wave2[vid] = f"golden-slot-{i} (stale, unmanaged box)"
            continue
        if (is_template(vid)
                and stored_hashes.get("golden", {}).get(b["name"], {}).get("hash")
                == golden_hashes[b["name"]]):
            print(f"  golden-{b['name']} hash matches — keeping template (test-run reuse)")
            continue
        if b["name"] in frozen_keep and is_template(vid):
            print(f"  golden-{b['name']} frozen with code-only drift — keeping the frozen "
                  f"template (rebuild would break freeze semantics)")
            continue
        wave2[vid] = f"golden-{b['name']}"
    if extra_destroy:
        wave2.update(extra_destroy)
    return wave1, wave2


def _parse_team_node(team_node):
    """--team-node '103=zfs-193,team4=hdd-150' -> dict; team keys or subnet identifiers."""
    if not team_node:
        return None
    if isinstance(team_node, dict):
        return team_node
    out = {}
    for pair in str(team_node).split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"  ERROR: --team-node entry {pair!r} is not TEAM=NODE")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def carry_box_password(previous_state):
    """box_password is a golden-hash INPUT (baked into /etc/shadow + cloud-init on the
    golden disk): re-minting it on a fresh deploy rebuilds every golden. Reuse the
    competition's existing one; mint only for a truly new competition."""
    return (previous_state or {}).get("box_password") or random_password()


def reset_domain_markers(comp_dir):
    """The per-deploy domain done-markers (.nakon-domain-<team>-adds.json) describe THIS
    deploy's DCs — run 2's fresh clones were never promoted, but a surviving run-1 artifact
    made the domain pass skip promotion (live-found 2026-09-25, matrix run 2). A fresh
    deploy resets them; resumes keep them (that is the guard's whole point). The
    per-COMPETITION .template-hashes.json is NOT touched — it drives template reuse."""
    for stale in Path(comp_dir).glob(".nakon-domain-*.json"):
        stale.unlink()


@dataclass
class DeployContext:
    """Everything the seven deploy phases share, built once by prepare().

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
    legacy_clones: dict = field(default_factory=dict)
    tf_ctx: dict = field(default_factory=dict)
    scoring_user: str = ""
    scoring_ip: str = ""
    ssh_key: Optional[Path] = None
    # --- run identity (constants.ownership_tags) ---
    # run_id: stamped on everything this run creates. reclaim_run_id: the PRIOR
    # state's run id — the only id that authorizes destroying a pre-existing VM in
    # phase 1 / the rebuild gates. They differ only on a fresh deploy whose state
    # predates run ids; post-change deploys always reuse the prior id.
    run_id: str = ""
    reclaim_run_id: str = ""
    allow_untagged_reclaim: bool = False

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
        """Record phase n as completed — the resume guard's only source of truth."""
        self.state["last_phase"] = n
        self.save_state()

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
    path it looked for. The pipeline-version refusal also lives here: a v1 file's phase
    numbers mean different things, and the guard must fire before anything resumes."""

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


def prepare(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
            team_node=None, engine_node=None, force_from_phase=False):
    """Everything deploy() must do before phase 1, as one testable step.

    Resolves the engine VMID, loads Compfile/config/state, mints or reuses the
    competition's secrets, computes multi-node placement, generates the nakon + stage
    configs, computes the golden hashes and runs the frozen gate, assembles
    terraform.tfvars.json, runs preflight and enumerates the targets. Returns None when
    the operator declines the confirmation prompt, so deploy() can return without
    running any phase (the same early return the inline code had).

    The write lock is deliberately NOT taken here: deploy() holds it across the whole
    run, phases included. Nothing here destroys anything — the frozen gate runs before
    phase 1 precisely so a freeze can still say "nothing destroyed"."""
    identity = RunIdentity()
    _resolve_engine_vmid(identity, comp_dir, from_phase, scoring_vmid)
    # (The engine lock itself is taken further down — multi-node placement must point
    # the env at the engine's host first, and that needs teams+boxes resolved.)

    spec = CompetitionSpec()
    _load_competition_spec(spec, comp_dir)

    prior = PriorDeployState()
    _load_prior_deploy_state(prior, comp_dir, from_phase, force_from_phase)

    inputs = CompetitionInputs()
    _load_competition_inputs(inputs, comp_dir)

    secrets = CompetitionSecrets()
    _resolve_competition_secrets(secrets, prior, spec, inputs, num_teams, identity, from_phase)

    place = EnginePlacement()
    _apply_engine_placement(place, prior, secrets, spec, identity, comp_dir, team_node, engine_node)

    generated = GeneratedConfigs()
    _generate_stage_configs_and_hashes(generated, comp_dir, spec, secrets)

    terraform = TerraformInputs()
    _build_terraform_inputs(terraform, comp_dir, spec, secrets, prior, identity, place, from_phase)

    _run_competition_preflight(comp_dir, spec, secrets, identity, place, terraform,
                               prior, from_phase)

    if not assume_yes and not prior.resuming:
        if not confirm_deploy(spec.name, spec.scenario, spec.difficulty, secrets.teams, spec.boxes):
            print("  Deployment cancelled.")
            return None

    targets = DeployTargets()
    _enumerate_deploy_targets(targets, comp_dir, spec, secrets, place)

    return _assemble_deploy_context(comp_dir, from_phase, assume_yes, identity, spec, prior,
                                    inputs, secrets, place, generated, terraform, targets)


def _resolve_competition_secrets(secrets, prior, spec, inputs, num_teams, identity, from_phase):
    """Mint or reuse every secret the competition needs, into `secrets`.

    A resume reuses state's secrets (minting only what an older state predates); a
    fresh deploy mints them, except where a carried box_password or the packet's
    passwords.json outranks the mint. From here on `secrets` is the single source for
    teams, passwords and the persisted .deploy_state.json dict itself."""
    # Run identity comes first so BOTH branches persist the same id (utils.mint_run_id):
    # reused from prior state when the competition directory has one — a resume, a
    # crash-loop re-run and a redeploy must reclaim and re-tag the SAME lineage, and a
    # fresh mint would orphan every kept template's ownership.
    secrets.run_id = prior.previous_state.get("run_id") or mint_run_id()
    if prior.resuming:
        secrets.state = json.loads(prior.state_path.read_text())
        secrets.teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in secrets.state["teams"].items()
        }
        secrets.number_of_teams = len(secrets.teams)
        secrets.admin_password = secrets.state.get("admin_password") or random_password()
        secrets.postgres_password = secrets.state.get("postgres_password") or random_password()
        secrets.redis_password = secrets.state.get("redis_password") or random_password()
        secrets.box_password = secrets.state.get("box_password") or random_password()
        secrets.box_creds = secrets.state.get("box_creds") or {
            name: random_password() for name in spec.credlist_usernames
        }
        secrets.domain_creds = secrets.state.get("domain_creds")
        secrets.inject_password = secrets.state.get("inject_password")
        secrets.state.update({
            "admin_password": secrets.admin_password,
            "postgres_password": secrets.postgres_password,
            "redis_password": secrets.redis_password,
            "box_password": secrets.box_password,
            "box_creds": secrets.box_creds,
            "domain_creds": secrets.domain_creds,
            "inject_password": secrets.inject_password,
            "scoring_vm_id": identity.engine_vmid,
            "run_id": secrets.run_id,
        })
        write_state(prior.state_path, secrets.state)
        print(f"  Resuming from phase {from_phase} "
              f"({secrets.number_of_teams} team(s), last completed phase {secrets.state.get('last_phase')})")
    else:
        if num_teams is not None:
            if not (1 <= num_teams <= MAX_TEAMS):
                raise SystemExit(
                    f"--teams must be between 1 and {MAX_TEAMS} (team identifiers are "
                    f"192.168.<101-254>.x)"
                )
            secrets.number_of_teams = num_teams
        else:
            while True:
                raw = input("How many teams? ").strip()
                try:
                    secrets.number_of_teams = int(raw)
                except ValueError:
                    print("  Enter a whole number.")
                    continue
                if 1 <= secrets.number_of_teams <= MAX_TEAMS:
                    break
                print(f"  Enter a number from 1 to {MAX_TEAMS} "
                      f"(team identifiers are 192.168.<101-254>.x).")
        secrets.teams = collect_teams(secrets.number_of_teams, identity.engine_vmid)
        secrets.admin_password = random_password()
        secrets.postgres_password = random_password()
        secrets.redis_password = random_password()
        # M4: box_password is a golden-hash INPUT (baked into /etc/shadow +
        # cloud-init on the golden disk) — a fresh deploy that re-minted it would
        # rebuild every golden and break the lifecycle's "2-team test run → 8-team
        # competition must not rebuild anything". Reuse the competition's existing
        # box password when prior state carries one; mint fresh only on a truly
        # new competition. passwords.json (packet profile) outranks both: the
        # packet's default credentials ARE the competition, and an operator edit
        # to passwords.json is a deliberate re-key (goldens rebuild — correct).
        secrets.box_password = ((inputs.packet_pw or {}).get("box_password")
                                or carry_box_password(prior.previous_state))
        if inputs.packet_pw:
            print("  Box credentials come from passwords.json (packet profile) — "
                  "not re-minted")
        secrets.box_creds = (dict(inputs.packet_credlists.get("linux") or {})
                             or {name: random_password() for name in spec.credlist_usernames})
        secrets.domain_creds = (dict(inputs.packet_credlists.get("domain") or {}) or None)
        secrets.inject_password = random_password() if inputs.injects else None
        secrets.state = {
            "last_phase": 0,
            "pipeline_version": PIPELINE_VERSION,
            "teams": secrets.teams,
            "admin_password": secrets.admin_password,
            "inject_password": secrets.inject_password,
            "postgres_password": secrets.postgres_password,
            "redis_password": secrets.redis_password,
            "box_password": secrets.box_password,
            "box_creds": secrets.box_creds,
            "domain_creds": secrets.domain_creds,
            "scoring_vm_id": identity.engine_vmid,
            "run_id": secrets.run_id,
        }
        write_state(prior.state_path, secrets.state)


def _apply_engine_placement(place, prior, secrets, spec, identity, comp_dir, team_node, engine_node):
    """Resolve multi-node placement, point the env at the engine's host, take the lock.

    Resolution happens now because teams and boxes are known and the Proxmox env must
    point at the engine's host BEFORE the endpoint-keyed lock and any node-scoped call.
    The lock is taken last, on the identity prepare() resolved first; it stays held for
    the whole process (deploy() relies on that)."""
    # Multi-node placement (no-op without nodes.json/placement.json): resolved now —
    # teams and boxes are known, and the env must point at the engine's host BEFORE
    # the endpoint-keyed engine lock and anything node-scoped. An existing
    # placement.json always wins (authoritative); a resume without one adopts its
    # deployed endpoint rather than re-balancing a live range.
    place.placement, _resolved_engine_record = resolve_placement(
        comp_dir, identity.engine_vmid, secrets.teams, spec.boxes, spec.comp_name,
        team_overrides=_parse_team_node(team_node), engine_override=engine_node,
        resume_endpoint=(prior.previous_state.get("deployed_endpoint") if prior.resuming else None))
    if place.placement:
        # Stays active for the whole deploy: every later node-scoped call and the
        # terraform env point at the placement's hosts.
        activate_placement(place.placement)
        if _resolved_engine_record is not None and _resolved_engine_record.engine_mgmt_ip:
            os.environ["TF_VAR_engine_mgmt_ip"] = _resolved_engine_record.engine_mgmt_ip
            if _resolved_engine_record.engine_mgmt_gw:
                os.environ.setdefault("TF_VAR_engine_mgmt_gw",
                                      _resolved_engine_record.engine_mgmt_gw)
    secrets.state["multi_node"] = bool(place.placement)
    write_state(prior.state_path, secrets.state)
    acquire_engine_lock(identity.engine_vmid)


def _generate_stage_configs_and_hashes(generated, comp_dir, spec, secrets):
    """Write the nakon/stage configs and compute every golden hash, into `generated`.

    The M4 golden hashes are computed BEFORE phase 1 — cleanup must know which golden
    templates survive (test-run reuse) and which rebuild — and the frozen gate fires
    here for the same reason: a config-class freeze must be able to say "nothing
    destroyed" before phase 1 starts deleting. The golden bundle is built now (content
    addressed; the phase-4 plant reuses the cache) because each box's hash consumes its
    plan's payload shas. See the comments on each sub-block for the incident history."""
    generated.nakon_config_path = generate_nakon_config(secrets.teams, spec.boxes, spec.difficulty, comp_dir, secrets.box_password,
                                               box_username=spec.box_username)
    # M3.2: the full bundle is never deployed as one pass anymore. The golden-stage
    # bundle is built inside golden_ops at plant time; the repair bundle in phase 5 and
    # the final bundle in phase 6 (all content-addressed, so resumes hit the cache).
    # Domain controllers keep an unbooted golden so each team's forest specializes its
    # own machine SID before promotion; their configs move to the repair stage.
    generated.unbooted = unbooted_golden_boxes(comp_dir)
    generated.golden_config_path, generated.repair_config_path, generated.final_config_path, _postclone_path = generate_stage_configs(
        comp_dir, secrets.teams, spec.boxes, unbooted=generated.unbooted)

    # M4: template hashes are computed BEFORE phase 1 — cleanup must know which golden
    # templates survive (test-run reuse) and which rebuild. The golden bundle is built
    # now (content-addressed; the phase-4 plant reuses the cache) because each box's
    # hash consumes its plan's payload shas. The frozen gate also fires HERE — before
    # phase 1 destroys anything ("nothing destroyed" is the whole point of the
    # freeze; the phase-4 per-box gate alone was too late, matrix run 4).
    node = os.environ["TF_VAR_proxmox_node"]
    golden_bundle = build_nakon_bundle(generated.golden_config_path)
    golden_machines_by_box = {
        m["name"].rsplit("-golden", 1)[0]: m
        for m in json.loads(generated.golden_config_path.read_text())["machines"]
    }
    base_template_ids = _template_vmid_map(node)
    generated.golden_inputs, generated.golden_hashes = golden_hash_entries(
        spec.boxes, base_template_ids, golden_machines_by_box, golden_bundle,
        secrets.box_password, spec.box_username, os.environ.get("TF_VAR_ssh_public_key", ""),
        spec.apt_cache, unbooted=generated.unbooted)

    frozen = frozen_state(comp_dir)
    generated.frozen_keep = set()
    if frozen:
        # The frozen record's `code` (commit + dirty) was written but never read: the
        # per-template drift gate below keys on input hashes and its code-class verdict
        # is warn-only, so a commit after --freeze used to run silently on unverified
        # code. Warn once here, before the golden loop, naming both commits.
        code_warning = frozen_code_drift(frozen)
        if code_warning:
            print(code_warning)
        # Goldens now: config-class drift refuses BEFORE phase 1 destroys anything.
        # The engine's gate still runs at phase 2 (its inputs are computed there),
        # likewise before any engine destruction. Code-only drift keeps the golden:
        # phase1_destroy_waves must not rebuild what the gate said to proceed on.
        frozen_hashes = (frozen.get("hashes") or {})
        # `box_name`, NOT `spec.name`: this loop used to read `for spec.name in
        # generated.golden_hashes`, which assigned each golden box name onto the spec
        # and left the LAST box there. On any FROZEN competition that clobbered
        # spec.name (the Compfile event name) before its later uses, so terraform's
        # `event_name` — and therefore `local.comp_tag`, the ownership tag on the engine
        # and every team box — became "comp-web01" instead of "comp-<competition>".
        # Phase 1's destroy compares against `comp-<competition>`, fails its
        # `comp_tags <= tags` ownership check, and REFUSES to reclaim the range's own
        # VMs; confirm_deploy also showed the wrong name. Fail-safe rather than
        # destructive, but it strands infrastructure and the next deploy collides on
        # those vmids.
        for box_name in generated.golden_hashes:
            stored_inputs = (frozen_hashes.get("golden") or {}).get(box_name, {}).get("inputs") or {}
            drift = golden_freeze_gate(box_name, stored_inputs, generated.golden_inputs[box_name],
                                       frozen.get("frozen_at"), golden_bundle)
            if drift["code"]:
                generated.frozen_keep.add(box_name)


def _build_terraform_inputs(terraform, comp_dir, spec, secrets, prior, identity, place, from_phase):
    """Assemble terraform.tfvars.json + the workdir/ssh fields, into `terraform`.

    terraform.tfvars.json outranks TF_VAR_* env, so it is the authoritative copy of
    this competition's teams/boxes/engine-vmid (concurrent comps on one node must not
    clobber each other through the shared .env). The stale-state guard runs BEFORE the
    file is written: the per-comp workdir carries state from wherever the last deploy
    ran, and pointing terraform at a different host/vmid would make it reconcile that
    state and destroy whatever sits at the old resources on the new host."""
    teams_json_src = {
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in secrets.teams.items()
    }
    if place.placement:
        # terraform's per-slot resources key on this: which host builds each team.
        for team_key in teams_json_src:
            teams_json_src[team_key]["slot"] = place.placement["team_slots"][team_key]
    teams_json = json.dumps(teams_json_src)
    boxes_json = json.dumps(spec.boxes)

    update_env({
        "TF_VAR_teams": teams_json,
        "TF_VAR_boxes_per_team": boxes_json,
        "TF_VAR_box_password": secrets.box_password,
        "TF_VAR_box_username": spec.box_username,
        "TF_VAR_scoring_vm_id": str(identity.engine_vmid),
    })

    # teams.json holds every team password: write it with 0600 applied at creation
    # (a write_text-then-chmod leaves it briefly world-readable).
    write_text_atomic(comp_dir / "teams.json", teams_json)

    # Per-competition Terraform working dir + tfvars so concurrent competitions on
    # one node don't share the single terraform/terraform.tfstate or clobber each
    # other via the shared .env. terraform.tfvars.json outranks TF_VAR_* env, so it
    # is authoritative for this comp's teams/boxes/engine-vmid regardless of what
    # another concurrent deploy wrote to .env. The ssh key path is made absolute
    # because it was ../proxmox-relative to the (now deeper) working dir.
    terraform.tf_dir = ensure_terraform_workdir(comp_dir)

    # Stale-state guard: the per-competition terraform workdir carries state from
    # wherever the LAST deploy ran. Pointing terraform at a different host (or engine
    # vmid) makes it "reconcile" that state against the new endpoint and destroy
    # whatever now sits at the old vmid there (2026-09-24: a realm run deleted that
    # host's existing 1090 engine because a dead primary attempt left 1090 in this
    # comp's state). Refuse unless the recorded host and vmid agree.
    if from_phase <= 2 and (terraform.tf_dir / "terraform.tfstate").exists() and prior.previous_state:
        cur_endpoint = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
        prev_endpoint = (prior.previous_state.get("deployed_endpoint") or "").rstrip("/")
        prev_engine = prior.previous_state.get("scoring_vm_id")
        host_mismatch = bool(prev_endpoint) and prev_endpoint != cur_endpoint
        vmid_mismatch = prev_engine is not None and int(prev_engine) != identity.engine_vmid
        if host_mismatch or vmid_mismatch:
            raise SystemExit(
                f"  ERROR: {terraform.tf_dir}/terraform.tfstate holds state from a deploy against "
                f"{prev_endpoint or 'another host'} (engine vmid {prev_engine}); this run "
                f"targets {cur_endpoint} (engine vmid {identity.engine_vmid}). Terraform would "
                f"reconcile the stale state and destroy whatever sits at those resources "
                f"on the new host. Destroy this competition first: python3 "
                f"destroy-competition.py --competition {spec.comp_name} --yes"
            )

    raw_key = os.environ["TF_VAR_ssh_private_key_path"]
    terraform.ssh_key_abs = (raw_key if os.path.isabs(raw_key)
                             else str((Path("terraform") / raw_key).resolve()))
    # Engine mgmt IP: static by default. The DHCP engine rebooted onto a different
    # address mid-event while terraform's saved output — which deploy/verify/
    # credentials all consume — stayed stale (shakedown-5x4: .221→.243→.233).
    # An explicit TF_VAR_engine_mgmt_ip="" keeps the old DHCP behavior; the
    # chosen value is also exported for template_ops (build-VM ipconfig0).
    terraform.engine_mgmt_ip = os.environ.get("TF_VAR_engine_mgmt_ip")
    if terraform.engine_mgmt_ip is None:
        terraform.engine_mgmt_ip = DEFAULT_ENGINE_MGMT_IP
        print(f"  Engine mgmt IP: static {terraform.engine_mgmt_ip} (default — override "
              f"TF_VAR_engine_mgmt_ip, set '' for DHCP)")
        os.environ["TF_VAR_engine_mgmt_ip"] = terraform.engine_mgmt_ip
    # Default the gateway whenever the engine mgmt IP is static (live-found 2026-09-29:
    # an explicitly-set mgmt IP skipped this branch on the .150 env, and the engine
    # template build's ipconfig0 went out with an empty gw= — PVE 400 "Parameter
    # verification failed").
    if not os.environ.get("TF_VAR_engine_mgmt_gw"):
        os.environ["TF_VAR_engine_mgmt_gw"] = DEFAULT_ENGINE_MGMT_GW
    terraform.tfvars = {
        # teams_json_src carries the placement slot per team (multi-node) — tfvars
        # outranks the env, so this is the copy terraform actually reads.
        "teams": teams_json_src,
        "boxes_per_team": spec.boxes,
        "box_password": secrets.box_password,
        "box_username": spec.box_username,
        "event_name": spec.name,
        "scoring_vm_id": identity.engine_vmid,
        "ssh_private_key_path": terraform.ssh_key_abs,
        # M3.3 two-apply: apply #1 (phase 2) builds the engine + bridges with an empty
        # team_box for_each; apply #2 (phase 4) flips this to true once the golden
        # templates exist. team_nics/reboot keep their full-teams config in apply #1 so
        # the engine already has a NIC on every bridge for the golden plant.
        "build_team_boxes": False,
        "golden_template_ids": [],
        # M4: apply #2 rewrites tfvars with this intact — a resume that skips phase 2
        # must not let the engine clone source fall back to the base image (which would
        # replace the engine with an unbootstrapped full clone mid-pipeline).
        "engine_clone_id": int(secrets.state.get("engine_template_vmid") or 0),
        # Portable-node mode (realm): static engine mgmt IP instead of agent discovery.
        # Persisted via tfvars so resumes don't depend on the env var being re-exported.
        "engine_mgmt_ip": terraform.engine_mgmt_ip,
        "engine_mgmt_gw": os.environ.get("TF_VAR_engine_mgmt_gw", ""),
        # Per-deploy identity: terraform stamps it onto the engine and every team box
        # (main.tf tags), making them reclaimable by THIS run's teardown only.
        "run_tag": secrets.run_id,
    }
    if place.placement:
        # Multi-node: per-slot satellite providers and the engine's jump routes.
        terraform.tfvars["satellites"] = satellite_tfvars(place.placement)
        terraform.tfvars["satellite_routes"] = satellite_routes_for(place.placement)
    terraform.tfvars_path = terraform.tf_dir / "terraform.tfvars.json"
    # Carries TF_VAR_box_password + the per-team passwords.
    write_text_atomic(terraform.tfvars_path, json.dumps(terraform.tfvars, indent=2))


def _run_competition_preflight(comp_dir, spec, secrets, identity, place, terraform,
                               prior, from_phase):
    """Run the competition preflight gates, at the phase-2 boundary.

    Gated on from_phase <= 2 because the gates check free capacity and template
    reachability for the work phases 1-2 do; a later resume must not re-refuse against
    an already-deployed range, which is also why check_free is off on a resume. The
    multinode gate is imported locally: only this branch pulls in the placement
    preflight machinery."""
    if from_phase <= 2:
        # The clash gates' "ours" exemption must be run-aware: the prior state's run
        # id is what marks THIS competition directory's leftovers ours. On a state
        # predating run ids it falls back to the minted id — pre-existing comp-tagged
        # VMs then classify as foreign and the gate refuses, pointing at the
        # --legacy-tags teardown (fail-closed beats guessing whose VM it is).
        reclaim_tag = prior.previous_state.get("run_id") or secrets.run_id
        if place.placement:
            from config_ops import preflight_gates_multinode
            preflight_gates_multinode(comp_dir, spec.boxes, secrets.teams, identity.engine_vmid, place.placement,
                                      engine_mgmt_ip=terraform.engine_mgmt_ip,
                                      check_free=not prior.resuming, our_run_tag=reclaim_tag)
        else:
            preflight_gates(comp_dir, spec.boxes, secrets.number_of_teams, teams=secrets.teams,
                            engine_vmid=identity.engine_vmid, check_free=not prior.resuming,
                            engine_mgmt_ip=terraform.engine_mgmt_ip, our_run_tag=reclaim_tag)


def _enumerate_deploy_targets(targets, comp_dir, spec, secrets, place):
    """Enumerate every VM this deploy will touch, into `targets`.

    node is read from the env here (the engine's host, already selected by placement);
    all_targets keeps every positional vmid, while the Linux/Windows work lists drop
    unmanaged boxes — pfSense/appliances have no plant/repair/fix_services/cloud-init,
    they clone from their own template and self-configure (but must stay in all_targets
    for vmid arithmetic)."""
    targets.node = os.environ["TF_VAR_proxmox_node"]
    targets.all_targets = enumerate_targets(secrets.teams, spec.boxes,
                                           placement=place.placement, default_node=targets.node)
    persist_targets(comp_dir, targets.all_targets, spec.boxes)
    # Unmanaged boxes (pfSense/appliances) get no plant/repair/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    targets.managed_targets = [t for t in targets.all_targets if not is_unmanaged(t["box"])]
    targets.linux_targets = [t for t in targets.managed_targets if not is_windows_template(t["box"]["template"])]
    targets.windows_targets = [t for t in targets.managed_targets if is_windows_template(t["box"]["template"])]


def _assemble_deploy_context(comp_dir, from_phase, assume_yes, identity, spec, prior, inputs,
                             secrets, place, generated, terraform, targets):
    """Expand the prepared stages onto DeployContext's flat fields.

    Kept out of prepare() so the sequencer reads as the ordered steps it is: this is the
    one place that maps the stage objects onto DeployContext, whose field order follows
    the pipeline."""
    return DeployContext(
        comp_dir=comp_dir,
        comp_name=spec.comp_name,
        state_path=prior.state_path,
        from_phase=from_phase,
        resuming=prior.resuming,
        assume_yes=assume_yes,
        name=spec.name,
        scenario=spec.scenario,
        box_username=spec.box_username,
        credlist_usernames=spec.credlist_usernames,
        nakon_jobs=spec.nakon_jobs,
        apt_cache=spec.apt_cache,
        injects=inputs.injects,
        packet_pw=inputs.packet_pw,
        state=secrets.state,
        teams=secrets.teams,
        number_of_teams=secrets.number_of_teams,
        admin_password=secrets.admin_password,
        postgres_password=secrets.postgres_password,
        redis_password=secrets.redis_password,
        box_password=secrets.box_password,
        box_creds=secrets.box_creds,
        domain_creds=secrets.domain_creds,
        inject_password=secrets.inject_password,
        placement=place.placement,
        node=targets.node,
        engine_vmid=identity.engine_vmid,
        engine_mgmt_ip=terraform.engine_mgmt_ip,
        boxes=spec.boxes,
        boxes_by_name={b["name"]: b for b in spec.boxes},
        unbooted=generated.unbooted,
        nakon_config_path=generated.nakon_config_path,
        golden_config_path=generated.golden_config_path,
        repair_config_path=generated.repair_config_path,
        final_config_path=generated.final_config_path,
        golden_inputs=generated.golden_inputs,
        golden_hashes=generated.golden_hashes,
        frozen_keep=generated.frozen_keep,
        tf_dir=terraform.tf_dir,
        tfvars_path=terraform.tfvars_path,
        tfvars=terraform.tfvars,
        ssh_key_abs=terraform.ssh_key_abs,
        all_targets=targets.all_targets,
        managed_targets=targets.managed_targets,
        linux_targets=targets.linux_targets,
        windows_targets=targets.windows_targets,
        run_id=secrets.run_id,
        reclaim_run_id=(prior.previous_state.get("run_id") or ""),
        allow_untagged_reclaim=(os.environ.get("TEZ_ALLOW_UNTAGGED_RECLAIM") == "1"),
    )


def _resolve_engine_vmid(identity, comp_dir, from_phase, scoring_vmid):
    """Settle the scoring-engine VMID into `identity` (see RunIdentity for why first).

    A corrupt or absent .deploy_state.json here falls back to the default rather than
    raising: _load_prior_deploy_state reads the same file a few steps later and does the
    refusing, with a message that names the actual problem."""
    # Resolve this competition's scoring-engine VMID before taking the engine lock
    # (the lock is keyed on it) and before phase-1 cleanup destroys it. On resume it
    # is authoritative from .deploy_state.json; on a fresh deploy it comes from the
    # flag/arg (default SCORING_ENGINE_VMID).
    _early_state = comp_dir / ".deploy_state.json"
    if from_phase > 1 and _early_state.exists():
        try:
            identity.engine_vmid = int(
                json.loads(_early_state.read_text()).get("scoring_vm_id", SCORING_ENGINE_VMID))
        except (ValueError, json.JSONDecodeError):
            identity.engine_vmid = SCORING_ENGINE_VMID
    else:
        identity.engine_vmid = int(scoring_vmid) if scoring_vmid is not None else SCORING_ENGINE_VMID


def _load_competition_spec(spec, comp_dir):
    """Load the Compfile, users.json and boxes.json into `spec`; print the banner.

    A missing boxes.json is fatal here: every later step (and every phase) enumerates
    boxes. Known-broken templates only warn — the competition may predate the fix, and
    the deploy still has to be able to refuse later on its own terms."""
    spec.name, spec.scenario, spec.difficulty = load_compfile(comp_dir / "Compfile")
    spec.box_username, spec.credlist_usernames = load_users_config(comp_dir)
    # nakon --jobs pass-through (M1.2): per-machine work is atomic in nakon's runner, so N
    # machines plant concurrently with each machine's step order (disruptive last) intact.
    # The ceiling is engine egress/CPU and mirror throughput, not the datastore (no bulk
    # writes) — start at 4 and judge against the M0.2 timings.
    spec.nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
    # apt_cache (M1.3): point each box's apt at the engine's apt-cacher-ng mirror cache.
    # Approved trade-off: through the IP-based proxy, resolv-conf-null-dns no longer breaks
    # apt (it still breaks every other resolver user on the box); empty-sources/hold
    # configs break apt either way. Set `apt_cache 0` in the Compfile for full realism.
    spec.apt_cache = bool(compfile_flag(comp_dir / "Compfile", "apt_cache", 1))
    spec.comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {spec.comp_name}")
    print(f"{'='*60}\n")

    spec.boxes = load_boxes(comp_dir)
    if not spec.boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)

    KNOWN_BROKEN_TEMPLATES = {"debian13-lite", "ubuntu24.04"}
    for b in spec.boxes:
        if b.get("template") in KNOWN_BROKEN_TEMPLATES:
            print(f"  WARNING: box '{b['name']}' uses template '{b['template']}', which is "
                  f"known broken (bad cloud-init — clones won't get a working network/SSH). "
                  f"Use '{b['template']}-fix' instead. See docs/usage-people.md's "
                  f"Troubleshooting table.")


def _load_prior_deploy_state(prior, comp_dir, from_phase, force_from_phase):
    """Read .deploy_state.json into `prior` and run the two resume refusals.

    Refuses a resume with no state file (there are no saved secrets to resume with) and
    refuses to resume across the pipeline-v1/v2 boundary (the phase numbers changed
    meaning). guard_resume_from_phase then refuses a --from-phase that skips phases the
    state never saw complete; force_from_phase is the operator's explicit override."""
    prior.state_path = comp_dir / ".deploy_state.json"
    prior.previous_state = {}
    if prior.state_path.exists():
        try:
            prior.previous_state = json.loads(prior.state_path.read_text())
        except (ValueError, OSError):
            prior.previous_state = {}
    if from_phase > 1 and not prior.state_path.exists():
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} but {prior.state_path} doesn't exist — there are no "
            f"saved team passwords/secrets to resume with, and generating fresh ones while "
            f"skipping the destructive phases would leave the deployed range and its credentials "
            f"out of sync. Re-run without --from-phase for a clean redeploy."
        )
    prior.resuming = from_phase > 1

    # Pipeline v2 (M3): golden templates + linked clones, phases renumbered. A v1 state
    # file's phase numbers mean different things, so resuming across the boundary is
    # refused rather than silently misinterpreted.
    if prior.resuming:
        _pv = None
        try:
            _pv = json.loads(prior.state_path.read_text()).get("pipeline_version")
        except (ValueError, OSError):
            pass
        if _pv != PIPELINE_VERSION:
            raise SystemExit(
                f"  ERROR: {prior.state_path} was written by pipeline v{_pv if _pv is not None else '1'} "
                f"(pre-golden-template phases) — this code is pipeline v{PIPELINE_VERSION} and its "
                f"--from-phase numbers mean different things. Run a fresh deploy (no --from-phase)."
            )
        # Refuse to skip a phase that never completed: the skipped phases are what build
        # the machines the later ones target (see guard_resume_from_phase).
        guard_resume_from_phase(from_phase, prior.previous_state.get("last_phase"), prior.state_path,
                                force=force_from_phase)
        # ...and refuse a resume-loop: the same phase failing the same way over and
        # over is not a repair (see guard_resume_streak).
        guard_resume_streak(from_phase, prior.previous_state, force=force_from_phase)


def _load_competition_inputs(inputs, comp_dir):
    """Load injects and the packet-published credentials into `inputs`.

    Loaded after the resume guards and before the secrets step: passwords.json outranks
    both state and a fresh mint, and the secrets step needs the packet's derived
    credlists, so the two inputs are materialised together in one place."""
    inputs.injects = load_injects(comp_dir)
    # Packet-published credentials (compile-packet.py -> passwords.json): when present,
    # box_password and the credlists are the packet's default credentials verbatim, not
    # random mints. Teams get them in the packet and rotate at minute zero — that IS the
    # competition. Also makes redeploy deterministic (no re-minted secret drifting from
    # the packet).
    inputs.packet_pw = load_packet_passwords(comp_dir)
    inputs.packet_credlists = (inputs.packet_pw or {}).get("credlists") or {}


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
           team_node=None, engine_node=None, force_from_phase=False, min_load_free=None):
    """Run the seven-phase deploy for one competition; from_phase > 1 resumes from .deploy_state.json.

    scoring_vmid overrides the scoring-engine VMID (default 1000) so several
    competitions can run concurrently on one node; it is persisted to
    .deploy_state.json and reused on resume.
    team_node (--team-node id=NODE,...) and engine_node (--engine-node NAME) pin the
    multi-node placement when nodes.json exists; without them teams are placed by
    capacity-fill. force_from_phase (--force-from-phase) bypasses the guard that refuses
    to skip phases .deploy_state.json never recorded as completed."""
    # Imported here, not at module scope: deploy_phases reaches back into this module
    # for the pure phase helpers and the globals the offline tests monkeypatch, so a
    # module-level import would be circular (and `python3 deploy.py` would trip over a
    # partially-initialised deploy_phases).
    from deploy_phases import PHASES, connect_terraform, finish_deploy

    # One driver per competition. Two concurrent deploys share the engine's
    # /opt/nakon staging dir and each one's 'rm -rf /opt/nakon/*' wipes the
    # other's plan archives mid-plant (scrim-extreme-2026-09-20: a pkill'd
    # wrapper left run-2's python alive while run-3 started; both died with
    # 'bundle is missing its plan archive').
    lock_fh = open(comp_dir / ".deploy.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"  ERROR: another deploy already holds the lock on {comp_dir} — refusing to run "
            "concurrently. Find it with pgrep -f create-competition; kill the PYTHON driver "
            "itself (pkill -f 'python.*create-competition'), not just a wrapper, and confirm "
            "with pgrep before retrying."
        )
    _DEPLOY_LOCKS[str(comp_dir)] = lock_fh

    ctx = prepare(comp_dir, num_teams=num_teams, assume_yes=assume_yes, from_phase=from_phase,
                  scoring_vmid=scoring_vmid, team_node=team_node, engine_node=engine_node,
                  force_from_phase=force_from_phase)
    if ctx is None:
        return

    # `--min-load-free N` on a resume into a phase that previously failed: wait for the
    # node to come down before starting. The retries that produced the eleven-attempt
    # cde-2026 storm were pure contention, and an operator was doing this by hand.
    if min_load_free is not None and ctx.from_phase > 1:
        streak_phase = (ctx.state.get("failure_streak") or {}).get("phase")
        if streak_phase == ctx.from_phase:
            print(f"  Phase {ctx.from_phase} failed on the previous attempt — waiting for "
                  f"node '{ctx.node}' to fall below load {min_load_free}...")
            wait_for_node_load(ctx.node, float(min_load_free), timeout=MIN_LOAD_FREE_TIMEOUT)

    current_phase = max(ctx.from_phase, 1)
    try:
        for n, phase in enumerate(PHASES, 1):
            # Every phase is CALLED so it can print its own "[N/7] Skipped (resume)"
            # banner, but only a phase that actually ran may become the failure's
            # phase or write last_phase: checkpointing a skipped phase would move the
            # resume guard's answer forward for a phase this run never executed.
            running = ctx.from_phase <= n
            if running:
                current_phase = n
            phase(ctx)
            if running:
                ctx.checkpoint(n)
            if n == 2:
                # The terraform/SSH context is read once, exactly where the inline
                # code read it: after phase 2's checkpoint, before phase 3. A fresh
                # run only has terraform's outputs after apply #1; a --from-phase 3+
                # resume skips the apply but still needs them. Outside the `running`
                # gate on purpose, so a failure here reports the phase the operator
                # asked to resume at rather than phase 2.
                connect_terraform(ctx)
    except BaseException as e:
        print(f"\n  [!] Deploy failed during phase {current_phase} of '{ctx.comp_name}'.")
        try:
            count = record_failure(ctx.state, current_phase, e)
            ctx.save_state()
            print(f"      Attempt {count} at phase {current_phase} with this signature"
                  + (f" — {RESUME_ATTEMPT_LIMIT} is the limit before a resume is refused."
                     if count >= RESUME_ATTEMPT_LIMIT else "."))
        except Exception as record_error:
            # Never let bookkeeping mask the real failure.
            print(f"      (could not record the failure streak: {record_error})")
        resume_phase = current_phase
        if current_phase >= 2 and "already exists" in str(e).lower():
            resume_phase = 1
            print("      This looks like a Proxmox/Terraform state mismatch (something the "
                  "prior attempt created still exists, but Terraform's state doesn't know about "
                  "it) — resuming from the failed phase would just hit the same error again.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {ctx.comp_name} --from-phase {resume_phase} --yes")
        raise

    clear_failure_streak(ctx.state)
    ctx.save_state()
    finish_deploy(ctx)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Tezcatlipoca — CTF Range Deployment. With no flags it runs fully "
                    "interactively (as before); flags let it run non-interactively.",
    )
    parser.add_argument("--competition", help="Competition name. If it already has a Compfile + "
                                              "boxes.json, deploy it straight away; otherwise it "
                                              "is created (needs --scenario/--difficulty or falls "
                                              "back to prompting).")
    parser.add_argument("--teams", type=int, help="Number of teams (skips the 'How many teams?' prompt).")
    parser.add_argument("--yes", action="store_true", help="Skip the confirm-deploy prompt.")
    parser.add_argument("--scenario", help="Scenario description (only when creating a new competition).")
    parser.add_argument("--difficulty", type=int, help="Difficulty 1-10 (only when creating a new competition).")
    parser.add_argument("--box-username", dest="box_username",
                        help="Themeable box login username (only when creating a new competition; "
                             "default 'ubuntu'). Written to competitions/<id>/users.json.")
    parser.add_argument("--credlist-usernames", dest="credlist_usernames",
                        help="Comma-separated, exactly 3 themeable credlist account names (only "
                             "when creating a new competition; default 'admin,user1,user2'). "
                             "Written to competitions/<id>/users.json.")
    parser.add_argument("--from-phase", type=int, default=1, dest="from_phase",
                        help="Resume from this phase (>1 skips the destructive cleanup + terraform "
                             "apply). See the resume hint printed on a failed deploy. Refused when "
                             "it skips phases the state file doesn't record as completed.")
    parser.add_argument("--force-from-phase", action="store_true", dest="force_from_phase",
                        help="Proceed even when --from-phase skips phases .deploy_state.json does "
                             "not record as completed. Only for a known-stale checkpoint (e.g. the "
                             "process died after a phase finished but before it was checkpointed); "
                             "the skipped phases build the machines the later ones target.")
    parser.add_argument("--min-load-free", type=float, default=None, dest="min_load_free",
                        help="On a resume into a phase that previously failed, wait until the "
                             "node's 1-minute load is below this before starting. Phase-4 retries "
                             "used to be hand-throttled this way; a contended node, not a short "
                             "timeout, is what made cde-2026 loop eleven times.")
    parser.add_argument("--scoring-vmid", type=int, default=None, dest="scoring_vmid",
                        help="VMID for this competition's scoring engine (default 1000). Give each "
                             "concurrent competition on a shared node a distinct free VMID so their "
                             "engines don't collide. Persisted to .deploy_state.json and reused on "
                             "resume (ignored on --from-phase, which reads it back from state).")
    parser.add_argument("--team-node", dest="team_node", default=None,
                        help="Multi-node pin: comma list of team=NODE, e.g. '103=zfs-193,104=zfs-193' "
                             "(team key or subnet identifier = nodes.json node name). Unpinned teams "
                             "are placed by capacity-fill. Needs nodes.json; ignored when this "
                             "competition already has a placement.json.")
    parser.add_argument("--engine-node", dest="engine_node", default=None,
                        help="Multi-node pin: which nodes.json node hosts the scoring engine "
                             "(default: the node holding the most teams). Needs nodes.json; ignored "
                             "when this competition already has a placement.json.")
    parser.add_argument("--plan-only", action="store_true", dest="plan_only",
                        help="Collect/generate the competition's config (Compfile, boxes.json) "
                             "and print a summary, then exit WITHOUT touching any infrastructure "
                             "— no teardown, no `terraform apply`. There is no confirmation "
                             "checkpoint between the box picker and a real deploy otherwise "
                             "(--yes skips it outright, and piped/scripted stdin that happens to "
                             "satisfy every remaining prompt walks straight into one) — use this "
                             "to review the plan first, then re-run without the flag to deploy it "
                             "for real.")
    args = parser.parse_args()

    def _print_plan(comp_name, comp_dir):
        boxes = json.loads((comp_dir / "boxes.json").read_text())
        print(f"\n  ── PLAN for '{comp_name}' — nothing has been deployed " + "─" * 20)
        for b in boxes:
            disk = f"{b['disk_gb']} GB disk" if b.get("disk_gb") else "template's own disk"
            print(f"    {b['name']:<12} {b['template']:<20} {b['cpu']} CPU, {b['memory_mb']} MB, {disk}")
        try:
            state_run = (json.loads((comp_dir / ".deploy_state.json").read_text()).get("run_id") or "")
        except (ValueError, OSError):
            state_run = ""
        if state_run:
            print(f"  Run identity: {state_run} (reused from this competition's state)")
        else:
            print("  Run identity: minted at deploy time (run-<id>, stored in "
                  ".deploy_state.json) — destruction paths require it")
        print("\n  Team count is decided at deploy time (--teams N, or the prompt).")
        print("  Nothing was deployed — no teardown, no terraform apply.")
        print(f"  Deploy for real with: python3 create-competition.py --competition {comp_name} "
              f"--teams <N> --yes")

    print("Tezcatlipoca - CTF Range Deployment")
    print("=" * 40)

    if args.competition:
        comp_name = args.competition.strip().lower().replace(" ", "-")
        if not valid_comp_name(comp_name):
            sys.exit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
        comp_dir = Path("competitions") / comp_name
        has_compfile = (comp_dir / "Compfile").exists()
        has_boxes = (comp_dir / "boxes.json").exists()

        if comp_dir.is_dir() and has_compfile and has_boxes:
            print(f"Reusing existing competition '{comp_name}'.")
        else:
            print(f"Creating new competition '{comp_name}'.")
            comp_dir.mkdir(parents=True, exist_ok=True)
            scenario = args.scenario if args.scenario is not None else input("Scenario description: ").strip()
            difficulty = args.difficulty if args.difficulty is not None else _prompt_difficulty()
            (comp_dir / "Compfile").write_text(
                f"name {comp_name}\n"
                f"scenario {scenario}\n"
                f"difficulty {difficulty}\n"
            )
            boxes = collect_boxes()
            (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))
            box_username, credlist_usernames = collect_users_config(
                box_username_flag=args.box_username, credlist_flag=args.credlist_usernames
            )
            (comp_dir / "users.json").write_text(json.dumps(
                {"box_username": box_username, "credlist_usernames": credlist_usernames}, indent=2
            ))

        if args.plan_only:
            _print_plan(comp_name, comp_dir)
            return

        deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase,
               scoring_vmid=args.scoring_vmid, team_node=args.team_node, engine_node=args.engine_node,
               force_from_phase=args.force_from_phase, min_load_free=args.min_load_free)
        return

    previous = load_previous_competitions()
    if previous:
        print("\nPrevious competitions:")
        for i, name in enumerate(previous, 1):
            print(f"  [{i}] {name}")
        print()

    choice = input("Create new (n) or reuse existing (number)? ").strip()

    if choice.lower() == "n":
        comp_name = input("Competition name: ").strip().lower().replace(" ", "-")
        if not valid_comp_name(comp_name):
            sys.exit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
        comp_dir = Path("competitions") / comp_name
        comp_dir.mkdir(parents=True, exist_ok=True)

        scenario = input("Scenario description: ").strip()
        difficulty = _prompt_difficulty()

        (comp_dir / "Compfile").write_text(
            f"name {comp_name}\n"
            f"scenario {scenario}\n"
            f"difficulty {difficulty}\n"
        )

        boxes = collect_boxes()
        (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))

        box_username, credlist_usernames = collect_users_config()
        (comp_dir / "users.json").write_text(json.dumps(
            {"box_username": box_username, "credlist_usernames": credlist_usernames}, indent=2
        ))
    else:
        try:
            idx = int(choice) - 1
        except ValueError:
            print("Invalid choice.")
            sys.exit(1)
        if 0 <= idx < len(previous):
            comp_name = previous[idx]
        else:
            print("Invalid choice.")
            sys.exit(1)
        comp_dir = Path("competitions") / comp_name

    if args.plan_only:
        _print_plan(comp_name, comp_dir)
        return

    deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase,
           scoring_vmid=args.scoring_vmid, team_node=args.team_node, engine_node=args.engine_node,
           force_from_phase=args.force_from_phase, min_load_free=args.min_load_free)


if __name__ == "__main__":
    main()