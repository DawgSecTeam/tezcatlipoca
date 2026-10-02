"""Orchestrator: seven-phase deploy and CLI."""

import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

import urllib3
from dotenv import load_dotenv

from beacon_ops import plant_team_beacons
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
    resolve_inject_times,
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
    PER_MACHINE_NAKON_BUDGET,
    SCORING_ENGINE_VMID,
    SNAP_BASE,
    SNAP_READY,
)
from domain_ops import deploy_domain_configs
from engine_ops import (ensure_nat_forwarding, prepare_engine_from_template,
                        push_event_conf)
from golden_ops import (_is_template, _template_vmid_map, build_golden_set,
                        unbooted_golden_boxes)
from hardening_ops import (_APT_PREP_BODY, _apt_prep_script, ensure_alpine_services,
                           fix_dns_on_boxes, fix_services_on_boxes, prep_apt_on_boxes,
                           setup_ubuntu_auth)
from nakon_ops import (acquire_engine_lock, build_nakon_bundle, generate_nakon_config,
                       generate_slot_golden_config, generate_stage_configs, run_nakon)
from nodes_ops import (activate_placement, golden_vmid_for_slot,
                       record_of, resolve_placement, satellite_routes_for,
                       satellite_tfvars)
from quotient.setup import create_injects, engine_paused, seed_teams, unpause_engine
from range_ops import (destroy_vm_if_exists, ensure_terraform_workdir, enumerate_targets,
                       persist_targets, take_snapshot,
                       terraform_dir, terraform_plugin_cache_dir)
from ssh_ops import (read_terraform_ctx, wait_for_boxes_ssh, wait_for_cloud_init,
                     wait_for_http)
from template_ops import (
    code_hash,
    engine_template_vmid,
    frozen_gate,
    frozen_state,
    golden_freeze_gate,
    golden_hash_inputs,
    golden_payload_hash,
    hash_from_inputs,
    load_template_hashes,
    save_template_hashes,
    stored_template_hash,
)
from timing import print_timing_summary, timed
from utils import (compfile_flag, is_unmanaged, load_compfile, load_users_config,
                   run_concurrent, run_terraform, valid_comp_name)
