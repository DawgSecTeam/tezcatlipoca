"""Orchestrator: seven-phase deploy and CLI."""

import fcntl
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from beacon_ops import plant_team_beacons
from config_ops import (
    _prompt_difficulty,
    collect_boxes,
    collect_teams,
    collect_users_config,
    confirm_deploy,
    destroy_bridge_if_exists,
    load_boxes,
    load_injects,
    load_packet_passwords,
    load_previous_competitions,
    preflight_gates,
    random_password,
    resolve_inject_times,
    update_env,
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
from engine_ops import (bootstrap_scoring_engine, ensure_nat_forwarding,
                        prepare_engine_from_template, push_event_conf)
from golden_ops import (_is_template, _quote_sshkeys, _template_vmid_map,
                        build_golden_set, unbooted_golden_boxes)
from hardening_ops import (_APT_PREP_BODY, _apt_prep_script, ensure_alpine_services,
                           fix_dns_on_boxes, fix_services_on_boxes, prep_apt_on_boxes,
                           setup_ubuntu_auth)
from jump_ops import build_jump_vms
from nakon_ops import (acquire_engine_lock, build_nakon_bundle, generate_nakon_config,
                       generate_slot_golden_config, generate_stage_configs, run_nakon)
from nodes_ops import (activate_placement, golden_vmid_for_slot,
                       record_of, resolve_placement, satellite_routes_for,
                       satellite_tfvars)
from quotient.setup import create_injects, engine_paused, seed_teams, unpause_engine
from range_ops import (destroy_vm_if_exists, ensure_terraform_workdir, enumerate_targets,
                       persist_targets, proxmox_api, take_snapshot,
                       terraform_dir, terraform_plugin_cache_dir)
from routing_ops import verify_satellite_routing
from ssh_ops import (forget_engine_host_key, read_terraform_ctx, wait_for_boxes_ssh,
                     wait_for_cloud_init, wait_for_http, wait_for_ssh)
from template_ops import (
    build_engine_template,
    code_hash,
    destroy_engine_template,
    engine_hash_inputs,
    engine_template_vmid,
    find_engine_template,
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
from utils import (compfile_flag, compfile_value, is_unmanaged, load_compfile, load_users_config,
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
    here; verify maps '{box}-golden'-style failures onto every team copy of the box."""
    if result is None or not getattr(result, "machines", None):
        return  # no --json outcome (older nakon) — coverage falls back to the tally
    failed = result.failed_configs()
    if not failed:
        return
    cov = state.setdefault("plant_coverage_failed", {})
    for m in stage_machines:
        bad = failed.get(m["name"])
        if not bad:
            continue
        if bad == {"<machine failed before any step>"}:
            bad = {(c if isinstance(c, str) else c["name"]) for c in m["configurations"]}
        cov[m["name"]] = sorted(set(cov.get(m["name"]) or []) | set(bad))


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


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1, scoring_vmid=None,
           team_node=None, engine_node=None):
    """Run the seven-phase deploy for one competition; from_phase > 1 resumes from .deploy_state.json.

    scoring_vmid overrides the scoring-engine VMID (default 1000) so several
    competitions can run concurrently on one node; it is persisted to
    .deploy_state.json and reused on resume.
    team_node (--team-node id=NODE,...) and engine_node (--engine-node NAME) pin the
    multi-node placement when nodes.json exists; without them teams are placed by
    capacity-fill."""
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

    def _save_state():
        # Atomic rename: .deploy_state.json holds the only copy of the box
        # passwords — a torn write here bricks both resume and redeploy.
        tmp_path = state_path.with_name(state_path.name + ".tmp")
        tmp_path.write_text(json.dumps(state, indent=2))
        os.replace(tmp_path, state_path)
        try:
            os.chmod(state_path, 0o600)
        except OSError:
            pass

    def checkpoint(n):
        state["last_phase"] = n
        _save_state()

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
        _save_state()
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
        _save_state()

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
    _save_state()
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
        comp_dir, teams, boxes, box_username=box_username, unbooted=unbooted)

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
    golden_inputs, golden_hashes = {}, {}
    for b in boxes:
        if b["name"] in unbooted:
            inputs = {"config": {"base_template_vmid": base_template_ids.get(b["template"]),
                                  "disk_gb": b.get("disk_gb"), "golden": "unbooted"},
                      "code": {"build_golden_set": code_hash(build_golden_set)}}
            golden_inputs[b["name"]] = inputs
            golden_hashes[b["name"]] = hash_from_inputs(inputs)
            continue
        if is_unmanaged(b):
            # Unmanaged boxes (pfSense) get no golden machine and no golden plant —
            # terraform clones them straight from their own base template. Their slot
            # still needs a hash entry so the loop below and phase 1's wave logic see
            # a stable value (live-found 2026-09-29: first unmanaged-carried bundle
            # KeyError'd here because no pfsense comp had run through the M4 hash path).
            inputs = {"config": {"base_template_vmid": base_template_ids.get(b["template"]),
                                 "disk_gb": b.get("disk_gb"), "golden": "unmanaged"},
                      "code": {"build_golden_set": code_hash(build_golden_set)}}
            golden_inputs[b["name"]] = inputs
            golden_hashes[b["name"]] = hash_from_inputs(inputs)
            continue
        inputs = golden_hash_inputs(
            b, golden_machines_by_box[b["name"]],
            golden_payload_hash(golden_bundle, b["name"]),
            box_password, box_username, os.environ.get("TF_VAR_ssh_public_key", ""),
            apt_cache)
        inputs["config"]["base_template_vmid"] = base_template_ids.get(b["template"])
        inputs["code"]["build_golden_set+apt_prep"] = code_hash(
            build_golden_set, _APT_PREP_BODY, _apt_prep_script)
        golden_inputs[b["name"]] = inputs
        golden_hashes[b["name"]] = hash_from_inputs(inputs)

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

    (comp_dir / "teams.json").write_text(teams_json)
    os.chmod(comp_dir / "teams.json", 0o600)

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
    tfvars_path.write_text(json.dumps(tfvars, indent=2))
    os.chmod(tfvars_path, 0o600)

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
            return

    node = os.environ["TF_VAR_proxmox_node"]
    all_targets = enumerate_targets(teams, boxes, placement=placement, default_node=node)
    persist_targets(comp_dir, all_targets, boxes)
    # Unmanaged boxes (pfSense/appliances) get no plant/repair/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    managed_targets = [t for t in all_targets if not is_unmanaged(t["box"])]
    linux_targets = [t for t in managed_targets if not is_windows_template(t["box"]["template"])]
    windows_targets = [t for t in managed_targets if is_windows_template(t["box"]["template"])]

    current_phase = max(from_phase, 1)
    try:
        if from_phase <= 1:
            current_phase = 1
            print("[1/7] Cleaning up previous deployment (parallel; deletes are metadata-light "
                  "— the datastore-saturation hazard belongs to bulk clone writes, not deletes)...")
            comp_tags = {"tezcatlipoca", f"comp-{comp_name}"}
            legacy_clones = {}
            cloned_path = comp_dir / "cloned_vms.json"
            if cloned_path.exists():
                try:
                    legacy_clones = {int(v): str(k) for k, v in json.loads(cloned_path.read_text()).items()}
                except (ValueError, TypeError, OSError):
                    print("  WARNING: could not parse cloned_vms.json — relying on computed vmids")

            def _destroy_pool(vmid_map, destroy_node):
                workers = min(8, len(vmid_map)) or 1
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(_destroy_owned, destroy_node, vmid, name)
                               for vmid, name in sorted(vmid_map.items())]
                    for fut in futures:
                        fut.result()

            def _destroy_owned(destroy_node, vmid, vm_name):
                with timed(comp_dir, 1, "destroy_vm", vm_name):
                    legacy_name = vm_name if vm_name.startswith("golden-") else None
                    destroy_vm_if_exists(destroy_node, vmid, expect_tags=comp_tags,
                                         legacy_name=legacy_name)

            def _destroy_node_waves(destroy_node, node_targets, slot, extra_destroy=None):
                """One wave pair per hosting node, that node's VMs and its slot's
                golden span (multi-node) — the engine node keeps the historical
                slot-0 behavior."""
                try:
                    node_vms = proxmox_api("GET", f"/nodes/{destroy_node}/qemu")["data"]
                except Exception as e:
                    print(f"  WARNING: could not scan {destroy_node} for stranded "
                          f"clones ({e}) — proceeding")
                    node_vms = []
                wave1, wave2 = phase1_destroy_waves(
                    node_vms, node_targets, legacy_clones if slot == 0 else {},
                    engine_vmid, boxes, comp_tags,
                    lambda vid: _is_template(destroy_node, vid),
                    load_template_hashes(comp_dir), golden_hashes,
                    frozen_keep=frozen_keep, slot=slot, extra_destroy=extra_destroy)
                _destroy_pool(wave1, destroy_node)
                _destroy_pool(wave2, destroy_node)

            # Wave 1: every team box (the computed set covers ALL teams now that terraform
            # builds them) plus any legacy API clones from a pre-golden range. Linked
            # clones must die BEFORE their templates.
            _destroy_node_waves(node, [t for t in all_targets if t.get("node") == node], 0)
            if placement:
                for sat in placement["satellites"]:
                    sat_rec = record_of(placement, sat["name"])
                    _destroy_node_waves(
                        sat_rec.node,
                        [t for t in all_targets if t.get("node") == sat_rec.node],
                        sat["slot"], extra_destroy={sat["jump_vmid"]: f"jump-{comp_name}-{sat['slot']}"})
                    for team_key in placement["team_nodes"]:
                        if placement["team_nodes"][team_key] != sat["name"]:
                            continue
                        with timed(comp_dir, 1, "destroy_bridge", f"vmbr{teams[team_key]['identifier']}"):
                            destroy_bridge_if_exists(sat_rec.node, f"vmbr{teams[team_key]['identifier']}")
            for team_key, team in teams.items():
                if placement and placement["team_nodes"][team_key] != placement["engine_node"]:
                    continue  # satellite bridge — destroyed above on its own host
                with timed(comp_dir, 1, "destroy_bridge", f"vmbr{team['identifier']}"):
                    destroy_bridge_if_exists(node, f"vmbr{team['identifier']}")
            (comp_dir / ".postclone-swept").unlink(missing_ok=True)
            reset_domain_markers(comp_dir)
            time.sleep(5)
            checkpoint(1)
        else:
            print("[1/7] Skipped (resume) — leaving existing VMs/bridges in place.")

        if from_phase <= 2:
            current_phase = 2
            # --- M4: engine template lifecycle (build once per competition, reuse
            # across its test runs; rebuild only on config drift and never when frozen).
            main_tf_text = Path("terraform/main.tf").read_text()
            quotient_ref = compfile_value(comp_dir / "Compfile", "quotient_ref")
            engine_inputs = engine_hash_inputs(
                int(os.environ["TF_VAR_template_vm_id"]), quotient_ref,
                main_tf_text, bootstrap_scoring_engine)
            engine_hash = hash_from_inputs(engine_inputs)
            stored = load_template_hashes(comp_dir)
            tmpl = find_engine_template(node, engine_vmid)
            rebuild = True
            if tmpl:
                entry = stored.get("engine") or {}
                if stored_template_hash(node, tmpl) == engine_hash and entry.get("hash") == engine_hash:
                    print(f"  Engine template hash matches — reusing (vmid {tmpl})")
                    rebuild = False
                elif not frozen_gate(comp_dir, entry.get("inputs"), engine_inputs,
                                     "engine template"):
                    # frozen + code-only drift: frozen_gate warned; keep the frozen template.
                    rebuild = False
            if rebuild:
                if tmpl:
                    print("  Engine template hash differs — rebuilding...")
                    # The old template's only clone is the deployed engine, and the
                    # build VM needs the planned mgmt IP — on a phase-2 resume the old
                    # engine is still up (phase 1 was skipped), so destroy it here.
                    # Apply #1 recreates it as a linked clone of the new template.
                    comp_tags = {"tezcatlipoca", f"comp-{comp_name}"}
                    destroy_vm_if_exists(node, engine_vmid, expect_tags=comp_tags)
                    destroy_engine_template(node, engine_vmid, expect_tags={
                        "tezcatlipoca", f"comp-{comp_name}", "engine-template"})
                ctx_early = {
                    "ssh_key_path": ssh_key_abs,
                    "vm_username": os.environ["TF_VAR_vm_username"],
                    "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"]),
                }
                with timed(comp_dir, 2, "engine_template_build"):
                    tmpl, build_info = build_engine_template(
                        node, comp_dir, engine_vmid, int(os.environ["TF_VAR_template_vm_id"]),
                        ctx_early, postgres_password, redis_password, quotient_ref,
                        engine_hash, engine_inputs)
                state["engine_build_info"] = build_info
            save_template_hashes(comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
            state["engine_template_vmid"] = tmpl
            state["engine_template_hash"] = engine_hash
            _save_state()

            print("[2/7] Terraform apply #1 (engine from template + bridges; team boxes "
                  "come in apply #2)...")
            tfvars["engine_clone_id"] = tmpl
            tfvars_path.write_text(json.dumps(tfvars, indent=2))
            os.chmod(tfvars_path, 0o600)
            tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
            tf_cwd = str(terraform_dir(comp_dir))
            run_terraform(["init"], cwd=tf_cwd, env=tf_env, timeout=300)
            # team_box's for_each is empty here (build_team_boxes=false): only the engine
            # (a linked clone of the engine template — seconds, no bulk disk copy) and the
            # bridges are built, plus team_nics' netplan for every team bridge and the
            # cold-boot that surfaces the engine's team NICs.
            with timed(comp_dir, 2, "terraform_apply"):
                run_terraform(["apply", "-auto-approve", "-parallelism=1"], cwd=tf_cwd, env=tf_env, timeout=2400)

            apply_ctx = read_terraform_ctx(comp_dir)
            # Fresh engine VM => new host key; drop any stale pin so accept-new re-pins it.
            forget_engine_host_key(apply_ctx["scoring_engine_ip"])
            with timed(comp_dir, 2, "wait_engine_ssh", apply_ctx["scoring_engine_ip"]):
                wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                             apply_ctx["scoring_engine_ip"], timeout=300)
            if placement and placement["satellites"]:
                # Jump/router per satellite, then the fail-loud routing gate: nothing
                # downstream (satellite golden plants, nakon, scoring) works without
                # engine -> jump -> satellite-bridge paths.
                jump_ctx = {"ssh_key_path": ssh_key_abs,
                            "vm_username": os.environ["TF_VAR_vm_username"],
                            "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"])}
                with timed(comp_dir, 2, "jump_vms", f"x{len(placement['satellites'])}"):
                    build_jump_vms(placement, engine_vmid, jump_ctx, comp_name,
                                   engine_mgmt_ip,
                                   engine_mgmt_gw=os.environ.get("TF_VAR_engine_mgmt_gw",
                                                                 DEFAULT_ENGINE_MGMT_GW))
                with timed(comp_dir, 2, "routing_converge"):
                    verify_satellite_routing(placement, apply_ctx)
            # Record where this state's resources live, for the stale-state guard above.
            state["deployed_endpoint"] = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")
            checkpoint(2)
        else:
            print("[2/7] Skipped (resume) — not re-running terraform apply.")

        ctx = read_terraform_ctx(comp_dir)
        key = Path(ctx["ssh_key_path"])
        scoring_user = os.environ["TF_VAR_vm_username"]
        scoring_ip = ctx["scoring_engine_ip"]

        if from_phase <= 3:
            current_phase = 3
            # M4: the deployed engine is a linked clone of the engine template — the
            # heavy bootstrap ran once on the template build VM. Per-deploy state is
            # applied fresh here: .env (BEFORE compose up, so the fresh postgres volume
            # initializes with this competition's credentials), fresh-volume compose up
            # (an empty scoring DB every run), and the cacher check.
            print("[3/7] Preparing scoring engine from template (fresh volumes, event.conf)...")
            with timed(comp_dir, 3, "engine_from_template"):
                prepare_engine_from_template(ctx, postgres_password, redis_password)

            print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
            with timed(comp_dir, 3, "push_event_conf"):
                push_event_conf(comp_dir, teams, boxes, ctx, name,
                                inject_password=inject_password, admin_password=admin_password,
                                postgres_password=postgres_password, redis_password=redis_password,
                                box_creds=box_creds,
                                extra_credlists=({"domain": domain_creds}
                                                 if domain_creds else None))
            ensure_nat_forwarding(ctx)
            checkpoint(3)
        else:
            print("[3/7] Skipped (resume).")

        if from_phase <= 4:
            current_phase = 4
            print("[4/7] Building the golden set (plant once per box type, convert to template)...")
            # M4 hash gate: a converted golden whose stored hash differs is rebuilt —
            # unless the competition is frozen, in which case config drift hard-fails
            # (frozen_gate) and code-only drift reuses the template with a warning.
            # Matching templates reach build_golden_set and short-circuit the build.
            stored = load_template_hashes(comp_dir)

            def _golden_rebuild_gate(destroy_node, slot):
                for i, b in enumerate(boxes):
                    vid = golden_vmid_for_slot(engine_vmid, slot, i)
                    if not _is_template(destroy_node, vid):
                        continue
                    if stored_template_hash(destroy_node, vid) == golden_hashes[b["name"]]:
                        continue
                    entry = stored.get("golden", {}).get(b["name"]) or {}
                    if frozen_gate(comp_dir, entry.get("inputs"), golden_inputs[b["name"]],
                                   f"golden template for '{b['name']}'"):
                        print(f"  golden-{b['name']} hash differs — rebuilding...")
                        destroy_vm_if_exists(destroy_node, vid, expect_tags={
                            "tezcatlipoca", GOLDEN_TAG, f"comp-{comp_name}"},
                            legacy_name=f"golden-{b['name']}")

            # Slot-0 goldens only exist when the engine node actually hosts teams —
            # an all-satellite spread leaves the engine with no local bridges to
            # anchor them on (their vmbr<id> lives on the satellite's host).
            engine_has_teams = bool(
                placement is None
                or placement["team_nodes"] and any(
                    n == placement["engine_node"]
                    for n in placement["team_nodes"].values()))
            golden_ids = {}
            if engine_has_teams:
                _golden_rebuild_gate(node, 0)
                golden_ids = build_golden_set(node, teams, boxes, ctx, comp_dir, engine_vmid,
                                              box_password, golden_config_path, key, scoring_user,
                                              scoring_ip, jobs=nakon_jobs,
                                              golden_hashes=golden_hashes, unbooted=unbooted)
            golden_ids_by_slot = {0: golden_ids}
            if placement and placement["satellites"]:
                # Satellite goldens: identical planted content (same hashes), built ON
                # each satellite from its own templates, at the slot's anchor subnet —
                # linked clones can't cross hosts on separate storages. The plant rides
                # the engine's gateway path; the jump routes + SNATs it there.
                for sat in placement["satellites"]:
                    sat_rec = record_of(placement, sat["name"])
                    slot = sat["slot"]
                    anchor = sat["anchor_identifier"]
                    _golden_rebuild_gate(sat_rec.node, slot)
                    slot_config = generate_slot_golden_config(comp_dir, teams, boxes,
                                                              unbooted, anchor, slot)
                    print(f"  Golden set on satellite '{sat['name']}' (slot {slot}, "
                          f"anchor 192.168.{anchor}.0/24)...")
                    with timed(comp_dir, 4, "golden_build", f"sat{slot}"):
                        golden_ids_by_slot[slot] = build_golden_set(
                            sat_rec.node, teams, boxes, ctx, comp_dir, engine_vmid,
                            box_password, slot_config, key, scoring_user, scoring_ip,
                            jobs=nakon_jobs, golden_hashes=golden_hashes,
                            unbooted=unbooted, slot=slot, anchor_identifier=anchor)
            state["golden_template_ids"] = golden_ids
            state["golden_ids_by_slot"] = {str(k): v for k, v in golden_ids_by_slot.items()}
            state["golden_hashes"] = golden_hashes
            save_template_hashes(comp_dir, golden={
                name: {"hash": golden_hashes[name], "inputs": golden_inputs[name]}
                for name in golden_hashes})
            _save_state()

            print("[4b/7] Terraform apply #2: every team as a linked clone of the golden set...")
            # Positional per box: two box types may share one base template, so a name-keyed
            # map would silently cross-wire golden disks (live-found 2026-09-24: web01
            # clones came from golden-db01's disk).
            # Length must equal boxes_per_team (positional). Unmanaged boxes (pfSense) have
            # no golden — terraform's team_box unmanaged branch clones them from their own
            # base template instead, so their slot here carries that base template's vmid
            # — ON THE TEAM'S OWN NODE for satellite slots (their clone must resolve
            # locally).
            _tmap = _template_vmid_map(node)
            tfvars["golden_template_ids"] = [
                int(_tmap[b["template"]]) if is_unmanaged(b)
                # 0 placeholder when the engine hosts no teams (all-satellite spread):
                # the slot-0 team_box for_each is empty then, so nothing reads it.
                else int(golden_ids.get(b["name"]) or 0)
                for b in boxes
            ]
            if placement and placement["satellites"]:
                by_slot_ids = {str(k): v for k, v in golden_ids_by_slot.items()}
                tfvars["golden_template_ids_by_slot"] = {}
                for sat in placement["satellites"]:
                    slot = sat["slot"]
                    sat_node = record_of(placement, sat["name"]).node
                    stmap = _template_vmid_map(sat_node)
                    slot_ids = by_slot_ids.get(str(slot)) or {}
                    tfvars["golden_template_ids_by_slot"][str(slot)] = [
                        int(stmap[b["template"]]) if is_unmanaged(b) else int(slot_ids[b["name"]])
                        for b in boxes
                    ]
            tfvars["build_team_boxes"] = True
            tfvars_path.write_text(json.dumps(tfvars, indent=2))
            os.chmod(tfvars_path, 0o600)
            tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
            tf_cwd = str(terraform_dir(comp_dir))
            # Linked clones are seconds each (no bulk disk copy); the budget is for the
            # cloud-init-adjacent API waits bpg does per box, not for storage.
            apply_timeout = 600 + 300 * len(all_targets)
            with timed(comp_dir, 4, "terraform_apply_teams"):
                run_terraform(["apply", "-auto-approve", "-parallelism=1"],
                              cwd=tf_cwd, env=tf_env, timeout=apply_timeout)

            # Apply #2 (re-)created the team boxes: any surviving .postclone-swept
            # marker describes the OLD clones, and a phase-5 resume would skip the
            # repair sweep/credlist/shim on the fresh ones (hit twice on
            # shakedown-5x4; the workaround was deleting the marker by hand).
            (comp_dir / ".postclone-swept").unlink(missing_ok=True)

            def _boot_win(t):
                print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
                gw = f"192.168.{t['identifier']}.1"
                with timed(comp_dir, 4, "bootstrap_windows", t["ip"]):
                    bootstrap_windows_box(t.get("node", node), t["vmid"], t["ip"], gw,
                                          "8.8.8.8", box_password)

            win_results = run_concurrent(windows_targets, _boot_win, max_workers=4)
            for t, r in zip(windows_targets, win_results):
                if isinstance(r, Exception):
                    raise r

            with timed(comp_dir, 4, "wait_boxes_ssh"):
                wait_for_boxes_ssh(ctx, all_targets, timeout=300)
            with timed(comp_dir, 4, "wait_cloud_init"):
                wait_for_cloud_init(ctx, all_targets, timeout=240)
            with timed(comp_dir, 4, "setup_auth"):
                setup_ubuntu_auth(linux_targets, ctx)
            with timed(comp_dir, 4, "fix_dns"):
                fix_dns_on_boxes(linux_targets, ctx)
            with timed(comp_dir, 4, "prep_apt"):
                prep_apt_on_boxes(linux_targets, ctx, use_proxy=apt_cache)

            print(f"  Snapshotting all boxes as '{SNAP_BASE}' (pre-sweep restore point)...")

            def _snap_base(t):
                with timed(comp_dir, 4, "snapshot", t["vm_name"]):
                    take_snapshot(t.get("node", node), t["vmid"], SNAP_BASE,
                                  description="tezcatlipoca: booted, networked, pre-sweep")

            run_concurrent(all_targets, _snap_base, max_workers=4)
            checkpoint(4)
        else:
            print("[4/7] Skipped (resume).")

        if from_phase <= 5:
            current_phase = 5
            swept_marker = comp_dir / ".postclone-swept"
            if swept_marker.exists():
                print("[5/7] Resume marker present — post-clone sweep already done; skipping")
            else:
                print("[5/7] Repair-stage sweep (sshd/sudoers) on every team box...")
                ensure_nat_forwarding(ctx)
                repair_machines = json.loads(repair_config_path.read_text())["machines"]
                if repair_machines:
                    repair_bundle = build_nakon_bundle(repair_config_path)
                    # strict=False: the sweep re-runs on every resume, and one flaky plant
                    # must not kill the sweep after 98% of it landed. The golden plant
                    # (phase 4) is the strict, authoritative one.
                    with timed(comp_dir, 5, "nakon", f"repair x{len(repair_machines)}"):
                        result = run_nakon(key, scoring_user, scoring_ip, repair_bundle,
                                           repair_config_path,
                                           timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(repair_machines)),
                                           strict=False, jobs=nakon_jobs)
                    # Stage-prefixed tally (verify's plant-integrity line names the pass)
                    # and the structured coverage record (verify's plant-coverage gate).
                    state["nakon_failed_steps"] = [f"repair: {line}" for line in result.failed[:20]]
                    _record_coverage(state, repair_machines, result)
                    _save_state()
                else:
                    print("  No repair-stage configurations in this lineup — sweep skipped")
                # fix_services right after the repair pass: it un-wedges sshd (the ssh-*
                # configs above restart sshd and can trip the start-limit), creates the
                # credlist OS accounts, and binds the services the golden stage installed.
                # It must run BEFORE domains (nakon joins over SSH) and before the final
                # pass (whose disruptive configs would break its apt/SSH needs).
                with timed(comp_dir, 5, "fix_services"):
                    fix_services_on_boxes(comp_dir, linux_targets, ctx, box_creds=box_creds)
                    if compfile_flag(comp_dir / "Compfile", "alpine_services"):
                        # Clones usually inherit the shim-installed services from the
                        # golden disk; this pass is the idempotent safety net.
                        ensure_alpine_services(comp_dir, linux_targets, ctx)
                swept_marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            checkpoint(5)
        else:
            print("[5/7] Skipped (resume).")

        if from_phase <= 6:
            current_phase = 6
            print("  Configuring Windows AD domains (if any)...")
            with timed(comp_dir, 6, "domains"):
                deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path,
                                               key, scoring_user, scoring_ip, box_password)

            # Final-stage pass AFTER domains: the disruptive configs break the DNS/apt the
            # Linux realmd joins need, and the boot-hostile configs would brick any member
            # box's domain-join reboot. From here on the boxes are in their as-started
            # competition flavor — nothing downstream reboots them or needs apt/DNS.
            final_machines = json.loads(final_config_path.read_text())["machines"]
            if final_machines:
                print(f"  Final-stage pass (disruption + boot-hostile) on {len(final_machines)} machine(s)...")
                ensure_nat_forwarding(ctx)
                final_bundle = build_nakon_bundle(final_config_path)
                with timed(comp_dir, 6, "nakon", f"final x{len(final_machines)}"):
                    result = run_nakon(key, scoring_user, scoring_ip, final_bundle,
                                       final_config_path,
                                       timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(final_machines)),
                                       strict=False, jobs=nakon_jobs)
                # Merge, not overwrite: a failure in BOTH passes must keep the repair
                # tally (phase 6 used to erase it by writing only on failure).
                merged = list(state.get("nakon_failed_steps") or [])
                merged += [f"final: {line}" for line in result.failed[:20]]
                seen = set()
                state["nakon_failed_steps"] = [x for x in merged if not (x in seen or seen.add(x))][:40]
                _record_coverage(state, final_machines, result)
                if state["nakon_failed_steps"]:
                    _save_state()

            if compfile_flag(comp_dir / "Compfile", "team_beacons"):
                print("  Planting team beacons (hunt artifacts)...")
                with timed(comp_dir, 6, "beacons"):
                    plant_team_beacons(teams, boxes, ctx, box_username=box_username,
                                       box_password=box_password)

            print(f"  Snapshotting all boxes as '{SNAP_READY}' (as-delivered restore point)...")

            def _snap_ready(t):
                with timed(comp_dir, 6, "snapshot", t["vm_name"]):
                    take_snapshot(t.get("node", node), t["vmid"], SNAP_READY,
                                  description="tezcatlipoca: as delivered, post-sweep + hardening")

            run_concurrent(all_targets, _snap_ready, max_workers=4)
            checkpoint(6)
        else:
            print("[6/7] Skipped (resume).")

        if from_phase <= 7:
            current_phase = 7
            print("[7/7] Seeding competition and creating injects...")

            with timed(comp_dir, 7, "wait_quotient_http"):
                wait_for_http(f"http://{scoring_ip}/api/login", timeout=120)

            quotient_ctx = {
                "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
                "quotient_admin_password": admin_password,
            }

            if not state.get("seeded"):
                print("  Seeding teams and starting the competition clock...")
                with timed(comp_dir, 7, "seed_teams"):
                    seed_teams(scoring_ip, quotient_ctx)
                state["seeded"] = True
                _save_state()
            else:
                print("  Teams already seeded (resume) — skipping.")

            if not state.get("engine_unpaused"):
                # The unpause POST isn't idempotent, so a resume in the crash
                # window between POST and flag-save asks the engine first and
                # re-POSTs only when it really is still paused.
                paused = engine_paused(scoring_ip, quotient_ctx)
                if paused is False:
                    print("  Engine reports itself unpaused — recording and skipping.")
                else:
                    with timed(comp_dir, 7, "unpause_engine"):
                        unpause_engine(scoring_ip, quotient_ctx)
                state["engine_unpaused"] = True
                _save_state()
            else:
                print("  Engine already unpaused (resume) — skipping.")

            if injects and not state.get("injects_created"):
                print(f"  Creating {len(injects)} inject(s)...")
                resolve_inject_times(injects)
                with timed(comp_dir, 7, "create_injects", f"x{len(injects)}"):
                    _created, failed_titles = create_injects(scoring_ip, admin_password, injects)
                if failed_titles:
                    print(f"  WARNING: {len(failed_titles)} inject(s) failed to create: "
                          f"{', '.join(failed_titles)} — re-run --from-phase 7 to retry "
                          f"(existing injects are deduped)")
                else:
                    state["injects_created"] = True
                    _save_state()
            elif injects:
                print("  Injects already created (resume) — skipping.")
            checkpoint(7)
        else:
            print("[7/7] Skipped (resume).")
    except BaseException as e:
        print(f"\n  [!] Deploy failed during phase {current_phase} of '{comp_name}'.")
        resume_phase = current_phase
        if current_phase >= 2 and "already exists" in str(e).lower():
            resume_phase = 1
            print("      This looks like a Proxmox/Terraform state mismatch (something the "
                  "prior attempt created still exists, but Terraform's state doesn't know about "
                  "it) — resuming from the failed phase would just hit the same error again.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {comp_name} --from-phase {resume_phase} --yes")
        raise

    cred_lines = [
        f"# Credentials for {name} — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scoreboard:  http://{scoring_ip}",
        f"admin  {admin_password}",
    ]
    if packet_pw:
        cred_lines.append("# box credentials below are the packet-published defaults "
                          "(passwords.json) — teams rotate them at minute zero")
    if inject_password:
        cred_lines.append(f"inject  {inject_password}")
    for team_name, team_data in teams.items():
        cred_lines.append(f"{team_name}  {team_data['password']}  (192.168.{team_data['identifier']}.0/24)")
    cred_lines.append(f"box-login ({box_username})  {box_password}")
    for user, pw in box_creds.items():
        cred_lines.append(f"box-credlist-{user}  {pw}")
    for user, pw in (domain_creds or {}).items():
        cred_lines.append(f"box-credlist-domain-{user}  {pw}")
    cred_path = comp_dir / "credentials.txt"
    cred_path.write_text("\n".join(cred_lines) + "\n")
    os.chmod(cred_path, 0o600)

    print_timing_summary(comp_dir)

    print(f"\n{'='*60}")
    print(f"  {name} is live")
    print(f"{'='*60}")
    print(f"Scenario: {scenario}")
    print(f"Saved to: competitions/{comp_name}/  (credentials.txt, mode 0600)")
    print(f"\nScoreboard:    http://{scoring_ip}")
    print(f"Admin login:   admin / {admin_password}")
    if inject_password:
        print(f"Inject login:  inject / {inject_password}   ({len(injects)} inject(s) loaded)")
    print(f"\nTeam logins:")
    for team_name, team_data in teams.items():
        print(f"  {team_name} / {team_data['password']}  (subnet 192.168.{team_data['identifier']}.0/24)")
    print(f"\nBox login:     {box_username} / {box_password}  (every team box)")
    print(f"Box credlist:  " + ", ".join(f"{u}/{p}" for u, p in box_creds.items()))
    print(f"\nScoring engine SSH: ssh -i {key} {scoring_user}@{scoring_ip}")
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
                             "apply). See the resume hint printed on a failed deploy.")
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
        print(f"\n  Team count is decided at deploy time (--teams N, or the prompt).")
        print(f"  Nothing was deployed — no teardown, no terraform apply.")
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
               scoring_vmid=args.scoring_vmid, team_node=args.team_node, engine_node=args.engine_node)
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
           scoring_vmid=args.scoring_vmid, team_node=args.team_node, engine_node=args.engine_node)


if __name__ == "__main__":
    main()
