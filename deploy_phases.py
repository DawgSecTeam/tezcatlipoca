"""The seven deploy phases, one function per phase, plus their per-phase helpers.

deploy() used to inline all of this in a single 940-line function (CC ~194) whose
phases were `if from_phase <= N:` blocks reading ~160 locals. Each phase here
takes the DeployContext prepare() built and touches only that, so a phase
boundary is a call boundary: a phase can be read, tested, or re-run on its own.

Phases reach back into deploy.py through this module's `deploy` handle for the
four pure helpers that are DEFINED there (phase1_destroy_waves,
golden_rebuild_gate, record_stage_coverage, reset_domain_markers). That
indirection is deliberate, not laziness: those functions read their own module
globals, and tests/test_deploy_phases.py monkeypatches
deploy._is_template / deploy.stored_template_hash / deploy.frozen_gate /
deploy.destroy_vm_if_exists to exercise golden_rebuild_gate offline — a
`from deploy import golden_rebuild_gate` would still work for that, but reaching
through the handle documents where the helper lives and keeps any future
monkeypatch of the helper itself effective. Everything else is imported from the
module that owns it. The handle also keeps the module graph one-directional at
import time: deploy.py imports this module lazily inside deploy(), so
`import deploy` here always resolves to a fully-initialised module (and
`python3 deploy.py` does not trip over a partially-initialised one).
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import deploy
from config_ops import destroy_bridge_if_exists, write_text_atomic
from constants import (DEFAULT_ENGINE_MGMT_GW, PER_MACHINE_NAKON_BUDGET,
                       SNAP_BASE)
from engine_ops import (bootstrap_scoring_engine, ensure_nat_forwarding,
                        prepare_engine_from_template, push_event_conf)
from golden_ops import (_is_template, _quote_sshkeys, _template_vmid_map,
                        build_golden_set)
from hardening_ops import (ensure_alpine_services, fix_dns_on_boxes,
                           fix_services_on_boxes, prep_apt_on_boxes,
                           setup_ubuntu_auth)
from jump_ops import build_jump_vms
from nakon_ops import (build_nakon_bundle, generate_slot_golden_config,
                       run_nakon)
from nodes_ops import record_of
from range_ops import (destroy_vm_if_exists, proxmox_api, take_snapshot,
                       terraform_dir, terraform_plugin_cache_dir)
from routing_ops import verify_satellite_routing
from ssh_ops import (forget_engine_host_key, read_terraform_ctx,
                     wait_for_boxes_ssh, wait_for_cloud_init, wait_for_ssh)
from template_ops import (build_engine_template, destroy_engine_template,
                          engine_hash_inputs, find_engine_template, frozen_gate,
                          hash_from_inputs, load_template_hashes,
                          save_template_hashes, stored_template_hash)
from timing import timed
from utils import (compfile_flag, compfile_value, is_unmanaged, run_concurrent,
                   run_terraform)
from windows_ops import bootstrap_windows_box


def destroy_owned(ctx, destroy_node, vmid, vm_name):
    """Destroy one VM if it is ours (tag-guarded), timed under phase 1."""
    with timed(ctx.comp_dir, 1, "destroy_vm", vm_name):
        legacy_name = vm_name if vm_name.startswith("golden-") else None
        destroy_vm_if_exists(destroy_node, vmid, expect_tags=ctx.comp_tags,
                             legacy_name=legacy_name)


def destroy_pool(ctx, vmid_map, destroy_node):
    """Destroy one wave in parallel (bounded like every other per-box bulk pass)."""
    workers = min(8, len(vmid_map)) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(destroy_owned, ctx, destroy_node, vmid, name)
                   for vmid, name in sorted(vmid_map.items())]
        for fut in futures:
            fut.result()


def destroy_node_waves(ctx, destroy_node, node_targets, slot, extra_destroy=None):
    """One wave pair per hosting node, that node's VMs and its slot's golden span
    (multi-node) — the engine node keeps the historical slot-0 behavior."""
    try:
        node_vms = proxmox_api("GET", f"/nodes/{destroy_node}/qemu")["data"]
    except Exception as e:
        print(f"  WARNING: could not scan {destroy_node} for stranded "
              f"clones ({e}) — proceeding")
        node_vms = []
    wave1, wave2 = deploy.phase1_destroy_waves(
        node_vms, node_targets, ctx.legacy_clones if slot == 0 else {},
        ctx.engine_vmid, ctx.boxes, ctx.comp_tags,
        lambda vid: _is_template(destroy_node, vid),
        load_template_hashes(ctx.comp_dir), ctx.golden_hashes,
        frozen_keep=ctx.frozen_keep, slot=slot, extra_destroy=extra_destroy)
    destroy_pool(ctx, wave1, destroy_node)
    destroy_pool(ctx, wave2, destroy_node)


def phase1_cleanup(ctx):
    """[1/7] Tear down the previous range: team boxes first, then their templates.

    Wave 1: every team box (the computed set covers ALL teams now that terraform
    builds them) plus any legacy API clones from a pre-golden range. Linked
    clones must die BEFORE their templates. Wave 2 (goldens/engine/stale slots)
    is phase1_destroy_waves' job; this function is only the node/bridge walk and
    the resume markers around it."""
    print("[1/7] Cleaning up previous deployment (parallel; deletes are metadata-light "
          "— the datastore-saturation hazard belongs to bulk clone writes, not deletes)...")
    cloned_path = ctx.comp_dir / "cloned_vms.json"
    ctx.legacy_clones = {}
    if cloned_path.exists():
        try:
            ctx.legacy_clones = {int(v): str(k) for k, v in json.loads(cloned_path.read_text()).items()}
        except (ValueError, TypeError, OSError):
            print("  WARNING: could not parse cloned_vms.json — relying on computed vmids")

    # Wave 1: every team box (the computed set covers ALL teams now that terraform
    # builds them) plus any legacy API clones from a pre-golden range. Linked
    # clones must die BEFORE their templates.
    destroy_node_waves(ctx, ctx.node,
                       [t for t in ctx.all_targets if t.get("node") == ctx.node], 0)
    if ctx.placement:
        for sat in ctx.placement["satellites"]:
            sat_rec = record_of(ctx.placement, sat["name"])
            destroy_node_waves(
                ctx, sat_rec.node,
                [t for t in ctx.all_targets if t.get("node") == sat_rec.node],
                sat["slot"],
                extra_destroy={sat["jump_vmid"]: f"jump-{ctx.comp_name}-{sat['slot']}"})
            for team_key in ctx.placement["team_nodes"]:
                if ctx.placement["team_nodes"][team_key] != sat["name"]:
                    continue
                with timed(ctx.comp_dir, 1, "destroy_bridge", f"vmbr{ctx.teams[team_key]['identifier']}"):
                    destroy_bridge_if_exists(sat_rec.node, f"vmbr{ctx.teams[team_key]['identifier']}")
    for team_key, team in ctx.teams.items():
        if ctx.placement and ctx.placement["team_nodes"][team_key] != ctx.placement["engine_node"]:
            continue  # satellite bridge — destroyed above on its own host
        with timed(ctx.comp_dir, 1, "destroy_bridge", f"vmbr{team['identifier']}"):
            destroy_bridge_if_exists(ctx.node, f"vmbr{team['identifier']}")
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)
    deploy.reset_domain_markers(ctx.comp_dir)
    time.sleep(5)


def phase2_engine_template(ctx):
    """[2/7] Engine template lifecycle, then terraform apply #1.

    M4: the engine template is built once per competition and reused across its test
    runs — rebuilt only on config drift, never when frozen. Apply #1 then builds the
    engine (a linked clone of that template, seconds) and every team bridge, with
    team_box's for_each deliberately empty; apply #2 in phase 4 flips it on once the
    goldens exist. The checkpoint and the terraform-context read stay in the caller
    (deploy), in that order: a phase-2 resume still needs the outputs, and a run that
    dies between them must leave last_phase at 2 on disk."""
    # --- M4: engine template lifecycle (build once per competition, reuse
    # across its test runs; rebuild only on config drift and never when frozen).
    main_tf_text = Path("terraform/main.tf").read_text()
    quotient_ref = compfile_value(ctx.comp_dir / "Compfile", "quotient_ref")
    engine_inputs = engine_hash_inputs(
        int(os.environ["TF_VAR_template_vm_id"]), quotient_ref,
        main_tf_text, bootstrap_scoring_engine)
    engine_hash = hash_from_inputs(engine_inputs)
    stored = load_template_hashes(ctx.comp_dir)
    tmpl = find_engine_template(ctx.node, ctx.engine_vmid)
    rebuild = True
    if tmpl:
        entry = stored.get("engine") or {}
        if stored_template_hash(ctx.node, tmpl) == engine_hash and entry.get("hash") == engine_hash:
            print(f"  Engine template hash matches — reusing (vmid {tmpl})")
            rebuild = False
        elif not frozen_gate(ctx.comp_dir, entry.get("inputs"), engine_inputs,
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
            destroy_vm_if_exists(ctx.node, ctx.engine_vmid, expect_tags=ctx.comp_tags)
            destroy_engine_template(ctx.node, ctx.engine_vmid, expect_tags={
                "tezcatlipoca", f"comp-{ctx.comp_name}", "engine-template"})
        ctx_early = {
            "ssh_key_path": ctx.ssh_key_abs,
            "vm_username": os.environ["TF_VAR_vm_username"],
            "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"]),
        }
        with timed(ctx.comp_dir, 2, "engine_template_build"):
            tmpl, build_info = build_engine_template(
                ctx.node, ctx.comp_dir, ctx.engine_vmid,
                int(os.environ["TF_VAR_template_vm_id"]),
                ctx_early, ctx.postgres_password, ctx.redis_password, quotient_ref,
                engine_hash, engine_inputs)
        ctx.state["engine_build_info"] = build_info
    save_template_hashes(ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.state["engine_template_vmid"] = tmpl
    ctx.state["engine_template_hash"] = engine_hash
    ctx.save_state()

    print("[2/7] Terraform apply #1 (engine from template + bridges; team boxes "
          "come in apply #2)...")
    ctx.tfvars["engine_clone_id"] = tmpl
    write_text_atomic(ctx.tfvars_path, json.dumps(ctx.tfvars, indent=2))
    tf_env = {**os.environ, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
    tf_cwd = str(terraform_dir(ctx.comp_dir))
    run_terraform(["init"], cwd=tf_cwd, env=tf_env, timeout=300)
    # team_box's for_each is empty here (build_team_boxes=false): only the engine
    # (a linked clone of the engine template — seconds, no bulk disk copy) and the
    # bridges are built, plus team_nics' netplan for every team bridge and the
    # cold-boot that surfaces the engine's team NICs.
    with timed(ctx.comp_dir, 2, "terraform_apply"):
        run_terraform(["apply", "-auto-approve", "-parallelism=1"], cwd=tf_cwd, env=tf_env, timeout=2400)

    apply_ctx = read_terraform_ctx(ctx.comp_dir)
    # Fresh engine VM => new host key; drop any stale pin so accept-new re-pins it.
    forget_engine_host_key(apply_ctx["scoring_engine_ip"])
    with timed(ctx.comp_dir, 2, "wait_engine_ssh", apply_ctx["scoring_engine_ip"]):
        wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                     apply_ctx["scoring_engine_ip"], timeout=300)
    if ctx.placement and ctx.placement["satellites"]:
        # Jump/router per satellite, then the fail-loud routing gate: nothing
        # downstream (satellite golden plants, nakon, scoring) works without
        # engine -> jump -> satellite-bridge paths.
        jump_ctx = {"ssh_key_path": ctx.ssh_key_abs,
                    "vm_username": os.environ["TF_VAR_vm_username"],
                    "ssh_public_key_quoted": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"])}
        with timed(ctx.comp_dir, 2, "jump_vms", f"x{len(ctx.placement['satellites'])}"):
            build_jump_vms(ctx.placement, ctx.engine_vmid, jump_ctx, ctx.comp_name,
                           ctx.engine_mgmt_ip,
                           engine_mgmt_gw=os.environ.get("TF_VAR_engine_mgmt_gw",
                                                         DEFAULT_ENGINE_MGMT_GW))
        with timed(ctx.comp_dir, 2, "routing_converge"):
            verify_satellite_routing(ctx.placement, apply_ctx)
    # Record where this state's resources live, for the stale-state guard above.
    ctx.state["deployed_endpoint"] = os.environ.get("TF_VAR_proxmox_endpoint", "").rstrip("/")


def phase3_prepare_engine(ctx):
    """[3/7] Apply this deploy's per-competition state to the engine clone."""
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