from windows_ops import bootstrap_windows_box, is_windows_template

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_DEPLOY_LOCKS = {}  # path -> open fh (keep referenced so flock survives)


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
                        golden_hashes, golden_inputs, comp_name):
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
            destroy_vm_if_exists(destroy_node, vid, expect_tags={
                "tezcatlipoca", GOLDEN_TAG, f"comp-{comp_name}"},
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
        """The tags every VM this competition owns carries (destroy's ownership guard)."""
        return {"tezcatlipoca", f"comp-{self.comp_name}"}


def boot_win(ctx, t):
    """Bootstrap one Windows box (apply #2 re-created it as a fresh clone)."""
    print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
    gw = f"192.168.{t['identifier']}.1"
    with timed(ctx.comp_dir, 4, "bootstrap_windows", t["ip"]):
        bootstrap_windows_box(t.get("node", ctx.node), t["vmid"], t["ip"], gw,
                              "8.8.8.8", ctx.box_password)


def snap_base(ctx, t):
    """Take the pre-sweep SNAP_BASE restore point on one target (the run_concurrent unit)."""
    with timed(ctx.comp_dir, 4, "snapshot", t["vm_name"]):
        take_snapshot(t.get("node", ctx.node), t["vmid"], SNAP_BASE,
                      description="tezcatlipoca: booted, networked, pre-sweep")


def snap_ready(ctx, t):
    """Take the as-delivered SNAP_READY restore point on one target."""
    with timed(ctx.comp_dir, 6, "snapshot", t["vm_name"]):
        take_snapshot(t.get("node", ctx.node), t["vmid"], SNAP_READY,
                      description="tezcatlipoca: as delivered, post-sweep + hardening")


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
    # Resolve this competition's scoring-engine VMID before taking the engine lock
    # (the lock is keyed on it) and before phase-1 cleanup destroys it. On resume it
    # is authoritative from .deploy_state.json; on a fresh deploy it comes from the
    # flag/arg (default SCORING_ENGINE_VMID).
    _early_state = comp_dir / ".deploy_state.json"
    if from_phase > 1 and _early_state.exists():
        try:
            engine_vmid = int(json.loads(_early_state.read_text()).get("scoring_vm_id", SCORING_ENGINE_VMID))
        except (ValueError, json.JSONDecodeError):
            engine_vmid = SCORING_ENGINE_VMID
    else:
        engine_vmid = int(scoring_vmid) if scoring_vmid is not None else SCORING_ENGINE_VMID
    # (The engine lock itself is taken further down — multi-node placement must point
    # the env at the engine's host first, and that needs teams+boxes resolved.)

    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    box_username, credlist_usernames = load_users_config(comp_dir)
    # nakon --jobs pass-through (M1.2): per-machine work is atomic in nakon's runner, so N
    # machines plant concurrently with each machine's step order (disruptive last) intact.
    # The ceiling is engine egress/CPU and mirror throughput, not the datastore (no bulk
    # writes) — start at 4 and judge against the M0.2 timings.
    nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
    # apt_cache (M1.3): point each box's apt at the engine's apt-cacher-ng mirror cache.
    # Approved trade-off: through the IP-based proxy, resolv-conf-null-dns no longer breaks
    # apt (it still breaks every other resolver user on the box); empty-sources/hold
    # configs break apt either way. Set `apt_cache 0` in the Compfile for full realism.
    apt_cache = bool(compfile_flag(comp_dir / "Compfile", "apt_cache", 1))
    comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {comp_name}")
    print(f"{'='*60}\n")

    boxes = load_boxes(comp_dir)
    if not boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)

    KNOWN_BROKEN_TEMPLATES = {"debian13-lite", "ubuntu24.04"}
    for b in boxes:
        if b.get("template") in KNOWN_BROKEN_TEMPLATES:
            print(f"  WARNING: box '{b['name']}' uses template '{b['template']}', which is "
                  f"known broken (bad cloud-init — clones won't get a working network/SSH). "
                  f"Use '{b['template']}-fix' instead. See docs/usage-people.md's "
                  f"Troubleshooting table.")

    state_path = comp_dir / ".deploy_state.json"
    previous_state = {}
    if state_path.exists():
        try:
            previous_state = json.loads(state_path.read_text())
        except (ValueError, OSError):
            previous_state = {}
    if from_phase > 1 and not state_path.exists():
        raise SystemExit(
            f"  ERROR: --from-phase {from_phase} but {state_path} doesn't exist — there are no "
            f"saved team passwords/secrets to resume with, and generating fresh ones while "
            f"skipping the destructive phases would leave the deployed range and its credentials "
            f"out of sync. Re-run without --from-phase for a clean redeploy."
        )
    resuming = from_phase > 1

    # Pipeline v2 (M3): golden templates + linked clones, phases renumbered. A v1 state
    # file's phase numbers mean different things, so resuming across the boundary is
    # refused rather than silently misinterpreted.
    PIPELINE_VERSION = 2
    if resuming:
        _pv = None
        try:
            _pv = json.loads(state_path.read_text()).get("pipeline_version")
        except (ValueError, OSError):
            pass
        if _pv != PIPELINE_VERSION:
            raise SystemExit(
                f"  ERROR: {state_path} was written by pipeline v{_pv if _pv is not None else '1'} "
                f"(pre-golden-template phases) — this code is pipeline v{PIPELINE_VERSION} and its "
                f"--from-phase numbers mean different things. Run a fresh deploy (no --from-phase)."
            )
        # Refuse to skip a phase that never completed: the skipped phases are what build
        # the machines the later ones target (see guard_resume_from_phase).
        guard_resume_from_phase(from_phase, previous_state.get("last_phase"), state_path,
                                force=force_from_phase)

    injects = load_injects(comp_dir)
    # Packet-published credentials (compile-packet.py -> passwords.json): when present,
    # box_password and the credlists are the packet's default credentials verbatim, not
    # random mints. Teams get them in the packet and rotate at minute zero — that IS the
    # competition. Also makes redeploy deterministic (no re-minted secret drifting from
    # the packet).
    packet_pw = load_packet_passwords(comp_dir)
    packet_credlists = (packet_pw or {}).get("credlists") or {}

    if resuming:
        state = json.loads(state_path.read_text())
        teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in state["teams"].items()
        }
        number_of_teams = len(teams)
        admin_password = state.get("admin_password") or random_password()
        postgres_password = state.get("postgres_password") or random_password()
        redis_password = state.get("redis_password") or random_password()
        box_password = state.get("box_password") or random_password()
        box_creds = state.get("box_creds") or {
            name: random_password() for name in credlist_usernames
        }
        domain_creds = state.get("domain_creds")
        inject_password = state.get("inject_password")
        state.update({
            "admin_password": admin_password,
            "postgres_password": postgres_password,
            "redis_password": redis_password,
            "box_password": box_password,
            "box_creds": box_creds,
            "domain_creds": domain_creds,
            "inject_password": inject_password,
            "scoring_vm_id": engine_vmid,
        })
        write_state(state_path, state)
        print(f"  Resuming from phase {from_phase} "
              f"({number_of_teams} team(s), last completed phase {state.get('last_phase')})")
    else:
        if num_teams is not None:
            if not (1 <= num_teams <= MAX_TEAMS):
                raise SystemExit(
                    f"--teams must be between 1 and {MAX_TEAMS} (team identifiers are "
                    f"192.168.<101-254>.x)"
                )
            number_of_teams = num_teams
        else:
            while True:
                raw = input("How many teams? ").strip()
                try:
                    number_of_teams = int(raw)
                except ValueError:
                    print("  Enter a whole number.")
                    continue
                if 1 <= number_of_teams <= MAX_TEAMS:
                    break
                print(f"  Enter a number from 1 to {MAX_TEAMS} "
                      f"(team identifiers are 192.168.<101-254>.x).")
        teams = collect_teams(number_of_teams, engine_vmid)
        admin_password = random_password()
        postgres_password = random_password()
        redis_password = random_password()
        # M4: box_password is a golden-hash INPUT (baked into /etc/shadow +
        # cloud-init on the golden disk) — a fresh deploy that re-minted it would
        # rebuild every golden and break the lifecycle's "2-team test run → 8-team
        # competition must not rebuild anything". Reuse the competition's existing
        # box password when prior state carries one; mint fresh only on a truly
        # new competition. passwords.json (packet profile) outranks both: the
        # packet's default credentials ARE the competition, and an operator edit
        # to passwords.json is a deliberate re-key (goldens rebuild — correct).
        box_password = ((packet_pw or {}).get("box_password")
                        or carry_box_password(previous_state))
        if packet_pw:
            print("  Box credentials come from passwords.json (packet profile) — "
                  "not re-minted")
        box_creds = (dict(packet_credlists.get("linux") or {})
                     or {name: random_password() for name in credlist_usernames})
        domain_creds = (dict(packet_credlists.get("domain") or {}) or None)
        inject_password = random_password() if injects else None
        state = {
            "last_phase": 0,
            "pipeline_version": PIPELINE_VERSION,
            "teams": teams,
            "admin_password": admin_password,
            "inject_password": inject_password,
            "postgres_password": postgres_password,
            "redis_password": redis_password,
            "box_password": box_password,
            "box_creds": box_creds,
            "domain_creds": domain_creds,
            "scoring_vm_id": engine_vmid,
        }
        write_state(state_path, state)

    # Multi-node placement (no-op without nodes.json/placement.json): resolved now —
    # teams and boxes are known, and the env must point at the engine's host BEFORE
    # the endpoint-keyed engine lock and anything node-scoped. An existing
    # placement.json always wins (authoritative); a resume without one adopts its
    # deployed endpoint rather than re-balancing a live range.
    placement, _resolved_engine_record = resolve_placement(
        comp_dir, engine_vmid, teams, boxes, comp_name,
        team_overrides=_parse_team_node(team_node), engine_override=engine_node,
        resume_endpoint=(previous_state.get("deployed_endpoint") if resuming else None))
    if placement:
        # Stays active for the whole deploy: every later node-scoped call and the
        # terraform env point at the placement's hosts.
        activate_placement(placement)
        if _resolved_engine_record is not None and _resolved_engine_record.engine_mgmt_ip:
            os.environ["TF_VAR_engine_mgmt_ip"] = _resolved_engine_record.engine_mgmt_ip
            if _resolved_engine_record.engine_mgmt_gw:
                os.environ.setdefault("TF_VAR_engine_mgmt_gw",
                                      _resolved_engine_record.engine_mgmt_gw)
    state["multi_node"] = bool(placement)
    write_state(state_path, state)
    acquire_engine_lock(engine_vmid)

    nakon_config_path = generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password,
                                               box_username=box_username)
    # M3.2: the full bundle is never deployed as one pass anymore. The golden-stage
    # bundle is built inside golden_ops at plant time; the repair bundle in phase 5 and
    # the final bundle in phase 6 (all content-addressed, so resumes hit the cache).
    # Domain controllers keep an unbooted golden so each team's forest specializes its
    # own machine SID before promotion; their configs move to the repair stage.
    unbooted = unbooted_golden_boxes(comp_dir)
    golden_config_path, repair_config_path, final_config_path, _postclone_path = generate_stage_configs(
        comp_dir, teams, boxes, unbooted=unbooted)

    # M4: template hashes are computed BEFORE phase 1 — cleanup must know which golden
    # templates survive (test-run reuse) and which rebuild. The golden bundle is built
    # now (content-addressed; the phase-4 plant reuses the cache) because each box's
    # hash consumes its plan's payload shas. The frozen gate also fires HERE — before
    # phase 1 destroys anything ("nothing destroyed" is the whole point of the
    # freeze; the phase-4 per-box gate alone was too late, matrix run 4).
    node = os.environ["TF_VAR_proxmox_node"]
    golden_bundle = build_nakon_bundle(golden_config_path)
    golden_machines_by_box = {
        m["name"].rsplit("-golden", 1)[0]: m
        for m in json.loads(golden_config_path.read_text())["machines"]
    }
    base_template_ids = _template_vmid_map(node)
    golden_inputs, golden_hashes = golden_hash_entries(
        boxes, base_template_ids, golden_machines_by_box, golden_bundle,
        box_password, box_username, os.environ.get("TF_VAR_ssh_public_key", ""),
        apt_cache, unbooted=unbooted)

    frozen = frozen_state(comp_dir)
    frozen_keep = set()
    if frozen:
        # Goldens now: config-class drift refuses BEFORE phase 1 destroys anything.
        # The engine's gate still runs at phase 2 (its inputs are computed there),
        # likewise before any engine destruction. Code-only drift keeps the golden:
        # phase1_destroy_waves must not rebuild what the gate said to proceed on.
        frozen_hashes = (frozen.get("hashes") or {})
        for name in golden_hashes:
            stored_inputs = (frozen_hashes.get("golden") or {}).get(name, {}).get("inputs") or {}
            drift = golden_freeze_gate(name, stored_inputs, golden_inputs[name],
                                       frozen.get("frozen_at"), golden_bundle)
            if drift["code"]:
                frozen_keep.add(name)

    teams_json_src = {
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in teams.items()
    }
    if placement:
        # terraform's per-slot resources key on this: which host builds each team.
        for team_key in teams_json_src:
            teams_json_src[team_key]["slot"] = placement["team_slots"][team_key]
    teams_json = json.dumps(teams_json_src)
    boxes_json = json.dumps(boxes)

    update_env({
        "TF_VAR_teams": teams_json,
        "TF_VAR_boxes_per_team": boxes_json,
        "TF_VAR_box_password": box_password,
        "TF_VAR_box_username": box_username,
        "TF_VAR_scoring_vm_id": str(engine_vmid),
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
    tf_dir = ensure_terraform_workdir(comp_dir)

    # Stale-state guard: the per-competition terraform workdir carries state from
    # wherever the LAST deploy ran. Pointing terraform at a different host (or engine
    # vmid) makes it "reconcile" that state against the new endpoint and destroy
    # whatever now sits at the old vmid there (2026-09-24: a realm run deleted that
    # host's existing 1090 engine because a dead primary attempt left 1090 in this
    # comp's state). Refuse unless the recorded host and vmid agree.
    if from_phase <= 2 and (tf_dir / "terraform.tfstate").exists() and previous_state:
        cur_endpoint = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
        prev_endpoint = (previous_state.get("deployed_endpoint") or "").rstrip("/")
        prev_engine = previous_state.get("scoring_vm_id")
        host_mismatch = bool(prev_endpoint) and prev_endpoint != cur_endpoint
        vmid_mismatch = prev_engine is not None and int(prev_engine) != engine_vmid
        if host_mismatch or vmid_mismatch:
            raise SystemExit(
                f"  ERROR: {tf_dir}/terraform.tfstate holds state from a deploy against "
                f"{prev_endpoint or 'another host'} (engine vmid {prev_engine}); this run "
                f"targets {cur_endpoint} (engine vmid {engine_vmid}). Terraform would "
                f"reconcile the stale state and destroy whatever sits at those resources "
                f"on the new host. Destroy this competition first: python3 "
                f"destroy-competition.py --competition {comp_name} --yes"
            )

    raw_key = os.environ["TF_VAR_ssh_private_key_path"]
    ssh_key_abs = raw_key if os.path.isabs(raw_key) else str((Path("terraform") / raw_key).resolve())
    # Engine mgmt IP: static by default. The DHCP engine rebooted onto a different
    # address mid-event while terraform's saved output — which deploy/verify/
    # credentials all consume — stayed stale (shakedown-5x4: .221→.243→.233).
    # An explicit TF_VAR_engine_mgmt_ip="" keeps the old DHCP behavior; the
    # chosen value is also exported for template_ops (build-VM ipconfig0).
    engine_mgmt_ip = os.environ.get("TF_VAR_engine_mgmt_ip")
    if engine_mgmt_ip is None:
        engine_mgmt_ip = DEFAULT_ENGINE_MGMT_IP
        print(f"  Engine mgmt IP: static {engine_mgmt_ip} (default — override "
              f"TF_VAR_engine_mgmt_ip, set '' for DHCP)")
        os.environ["TF_VAR_engine_mgmt_ip"] = engine_mgmt_ip
    # Default the gateway whenever the engine mgmt IP is static (live-found 2026-09-29:
    # an explicitly-set mgmt IP skipped this branch on the .150 env, and the engine
    # template build's ipconfig0 went out with an empty gw= — PVE 400 "Parameter
    # verification failed").
    if not os.environ.get("TF_VAR_engine_mgmt_gw"):
        os.environ["TF_VAR_engine_mgmt_gw"] = DEFAULT_ENGINE_MGMT_GW
    tfvars = {
        # teams_json_src carries the placement slot per team (multi-node) — tfvars
        # outranks the env, so this is the copy terraform actually reads.
        "teams": teams_json_src,
        "boxes_per_team": boxes,
        "box_password": box_password,
        "box_username": box_username,
        "event_name": name,
        "scoring_vm_id": engine_vmid,
        "ssh_private_key_path": ssh_key_abs,
        # M3.3 two-apply: apply #1 (phase 2) builds the engine + bridges with an empty
        # team_box for_each; apply #2 (phase 4) flips this to true once the golden
        # templates exist. team_nics/reboot keep their full-teams config in apply #1 so
        # the engine already has a NIC on every bridge for the golden plant.
        "build_team_boxes": False,
        "golden_template_ids": [],
        # M4: apply #2 rewrites tfvars with this intact — a resume that skips phase 2
        # must not let the engine clone source fall back to the base image (which would
        # replace the engine with an unbootstrapped full clone mid-pipeline).
        "engine_clone_id": int(state.get("engine_template_vmid") or 0),
        # Portable-node mode (realm): static engine mgmt IP instead of agent discovery.
        # Persisted via tfvars so resumes don't depend on the env var being re-exported.
        "engine_mgmt_ip": engine_mgmt_ip,
        "engine_mgmt_gw": os.environ.get("TF_VAR_engine_mgmt_gw", ""),
    }
    if placement:
        # Multi-node: per-slot satellite providers and the engine's jump routes.
        tfvars["satellites"] = satellite_tfvars(placement)
        tfvars["satellite_routes"] = satellite_routes_for(placement)
    tfvars_path = tf_dir / "terraform.tfvars.json"
    # Carries TF_VAR_box_password + the per-team passwords.
    write_text_atomic(tfvars_path, json.dumps(tfvars, indent=2))

    if from_phase <= 2:
        if placement:
            from config_ops import preflight_gates_multinode
            preflight_gates_multinode(comp_dir, boxes, teams, engine_vmid, placement,
                                      engine_mgmt_ip=engine_mgmt_ip,
                                      check_free=not resuming)
        else:
            preflight_gates(comp_dir, boxes, number_of_teams, teams=teams,
                            engine_vmid=engine_vmid, check_free=not resuming,
                            engine_mgmt_ip=engine_mgmt_ip)

    if not assume_yes and not resuming:
        if not confirm_deploy(name, scenario, difficulty, teams, boxes):
            print("  Deployment cancelled.")
            return None

    node = os.environ["TF_VAR_proxmox_node"]
    all_targets = enumerate_targets(teams, boxes, placement=placement, default_node=node)
    persist_targets(comp_dir, all_targets, boxes)
    # Unmanaged boxes (pfSense/appliances) get no plant/repair/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    managed_targets = [t for t in all_targets if not is_unmanaged(t["box"])]
    linux_targets = [t for t in managed_targets if not is_windows_template(t["box"]["template"])]
    windows_targets = [t for t in managed_targets if is_windows_template(t["box"]["template"])]

    return DeployContext(
        comp_dir=comp_dir,
        comp_name=comp_name,
        state_path=state_path,
        from_phase=from_phase,
        resuming=resuming,
        assume_yes=assume_yes,
        name=name,
        scenario=scenario,
        box_username=box_username,
        credlist_usernames=credlist_usernames,
        nakon_jobs=nakon_jobs,
        apt_cache=apt_cache,
        injects=injects,
        packet_pw=packet_pw,
        state=state,
        teams=teams,
        number_of_teams=number_of_teams,
        admin_password=admin_password,
        postgres_password=postgres_password,
        redis_password=redis_password,
        box_password=box_password,
        box_creds=box_creds,
        domain_creds=domain_creds,
        inject_password=inject_password,
        placement=placement,
        node=node,
        engine_vmid=engine_vmid,
        engine_mgmt_ip=engine_mgmt_ip,
        boxes=boxes,
        boxes_by_name={b["name"]: b for b in boxes},
        unbooted=unbooted,
        nakon_config_path=nakon_config_path,
        golden_config_path=golden_config_path,
        repair_config_path=repair_config_path,
        final_config_path=final_config_path,
        golden_inputs=golden_inputs,
        golden_hashes=golden_hashes,
        frozen_keep=frozen_keep,
        tf_dir=tf_dir,
        tfvars_path=tfvars_path,
        tfvars=tfvars,
        ssh_key_abs=ssh_key_abs,
        all_targets=all_targets,
        managed_targets=managed_targets,
        linux_targets=linux_targets,
        windows_targets=windows_targets,
    )


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
           team_node=None, engine_node=None, force_from_phase=False):
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
    from deploy_phases import phase1_cleanup, phase2_engine_template

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

    current_phase = max(ctx.from_phase, 1)
    try:
        if ctx.from_phase <= 1:
            current_phase = 1
            phase1_cleanup(ctx)
            ctx.checkpoint(1)
        else:
            print("[1/7] Skipped (resume) — leaving existing VMs/bridges in place.")

        if ctx.from_phase <= 2:
            current_phase = 2
            phase2_engine_template(ctx)
            ctx.checkpoint(2)
        else:
            print("[2/7] Skipped (resume) — not re-running terraform apply.")

        tf_ctx = read_terraform_ctx(ctx.comp_dir)
        ctx.tf_ctx = tf_ctx
        ctx.ssh_key = Path(tf_ctx["ssh_key_path"])
        ctx.scoring_user = os.environ["TF_VAR_vm_username"]
        ctx.scoring_ip = tf_ctx["scoring_engine_ip"]

        if ctx.from_phase <= 3:
            current_phase = 3
            # M4: the deployed engine is a linked clone of the engine template — the
            # heavy bootstrap ran once on the template build VM. Per-deploy state is
            # applied fresh here: .env (BEFORE compose up, so the fresh postgres volume
            # initializes with this competition's credentials), fresh-volume compose up
            # (an empty scoring DB every run), and the cacher check.
            print("[3/7] Preparing scoring engine from template (fresh volumes, event.conf)...")
            with timed(ctx.comp_dir, 3, "engine_from_template"):
                prepare_engine_from_template(ctx.tf_ctx, ctx.postgres_password, ctx.redis_password)

            print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
            with timed(ctx.comp_dir, 3, "push_event_conf"):
                push_event_conf(ctx.comp_dir, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.name,
                                inject_password=ctx.inject_password, admin_password=ctx.admin_password,
                                postgres_password=ctx.postgres_password, redis_password=ctx.redis_password,
                                box_creds=ctx.box_creds,
                                extra_credlists=({"domain": ctx.domain_creds}
                                                 if ctx.domain_creds else None))
            ensure_nat_forwarding(ctx.tf_ctx)
            ctx.checkpoint(3)
        else:
            print("[3/7] Skipped (resume).")

        if ctx.from_phase <= 4:
            current_phase = 4
            print("[4/7] Building the golden set (plant once per box type, convert to template)...")
            # M4 hash gate: a converted golden whose stored hash differs is rebuilt —
            # unless the competition is frozen, in which case config drift hard-fails
            # (frozen_gate) and code-only drift reuses the template with a warning.
            # Matching templates reach build_golden_set and short-circuit the build.
            stored = load_template_hashes(ctx.comp_dir)

            # Slot-0 goldens only exist when the engine node actually hosts teams —
            # an all-satellite spread leaves the engine with no local bridges to
            # anchor them on (their vmbr<id> lives on the satellite's host).
            engine_has_teams = bool(
                ctx.placement is None
                or ctx.placement["team_nodes"] and any(
                    n == ctx.placement["engine_node"]
                    for n in ctx.placement["team_nodes"].values()))
            golden_ids = {}
            if engine_has_teams:
                golden_rebuild_gate(ctx.comp_dir, ctx.node, 0, ctx.boxes, ctx.engine_vmid, stored,
                                    ctx.golden_hashes, ctx.golden_inputs, ctx.comp_name)
                golden_ids = build_golden_set(ctx.node, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.comp_dir,
                                              ctx.engine_vmid, ctx.box_password,
                                              ctx.golden_config_path, ctx.ssh_key, ctx.scoring_user,
                                              ctx.scoring_ip, jobs=ctx.nakon_jobs,
                                              golden_hashes=ctx.golden_hashes, unbooted=ctx.unbooted)
            golden_ids_by_slot = {0: golden_ids}
            if ctx.placement and ctx.placement["satellites"]:
                # Satellite goldens: identical planted content (same hashes), built ON
                # each satellite from its own templates, at the slot's anchor subnet —
                # linked clones can't cross hosts on separate storages. The plant rides
                # the engine's gateway path; the jump routes + SNATs it there.
                for sat in ctx.placement["satellites"]:
                    sat_rec = record_of(ctx.placement, sat["name"])
                    slot = sat["slot"]
                    anchor = sat["anchor_identifier"]
                    golden_rebuild_gate(ctx.comp_dir, sat_rec.node, slot, ctx.boxes, ctx.engine_vmid,
                                        stored, ctx.golden_hashes, ctx.golden_inputs, ctx.comp_name)
                    slot_config = generate_slot_golden_config(ctx.comp_dir, ctx.boxes,
                                                              ctx.unbooted, anchor, slot)
                    print(f"  Golden set on satellite '{sat['name']}' (slot {slot}, "
                          f"anchor 192.168.{anchor}.0/24)...")
                    with timed(ctx.comp_dir, 4, "golden_build", f"sat{slot}"):
                        golden_ids_by_slot[slot] = build_golden_set(
                            sat_rec.node, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.comp_dir, ctx.engine_vmid,
                            ctx.box_password, slot_config, ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip,
                            jobs=ctx.nakon_jobs, golden_hashes=ctx.golden_hashes,
                            unbooted=ctx.unbooted, slot=slot, anchor_identifier=anchor)
            ctx.state["golden_template_ids"] = golden_ids
            ctx.state["golden_ids_by_slot"] = {str(k): v for k, v in golden_ids_by_slot.items()}
            ctx.state["golden_hashes"] = ctx.golden_hashes
            save_template_hashes(ctx.comp_dir, golden={
                name: {"hash": ctx.golden_hashes[name], "inputs": ctx.golden_inputs[name]}
                for name in ctx.golden_hashes})
            ctx.save_state()

            print("[4b/7] Terraform apply #2: every team as a linked clone of the golden set...")
            # Positional per box: two box types may share one base template, so a name-keyed
            # map would silently cross-wire golden disks (live-found 2026-09-24: web01
            # clones came from golden-db01's disk).
            # Length must equal boxes_per_team (positional). Unmanaged boxes (pfSense) have
            # no golden — terraform's team_box unmanaged branch clones them from their own
            # base template instead, so their slot here carries that base template's vmid
            # — ON THE TEAM'S OWN NODE for satellite slots (their clone must resolve
            # locally).
            _tmap = _template_vmid_map(ctx.node)
            ctx.tfvars["golden_template_ids"] = [
                int(_tmap[b["template"]]) if is_unmanaged(b)
                # 0 placeholder when the engine hosts no teams (all-satellite spread):
                # the slot-0 team_box for_each is empty then, so nothing reads it.
                else int(golden_ids.get(b["name"]) or 0)
                for b in ctx.boxes
            ]
            if ctx.placement and ctx.placement["satellites"]:
                by_slot_ids = {str(k): v for k, v in golden_ids_by_slot.items()}
                ctx.tfvars["golden_template_ids_by_slot"] = {}
                for sat in ctx.placement["satellites"]:
                    slot = sat["slot"]
                    sat_node = record_of(ctx.placement, sat["name"]).node
                    stmap = _template_vmid_map(sat_node)
                    slot_ids = by_slot_ids.get(str(slot)) or {}
                    ctx.tfvars["golden_template_ids_by_slot"][str(slot)] = [
                        int(stmap[b["template"]]) if is_unmanaged(b) else int(slot_ids[b["name"]])
                        for b in ctx.boxes
                    ]
            ctx.tfvars["build_team_boxes"] = True
            write_text_atomic(ctx.tfvars_path, json.dumps(ctx.tfvars, indent=2))
            tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
            tf_cwd = str(terraform_dir(ctx.comp_dir))
            # Linked clones are seconds each (no bulk disk copy); the budget is for the
            # cloud-init-adjacent API waits bpg does per box, not for storage.
            apply_timeout = 600 + 300 * len(ctx.all_targets)
            with timed(ctx.comp_dir, 4, "terraform_apply_teams"):
                run_terraform(["apply", "-auto-approve", "-parallelism=1"],
                              cwd=tf_cwd, env=tf_env, timeout=apply_timeout)

            # Apply #2 (re-)created the team boxes: any surviving .postclone-swept
            # marker describes the OLD clones, and a phase-5 resume would skip the
            # repair sweep/credlist/shim on the fresh ones (hit twice on
            # shakedown-5x4; the workaround was deleting the marker by hand).
            (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)

            win_results = run_concurrent(ctx.windows_targets, partial(boot_win, ctx), max_workers=4)
            for t, r in zip(ctx.windows_targets, win_results):
                if isinstance(r, Exception):
                    raise r

            with timed(ctx.comp_dir, 4, "wait_boxes_ssh"):
                wait_for_boxes_ssh(ctx.tf_ctx, ctx.all_targets, timeout=300)
            with timed(ctx.comp_dir, 4, "wait_cloud_init"):
                wait_for_cloud_init(ctx.tf_ctx, ctx.all_targets, timeout=240)
            with timed(ctx.comp_dir, 4, "setup_auth"):
                setup_ubuntu_auth(ctx.linux_targets, ctx.tf_ctx)
            with timed(ctx.comp_dir, 4, "fix_dns"):
                fix_dns_on_boxes(ctx.linux_targets, ctx.tf_ctx)
            with timed(ctx.comp_dir, 4, "prep_apt"):
                prep_apt_on_boxes(ctx.linux_targets, ctx.tf_ctx, use_proxy=ctx.apt_cache)

            print(f"  Snapshotting all boxes as '{SNAP_BASE}' (pre-sweep restore point)...")
            run_concurrent(ctx.all_targets, partial(snap_base, ctx), max_workers=4)
            ctx.checkpoint(4)
        else:
            print("[4/7] Skipped (resume).")

        if ctx.from_phase <= 5:
            current_phase = 5
            swept_marker = ctx.comp_dir / ".postclone-swept"
            if swept_marker.exists():
                print("[5/7] Resume marker present — post-clone sweep already done; skipping")
            else:
                print("[5/7] Repair-stage sweep (sshd/sudoers) on every team box...")
                ensure_nat_forwarding(ctx.tf_ctx)
                repair_machines = json.loads(ctx.repair_config_path.read_text())["machines"]
                if repair_machines:
                    repair_bundle = build_nakon_bundle(ctx.repair_config_path)
                    # strict=False: the sweep re-runs on every resume, and one flaky plant
                    # must not kill the sweep after 98% of it landed. The golden plant
                    # (phase 4) is the strict, authoritative one.
                    with timed(ctx.comp_dir, 5, "nakon", f"repair x{len(repair_machines)}"):
                        result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, repair_bundle,
                                           ctx.repair_config_path,
                                           timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(repair_machines)),
                                           strict=False, jobs=ctx.nakon_jobs)
                    # Stage-prefixed tally (verify's plant-integrity line names the pass)
                    # and the structured coverage record (verify's plant-coverage gate).
                    ctx.state["nakon_failed_steps"] = [f"repair: {line}" for line in result.failed[:20]]
                    record_stage_coverage(ctx.state, repair_machines, result, ctx.save_state)
                else:
                    print("  No repair-stage configurations in this lineup — sweep skipped")
                # fix_services right after the repair pass: it un-wedges sshd (the ssh-*
                # configs above restart sshd and can trip the start-limit), creates the
                # credlist OS accounts, and binds the services the golden stage installed.
                # It must run BEFORE domains (nakon joins over SSH) and before the final
                # pass (whose disruptive configs would break its apt/SSH needs).
                with timed(ctx.comp_dir, 5, "fix_services"):
                    fix_services_on_boxes(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx, box_creds=ctx.box_creds)
                    if compfile_flag(ctx.comp_dir / "Compfile", "alpine_services"):
                        # Clones usually inherit the shim-installed services from the
                        # golden disk; this pass is the idempotent safety net.
                        ensure_alpine_services(ctx.comp_dir, ctx.linux_targets, ctx.tf_ctx)
                swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            ctx.checkpoint(5)
        else:
            print("[5/7] Skipped (resume).")

        if ctx.from_phase <= 6:
            current_phase = 6
            print("  Configuring Windows AD domains (if any)...")
            with timed(ctx.comp_dir, 6, "domains"):
                deploy_domain_configs(ctx.teams, ctx.boxes, ctx.comp_dir, ctx.nakon_config_path,
                                      ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, ctx.box_password)

            # Final-stage pass AFTER domains: the disruptive configs break the DNS/apt the
            # Linux realmd joins need, and the boot-hostile configs would brick any member
            # box's domain-join reboot. From here on the boxes are in their as-started
            # competition flavor — nothing downstream reboots them or needs apt/DNS.
            final_machines = json.loads(ctx.final_config_path.read_text())["machines"]
            if final_machines:
                print(f"  Final-stage pass (disruption + boot-hostile) on {len(final_machines)} machine(s)...")
                ensure_nat_forwarding(ctx.tf_ctx)
                final_bundle = build_nakon_bundle(ctx.final_config_path)
                with timed(ctx.comp_dir, 6, "nakon", f"final x{len(final_machines)}"):
                    result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, final_bundle,
                                       ctx.final_config_path,
                                       timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(final_machines)),
                                       strict=False, jobs=ctx.nakon_jobs)
                # Merge, not overwrite: a failure in BOTH passes must keep the repair
                # tally (phase 6 used to erase it by writing only on failure).
                merged = list(ctx.state.get("nakon_failed_steps") or [])
                merged += [f"final: {line}" for line in result.failed[:20]]
                seen = set()
                ctx.state["nakon_failed_steps"] = [x for x in merged if not (x in seen or seen.add(x))][:40]
                # Save unconditionally: _record_coverage may have CLEARED a stale
                # coverage entry on this fully-green pass, and a clear that never
                # reaches disk leaves verify's coverage gate red (see
                # record_stage_coverage — the ff9b19f bug, re-created).
                record_stage_coverage(ctx.state, final_machines, result, ctx.save_state)

            if compfile_flag(ctx.comp_dir / "Compfile", "team_beacons"):
                print("  Planting team beacons (hunt artifacts)...")
                with timed(ctx.comp_dir, 6, "beacons"):
                    plant_team_beacons(ctx.teams, ctx.boxes, ctx.tf_ctx, box_username=ctx.box_username,
                                       box_password=ctx.box_password)

            print(f"  Snapshotting all boxes as '{SNAP_READY}' (as-delivered restore point)...")
            run_concurrent(ctx.all_targets, partial(snap_ready, ctx), max_workers=4)
            ctx.checkpoint(6)
        else:
            print("[6/7] Skipped (resume).")

        if ctx.from_phase <= 7:
            current_phase = 7
            print("[7/7] Seeding competition and creating injects...")

            with timed(ctx.comp_dir, 7, "wait_quotient_http"):
                wait_for_http(f"http://{ctx.scoring_ip}/api/login", timeout=120)

            quotient_ctx = {
                "teams": {team_key: team_data["identifier"] for team_key, team_data in ctx.teams.items()},
                "quotient_admin_password": ctx.admin_password,
            }

            if not ctx.state.get("seeded"):
                print("  Seeding teams and starting the competition clock...")
                with timed(ctx.comp_dir, 7, "seed_teams"):
                    seed_teams(ctx.scoring_ip, quotient_ctx)
                ctx.state["seeded"] = True
                ctx.save_state()
            else:
                print("  Teams already seeded (resume) — skipping.")

            if not ctx.state.get("engine_unpaused"):
                # The unpause POST isn't idempotent, so a resume in the crash
                # window between POST and flag-save asks the engine first and
                # re-POSTs only when it really is still paused.
                paused = engine_paused(ctx.scoring_ip, quotient_ctx)
                if paused is False:
                    print("  Engine reports itself unpaused — recording and skipping.")
                else:
                    with timed(ctx.comp_dir, 7, "unpause_engine"):
                        unpause_engine(ctx.scoring_ip, quotient_ctx)
                ctx.state["engine_unpaused"] = True
                ctx.save_state()
            else:
                print("  Engine already unpaused (resume) — skipping.")

            if ctx.injects and not ctx.state.get("injects_created"):
                print(f"  Creating {len(ctx.injects)} inject(s)...")
                resolve_inject_times(ctx.injects)
                with timed(ctx.comp_dir, 7, "create_injects", f"x{len(ctx.injects)}"):
                    _created, failed_titles = create_injects(ctx.scoring_ip, ctx.admin_password, ctx.injects)
                if failed_titles:
                    print(f"  WARNING: {len(failed_titles)} inject(s) failed to create: "
                          f"{', '.join(failed_titles)} — re-run --from-phase 7 to retry "
                          f"(existing injects are deduped)")
                else:
                    ctx.state["injects_created"] = True
                    ctx.save_state()
            elif ctx.injects:
                print("  Injects already created (resume) — skipping.")
            ctx.checkpoint(7)
        else:
            print("[7/7] Skipped (resume).")
    except BaseException as e:
        print(f"\n  [!] Deploy failed during phase {current_phase} of '{ctx.comp_name}'.")
        resume_phase = current_phase
        if current_phase >= 2 and "already exists" in str(e).lower():
            resume_phase = 1
            print("      This looks like a Proxmox/Terraform state mismatch (something the "
                  "prior attempt created still exists, but Terraform's state doesn't know about "
                  "it) — resuming from the failed phase would just hit the same error again.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {ctx.comp_name} --from-phase {resume_phase} --yes")
        raise

    cred_lines = [
        f"# Credentials for {ctx.name} — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scoreboard:  http://{ctx.scoring_ip}",
        f"admin  {ctx.admin_password}",
    ]
    if ctx.packet_pw:
        cred_lines.append("# box credentials below are the packet-published defaults "
                          "(passwords.json) — teams rotate them at minute zero")
    if ctx.inject_password:
        cred_lines.append(f"inject  {ctx.inject_password}")
    for team_name, team_data in ctx.teams.items():
        cred_lines.append(f"{team_name}  {team_data['password']}  (192.168.{team_data['identifier']}.0/24)")
    cred_lines.append(f"box-login ({ctx.box_username})  {ctx.box_password}")
    for user, pw in ctx.box_creds.items():
        cred_lines.append(f"box-credlist-{user}  {pw}")
    for user, pw in (ctx.domain_creds or {}).items():
        cred_lines.append(f"box-credlist-domain-{user}  {pw}")
    cred_path = ctx.comp_dir / "credentials.txt"
    # The operator/packet-facing credential file — same 0600-at-creation rule.
    write_text_atomic(cred_path, "\n".join(cred_lines) + "\n")

    print_timing_summary(ctx.comp_dir)

    print(f"\n{'='*60}")
    print(f"  {ctx.name} is live")
    print(f"{'='*60}")
    print(f"Scenario: {ctx.scenario}")
    print(f"Saved to: competitions/{ctx.comp_name}/  (credentials.txt, mode 0600)")
    print(f"\nScoreboard:    http://{ctx.scoring_ip}")
    print(f"Admin login:   admin / {ctx.admin_password}")
    if ctx.inject_password:
        print(f"Inject login:  inject / {ctx.inject_password}   ({len(ctx.injects)} inject(s) loaded)")
    print("\nTeam logins:")
    for team_name, team_data in ctx.teams.items():
        print(f"  {team_name} / {team_data['password']}  (subnet 192.168.{team_data['identifier']}.0/24)")
    print(f"\nBox login:     {ctx.box_username} / {ctx.box_password}  (every team box)")
    print("Box credlist:  " + ", ".join(f"{u}/{p}" for u, p in ctx.box_creds.items()))
    print(f"\nScoring engine SSH: ssh -i {ctx.ssh_key} {ctx.scoring_user}@{ctx.scoring_ip}")
    print(f"{'='*60}")


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
               force_from_phase=args.force_from_phase)
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
           force_from_phase=args.force_from_phase)


if __name__ == "__main__":
    main()