def phase4_golden_set(ctx):
    """[4/7] Plant the golden set, then terraform apply #2 to build every team box.

    Two halves on purpose: the goldens must exist as templates before apply #2
    flips build_team_boxes on, and both are timed under phase 4 so the timing
    summary shows the plant and the linked-clone burst separately. The [4/7] and
    [4b/7] banners stay distinct because operators (and incident logs) key on
    them."""

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
        deploy.golden_rebuild_gate(ctx.comp_dir, ctx.node, 0, ctx.boxes, ctx.engine_vmid,
                                   stored, ctx.golden_hashes, ctx.golden_inputs, ctx.comp_name)
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
            deploy.golden_rebuild_gate(ctx.comp_dir, sat_rec.node, slot, ctx.boxes,
                                       ctx.engine_vmid, stored, ctx.golden_hashes,
                                       ctx.golden_inputs, ctx.comp_name)
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


def phase5_repair_sweep(ctx):
    """[5/7] Post-clone repair sweep: sshd/sudoers plants, then fix_services.

    Re-runnable by design: the .postclone-swept marker makes a resume skip the
    whole sweep, and the nakon pass itself is strict=False (see the comment at the
    call) because one flaky plant must not kill a sweep that 98% landed."""
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
            deploy.record_stage_coverage(ctx.state, repair_machines, result, ctx.save_state)
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
