
"""Redeploy a subset of a live competition's boxes (rollback-ready/base, reconfigure, rebuild)."""

import argparse
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

import urllib3
from dotenv import load_dotenv

import pipeline_api
from config_ops import write_state
from constants import SNAP_BASE, SNAP_READY, WINDOWS_ADMIN_USER, ownership_tags
from engine_ops import (ensure_nat_forwarding, prepare_engine_from_template,
                        push_event_conf, read_event_conf)
from nakon_ops import acquire_engine_lock, build_nakon_bundle, release_engine_lock
from range_ops import (
    clone_marker,
    delete_snapshot,
    describe_target,
    destroy_vm_if_exists,
    guest_agent_exec_root,
    guest_agent_exec_windows,
    list_snapshots,
    load_targets,
    parse_vm_tags,
    proxmox_api,
    rollback_snapshot,
    snapshot_support_hint,
    start_vm,
    take_snapshot,
    wait_for_guest_agent,
    wait_for_proxmox_task,
)
from template_ops import stored_template_hash
from timing import timed
from ssh_ops import classify_ssh_failure, forget_engine_host_key, ssh_to_engine, wait_for_ssh
from utils import (compfile_flag, load_compfile, load_users_config, pick_competition,
                   run_terraform, valid_comp_name)

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def parse_team_selector(raw, teams):
    """Parse team selector (team key, number, or subnet identifier)."""
    by_key = {k.lower(): k for k in teams}
    by_ident = {str(v["identifier"]): k for k, v in teams.items()}
    by_number = {k.lower().removeprefix("team"): k for k in teams}

    selected = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        low = token.lower()
        match = by_key.get(low) or by_ident.get(low) or by_number.get(low)
        if match is None:
            raise SystemExit(
                f"  ERROR: no team matches '{token}'. This competition has: "
                + ", ".join(f"{k} (identifier {v['identifier']})" for k, v in teams.items())
            )
        if match not in selected:
            selected.append(match)
    return selected


def parse_box_selector(raw, boxes):
    known = {b["name"].lower(): b["name"] for b in boxes}
    selected = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        match = known.get(token.lower())
        if match is None:
            raise SystemExit(
                f"  ERROR: no box named '{token}'. This competition has: "
                + ", ".join(b["name"] for b in boxes)
            )
        if match not in selected:
            selected.append(match)
    return selected


def box_platform(box):
    """Platform via pipeline_api.os_to_platform (the single map nakon also uses)."""
    return pipeline_api.os_to_platform(box.get("template", ""))


def select_targets(comp_dir, teams, boxes, args):
    """Full target list, narrowed by whichever filters were given (AND-combined)."""
    targets = load_targets(comp_dir, teams, boxes)

    if args.teams:
        keep = set(parse_team_selector(args.teams, teams))
        targets = [t for t in targets if t["team_key"] in keep]
    if args.boxes:
        keep = set(parse_box_selector(args.boxes, boxes))
        targets = [t for t in targets if t["box_name"] in keep]
    if args.platform:
        want = args.platform.lower()
        targets = [t for t in targets if box_platform(t["box"]) == want]

    return targets



def _is_golden_pipeline(state):
    """True when the range's state carries golden templates (pipeline v2 or later —
    v3 layered the firewall schema on top of v2's goldens, it did not replace them).
    A missing version means a pre-golden range, whose only rebuild source is the
    original box template. Live-found 2026-10-03: the reset matrix's rebuild rung
    took the v1 path on a v3 range because these gates read `== 2`."""
    try:
        return int(state.get("pipeline_version") or 1) >= 2
    except (TypeError, ValueError):
        return False


def run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """Configure half: DNS, auth, scoped nakon (--only), service hardening."""
    key = Path(ctx["ssh_key_path"])
    scoring_user = os.environ["TF_VAR_vm_username"]
    scoring_ip = ctx["scoring_engine_ip"]

    linux_targets = [t for t in targets if box_platform(t["box"]) == "linux"]
    # Order matters, and it is a documented invariant (docs/architecture.md: "On fresh
    # clones the NOPASSWD sudoers grant lands BEFORE the DNS fix, whose sudo calls
    # soft-fail until the grant does"). Same order as deploy.py phase 4. Re-inverting
    # these two is a silent ~2-minute-per-box regression: without the grant, DNS_FIX_CMD's
    # sudo is rejected, so fix_dns_on_boxes burns its full 8x15s retry ladder before
    # falling back to the root guest agent (hardening_ops.py), which is the only reason
    # the wrong order "works" at all.
    pipeline_api.setup_ubuntu_auth(linux_targets, ctx)
    pipeline_api.fix_dns_on_boxes(linux_targets, ctx)

    pipeline_api.ensure_nat_forwarding(ctx)

    machines = [t["machine"] for t in targets]
    if nakon_bundle is None or nakon_config_path is None:
        # prepare_nakon_assets returned no stage: every plant for this comp rides the
        # golden clone, so there is nothing for nakon to re-apply. The python-side
        # steps below (auth grant, DNS, service hardening) still re-run.
        print("  (no nakon replant stage for this competition — skipping the plant pass)")
    else:
        print(f"  Running Nakon on {len(machines)} machine(s): {', '.join(machines)}")
        # strict=False, mirroring deploy.py's phase-6 stance: these re-plants hit live
        # boxes mid-event, and one flaky/broken pin must not abort a repair sweep.
        nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
        result = pipeline_api.run_nakon(
            key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
            only=machines,
            timeout=max(2400, pipeline_api.PER_MACHINE_NAKON_BUDGET * len(machines)),
            strict=False, jobs=nakon_jobs,
        )
        # Append, never replace: the deploy-time entries belong to verify's plant-integrity
        # line, and a scoped mid-event replant must not retroactively declare the deploy's
        # failed steps resolved — or erase the record that they ever existed.
        state["nakon_failed_steps"] = list(state.get("nakon_failed_steps") or []) + [
            f"redeploy: {line}" for line in result.failed[:20]]
        state_path = comp_dir / ".deploy_state.json"
        if state_path.exists():
            # Shared atomic writer (config_ops.write_state): 0600 at creation, temp+os.replace.
            # Never hand-roll this — .deploy_state.json carries the only copy of the box
            # passwords, so a torn write bricks resume AND redeploy at once (deploy.py's
            # atomic-rename note; winad-testrun 2026-09-25).
            write_state(state_path, state)

    pipeline_api.fix_services_on_boxes(comp_dir, linux_targets, ctx, box_creds=state.get("box_creds"))


def _domain_config_path(comp_dir, fallback):
    full_config = comp_dir / "nakon-config.json"
    return full_config if full_config.exists() else fallback


def rerun_domain_configs(targets, ctx, comp_dir, state, nakon_config_path):
    """Re-run AD domain chain for affected teams (promote DC only if reset).

    Returns True when the boxes' domain state may be treated as settled — no
    domain semantics at all, or the chain re-ran clean — and False when the
    chain was supposed to run but could not. Callers must NOT re-take tz-ready
    on False: snapshotting then would bake a broken state in as 'as delivered'."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        return True
    roles = json.loads(roles_path.read_text())
    domain_targets = [t for t in targets if roles.get(t["box_name"])]
    if not domain_targets:
        return True

    box_password = state.get("box_password")
    if not box_password:
        print("  WARNING: .deploy_state.json has no box_password — cannot re-run domain "
              "configuration for the reset domain-role box(es). Rejoin by hand, or re-run "
              "create-competition.py --from-phase 6.")
        return False

    teams = json.loads((comp_dir / "teams.json").read_text())
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    affected_teams = {t["team_key"] for t in domain_targets}

    print("  Domain-role box(es) were reset to a pre-domain state — re-running domain "
          "configuration for " + ", ".join(sorted(affected_teams)) + "...")
    # The post-clone stage file may contain only the selected/rebuilt box types. Domain
    # orchestration still needs every member machine in the affected team so it can
    # rejoin them after a DC reset; use the full source-of-truth machine list here.
    domain_config_path = _domain_config_path(comp_dir, nakon_config_path)
    for team_key in sorted(affected_teams):
        dc_reset = any(
            roles.get(t["box_name"]) == "dc" and t["team_key"] == team_key
            for t in domain_targets
        )
        if dc_reset:
            stale_marker = comp_dir / f".nakon-domain-{team_key}-adds.json"
            if stale_marker.exists():
                print(f"  Deleting stale ADDS marker for {team_key} — the DC was reset, so "
                      "it must be re-promoted, not assumed promoted.")
                stale_marker.unlink()
        pipeline_api.deploy_domain_configs(
            {team_key: teams[team_key]}, boxes, comp_dir, domain_config_path,
            Path(ctx["ssh_key_path"]), os.environ["TF_VAR_vm_username"],
            ctx["scoring_engine_ip"], box_password, promote_dc=dc_reset,
        )
    return True


def mode_resync(targets, ctx, node, comp_dir, state, state_path):
    """Engine-authoritative credential alignment — no rollback, no re-plant.

    1. Pull the secrets the engine actually holds (event.conf, credlist,
       /opt/quotient/.env) and align .deploy_state.json where they differ.
    2. Re-set the selected boxes' passwords to the state values via the guest
       agent, so drifted boxes come back in line without needing SSH.
    box_password (the box login) exists nowhere on the engine — it is baked
    into the boxes at bootstrap — so it is reported, not repaired."""
    print("  Reading engine-authoritative secrets (event.conf, credlist, /opt/quotient/.env)...")
    secrets = read_event_conf(ctx)

    changed = []
    for key in ("admin_password", "postgres_password", "redis_password", "inject_password"):
        new = secrets.get(key)
        if new and state.get(key) != new:
            print(f"    {key}: state {'drifted' if state.get(key) else 'missing'} -> aligned with engine")
            state[key] = new
            changed.append(key)
    if secrets.get("box_creds") and state.get("box_creds") != secrets["box_creds"]:
        print("    box_creds: drifted -> aligned with engine credlist")
        state["box_creds"] = secrets["box_creds"]
        changed.append("box_creds")
    for team_key, pw in (secrets.get("team_passwords") or {}).items():
        entry = (state.get("teams") or {}).get(team_key)
        if pw and entry and entry.get("password") != pw:
            print(f"    {team_key} password: drifted -> aligned with engine")
            entry["password"] = pw
            changed.append(f"teams.{team_key}.password")
    if not changed:
        print("    .deploy_state.json already matches the engine.")
    if changed:
        # Atomic + 0600, same reason as run_nakon_and_harden above.
        write_state(state_path, state)

    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: .deploy_state.json has no box_password — cannot re-set box logins. "
            "Only the state alignment above was applied.")
    box_username, _credlist = load_users_config(comp_dir)
    for t in targets:
        tnode = t.get("node") or node
        try:
            if box_platform(t["box"]) == "windows":
                rc, out, err = guest_agent_exec_windows(
                    tnode, t["vmid"],
                    f"net user {WINDOWS_ADMIN_USER} '{box_password}'", timeout=120)
            else:
                script = f"echo {shlex.quote(f'{box_username}:{box_password}')} | chpasswd"
                for user, pw in (state.get("box_creds") or {}).items():
                    script += f"; echo {shlex.quote(f'{user}:{pw}')} | chpasswd"
                rc, out, err = guest_agent_exec_root(tnode, t["vmid"], script, timeout=120)
            if rc == 0:
                print(f"    {describe_target(t)}: passwords re-set (via guest agent)")
            else:
                print(f"    WARNING: {describe_target(t)}: guest agent rc={rc}: {(err or '').strip()[:120]}")
        except Exception as e:
            print(f"    WARNING: {describe_target(t)}: password re-set failed ({e})")

    print("  NOTE: box_password itself cannot be recovered from the engine — if the box "
          "login (as opposed to credlist accounts) is what drifted, reset it by hand.")
    return targets


def mode_rollback(targets, ctx, node, snapshot, comp_dir, state, nakon_config_path,
                  nakon_bundle, reconfigure):
    """Rollback to snapshot, optionally reconfigure."""
    restored = []
    for t in targets:
        print(f"  Rolling back {describe_target(t)} to '{snapshot}'...")
        try:
            tnode = t.get("node") or node
            if snapshot == SNAP_BASE:
                # PVE only rolls a disk back to its MOST RECENT snapshot (live-found
                # 2026-10-03: "can't rollback, 'tz-base' is not most recent snapshot").
                # tz-ready is always taken after tz-base, so reaching the pre-plant
                # disk requires dropping the newer restore point first — the replant
                # below re-takes it (and rebuild re-takes both), so nothing is lost
                # that the ladder's own next steps don't recreate.
                snaps = list_snapshots(tnode, t["vmid"])
                if SNAP_READY in snaps:
                    print(f"    dropping newer '{SNAP_READY}' (PVE rolls back only to the "
                          f"most recent snapshot; it is re-taken after the replant)")
                    delete_snapshot(tnode, t["vmid"], SNAP_READY)
            rollback_snapshot(tnode, t["vmid"], snapshot)
            restored.append(t)
            print(f"    {t['vm_name']} restored")
        except Exception as e:
            print(f"  WARNING: rollback failed for {describe_target(t)}: {e}")

    if not restored:
        raise SystemExit("  ERROR: no box was rolled back successfully — nothing to do.")

    pipeline_api.wait_for_boxes_ssh(ctx, restored, timeout=300)

    if reconfigure:
        pipeline_api.wait_for_cloud_init(ctx, restored, timeout=240)
        run_nakon_and_harden(restored, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
        try:
            domain_settled = rerun_domain_configs(restored, ctx, comp_dir, state, nakon_config_path)
        except Exception:
            print("  tz-ready NOT re-taken — the domain chain failed above, so the boxes are "
                  "not in an 'as delivered' state.")
            raise
        if domain_settled:
            print(f"  Re-taking '{SNAP_READY}' for the recovered boxes...")
            for t in restored:
                take_snapshot(t.get("node") or node, t["vmid"], SNAP_READY,
                              description="tezcatlipoca: as delivered (re-taken by redeploy)")
        else:
            print("  tz-ready NOT re-taken — domain configuration could not run (see above); "
                  "snapshotting now would bake a broken state in as 'as delivered'.")

    return restored


def mode_reconfigure(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle):
    """No rollback — re-run the configure chain against the boxes as they are right now."""
    # 900s: the operator->engine->box jump path has banner-timeout flakiness
    # windows; a shared short budget aborts scoping runs on boxes the engine
    # reaches fine
    pipeline_api.wait_for_boxes_ssh(ctx, targets, timeout=900)
    run_nakon_and_harden(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    roles_path = comp_dir / "domain_roles.json"
    if roles_path.exists():
        roles = json.loads(roles_path.read_text())
        if any(roles.get(t["box_name"]) for t in targets):
            print("  NOTE: reconfigure never resets disks, so domain membership is assumed "
                  "intact. If the AD domain itself is what's broken, use --mode rollback-base "
                  "(or rebuild) — those re-promote/re-join after the reset.")
    return targets


def template_vmid_for(box):
    """Resolve a box's template name to a vmid, the way main.tf's templates data source does."""
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    data = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    for vm in data:
        if (vm.get("name") == box["template"]
                and vm.get("template") == 1
                and "template" in (vm.get("tags") or "").split(";")
                and vm.get("vmid") != scoring_template_id):
            return vm["vmid"]
    raise SystemExit(
        f"  ERROR: no Proxmox template named '{box['template']}' (tagged 'template'). "
        f"Available: " + ", ".join(pipeline_api.list_proxmox_templates())
    )


def mode_rebuild(targets, ctx, node, comp_dir, state, nakon_config_path, nakon_bundle):
    """Recreate VM from template then configure (clones template, not team1 live box)."""
    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: .deploy_state.json has no box_password — can't recreate a box with the "
            "login the rest of the range uses. Rebuild is unavailable for this competition."
        )
    ssh_public_key = os.environ["TF_VAR_ssh_public_key"]
    box_username = ctx.get("box_username", "ubuntu")

    rebuilt = []
    for t in targets:
        tnode = t.get("node") or node
        box = t["box"]
        windows = box_platform(box) == "windows"
        full_clone = True
        src_vmid = None
        src_label = None
        if _is_golden_pipeline(state):
            # Multi-node: the box's golden is its own node's slot copy (state carries
            # per-slot ids); single-node keeps the flat map.
            by_slot = state.get("golden_ids_by_slot") or {}
            if by_slot:
                from nodes_ops import read_placement, slot_of_team
                pl = read_placement(comp_dir)
                slot = slot_of_team(pl, t["team_key"]) if pl else 0
                slot_ids = by_slot.get(str(slot)) or {}
            else:
                slot_ids = state.get("golden_template_ids") or {}
            if box["name"] in slot_ids:
                src_vmid = int(slot_ids[box["name"]])
                full_clone = False
                src_label = f"golden template '{box['template']}' (vmid {src_vmid}, linked clone)"
                # M4 frozen semantics: the rebuild uses the frozen template as-is. A hash
                # mismatch against the verified record is a WARNING, never a mid-event
                # blocker — but it must be loud, because it means the node's template is
                # not the one the verify PASS covered.
                expected_hash = (state.get("golden_hashes") or {}).get(box["name"])
                on_node = stored_template_hash(tnode, src_vmid)
                if expected_hash and on_node and on_node != expected_hash:
                    print(f"    WARNING: golden template for '{box['name']}' on the node "
                          f"({on_node[:12]}) differs from the verified hash "
                          f"({expected_hash[:12]}) — rebuilding from the node's template "
                          f"anyway. If this competition is frozen, investigate after the "
                          f"event.")
            else:
                raise SystemExit(
                    f"  ERROR: pipeline v2 state has no golden template for '{box['template']}' "
                    f"(slot golden ids: {sorted(slot_ids)}) — a rebuild from the ORIGINAL "
                    f"template would miss every golden-stage install (services, vulns). "
                    f"Redeploy the range instead.")
        else:
            src_vmid = template_vmid_for(box)
            src_label = f"template '{box['template']}' (vmid {src_vmid})"
        print(f"  Rebuilding {describe_target(t)} from {src_label}...")

        # Ownership guard: the state's run id is the destruction anchor. A box that
        # belongs to another run of the same competition ID (a second worktree) is
        # refused — rebuilding over it would eat someone else's range. Legacy state
        # (no run id) keeps today's behavior via the explicit untagged allowance.
        _run_id = state.get("run_id") or ""
        destroy_vm_if_exists(tnode, t["vmid"],
                             expect_tags=ownership_tags(comp_dir.name, _run_id),
                             allow_untagged=not _run_id)

        upid = proxmox_api("POST", f"/nodes/{tnode}/qemu/{src_vmid}/clone", data={
            "newid": t["vmid"],
            "name": t["vm_name"],
            "full": 1 if full_clone else 0,
            # /clone takes no tags, and the tagging PUT below only lands after the clone
            # task finishes — a kill mid-clone would otherwise leave an unmarked VM
            # squatting our vmid slot. The description exists from the clone's first
            # instant (range_ops.clone_marker; winad-testrun 2026-09-25).
            "description": clone_marker(comp_dir.name),
        })["data"]
        wait_for_proxmox_task(tnode, upid)

        config = {
            "net0": f"virtio,bridge=vmbr{t['identifier']}",
            "cores": box["cpu"],
            "memory": box["memory_mb"],
            # Stamp ownership explicitly: a clone INHERITS its source's tags, and a
            # golden reused across runs carries a stale run-<id> — the next teardown's
            # full-tag-set guard would then refuse this box as foreign.
            "tags": ";".join(sorted(ownership_tags(comp_dir.name, _run_id))),
        }
        if not windows:
            config.update({
                "ipconfig0": f"ip={t['ip']}/24,gw=192.168.{t['identifier']}.1",
                "ciuser": box_username,
                "cipassword": box_password,
                "sshkeys": quote_sshkeys(ssh_public_key),
            })
        proxmox_api("PUT", f"/nodes/{tnode}/qemu/{t['vmid']}/config", data=config)

        if box.get("disk_gb"):
            # The box's own disk interface (Windows boxes are sata0 — a hardcoded scsi0
            # made every Windows rebuild die here), and only ever grow: a golden clone
            # already carries the box's size, and PVE refuses a shrink.
            iface = box.get("disk_iface") or "scsi0"
            cur = proxmox_api("GET", f"/nodes/{tnode}/qemu/{t['vmid']}/config")["data"].get(iface, "")
            m = re.search(r"size=(\d+)G", cur)
            if not m or int(m.group(1)) < int(box["disk_gb"]):
                proxmox_api("PUT", f"/nodes/{tnode}/qemu/{t['vmid']}/resize", data={
                    "disk": iface, "size": f"{box['disk_gb']}G",
                })

        start_vm(tnode, t["vmid"])

        if windows:
            print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
            gw = f"192.168.{t['identifier']}.1"
            pipeline_api.bootstrap_windows_box(tnode, t["vmid"], t["ip"], gw, "8.8.8.8", box_password)

        rebuilt.append(t)

        # M3.3 made every team a Terraform resource (`team_box` for_each covers all teams;
        # satellites get team_box_sat1..4), so on a v2 range EVERY rebuilt box drifts from
        # state — the old team1-only note was pre-M3.3 truth. Fine mid-event; nobody runs a
        # full apply against a live range, but the next one will want to replace these.
        if _is_golden_pipeline(state) or t["team_key"] == "team1":
            print(f"    NOTE: {t['vm_name']} is a Terraform-managed resource. It was recreated "
                  f"outside Terraform, so the next `terraform apply` will see drift and want to "
                  f"replace it. Fine mid-event; re-import or accept the replacement afterwards.")

    pipeline_api.wait_for_boxes_ssh(ctx, rebuilt, timeout=600)
    pipeline_api.wait_for_cloud_init(ctx, rebuilt, timeout=300)

    with timed(comp_dir, "rebuild", "team_rebuild_total",
               ",".join(t["vm_name"] for t in rebuilt)):
        print(f"  Snapshotting rebuilt boxes as '{SNAP_BASE}'...")
        for t in rebuilt:
            take_snapshot(t.get("node") or node, t["vmid"], SNAP_BASE,
                          description="tezcatlipoca: rebuilt from template, pre-Nakon")

        # M4: the rebuild plants the POST-CLONE STAGES in the deploy's order, not the
        # full config — the golden-stage work rides the clone, and planting the
        # disruptive/boot-hostile final stage before the domain joins is exactly the
        # brick hazard the three-pass split exists to prevent.
        rebuilt_names = [t["machine"] for t in rebuilt]
        repair_path = comp_dir / ".nakon-repair.json"
        final_path = comp_dir / ".nakon-final.json"

        def _stage_pass(path, stage):
            if not path.exists():
                print(f"  ({stage} stage file missing — skipping)")
                return
            machines = json.loads(path.read_text())["machines"]
            names = [m["name"] for m in machines if m["name"] in rebuilt_names]
            if not names:
                print(f"  (no {stage}-stage configs for the selected boxes — skipping)")
                return
            bundle = build_nakon_bundle(path)
            key = Path(ctx["ssh_key_path"])
            scoring_user = os.environ["TF_VAR_vm_username"]
            scoring_ip = ctx["scoring_engine_ip"]
            nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
            pipeline_api.ensure_nat_forwarding(ctx)
            with timed(comp_dir, "rebuild", f"nakon_{stage}", f"x{len(names)}"):
                result = pipeline_api.run_nakon(
                    key, scoring_user, scoring_ip, bundle, path,
                    only=names,
                    timeout=max(2400, pipeline_api.PER_MACHINE_NAKON_BUDGET * len(names)),
                    strict=False, jobs=nakon_jobs,
                )
            if result.failed:
                print(f"  WARNING: {stage}-stage replant had {len(result.failed)} "
                      f"FAILED step(s) (recorded in .deploy_state.json)")
                state["nakon_failed_steps"] = list(state.get("nakon_failed_steps") or []) + [
                    f"redeploy {stage}-stage: {line}" for line in result.failed[:20]]
                write_state(comp_dir / ".deploy_state.json", state)

        print("  Repair-stage sweep (sshd/sudoers) on rebuilt boxes...")
        _stage_pass(repair_path, "repair")
        pipeline_api.fix_services_on_boxes(
            comp_dir, [t for t in rebuilt if box_platform(t["box"]) == "linux"],
            ctx, box_creds=state.get("box_creds"))

        try:
            domain_settled = rerun_domain_configs(rebuilt, ctx, comp_dir, state, nakon_config_path)
        except Exception:
            print("  tz-ready NOT re-taken — the domain chain failed above, so the boxes are "
                  "not in an 'as delivered' state.")
            raise

        if final_path.exists():
            print("  Final-stage pass (disruption + boot-hostile) on rebuilt boxes...")
            _stage_pass(final_path, "final")

        if domain_settled:
            print(f"  Snapshotting rebuilt boxes as '{SNAP_READY}'...")
            for t in rebuilt:
                take_snapshot(t.get("node") or node, t["vmid"], SNAP_READY,
                              description="tezcatlipoca: as delivered (rebuilt by redeploy)")
        else:
            print("  tz-ready NOT re-taken — domain configuration could not run (see above).")
    return rebuilt


def prepare_nakon_assets(comp_dir, state, teams, boxes, difficulty):
    """(nakon_config_path, nakon_bundle) for the replant modes.

    A function rather than main() inline so the reset ladder can build them LAZILY —
    a reset that settles at the tz-ready rung must not demand stage files it would
    never have used (a legacy comp with neither stage files nor box_password still
    gets its cheap rollback)."""
    # Pipeline v2+ (golden templates): repair re-plants run the POST-CLONE stage only —
    # the golden-stage installs ride the linked clone and re-running them over live
    # boxes mid-event is exactly what the stage split removed.
    if _is_golden_pipeline(state):
        postclone = comp_dir / ".nakon-postclone.json"
        if postclone.exists():
            # A comp whose plants are ALL golden-stage (e.g. every cde-2026-style pin)
            # has an empty postclone stage — live-found 2026-10-03: build_nakon_bundle
            # refuses an empty machine list, so a legitimate no-replant must not try.
            # Auth/DNS/service hardening below still re-runs; the probe judges health.
            if json.loads(postclone.read_text()).get("machines"):
                nakon_config_path = postclone
            else:
                print("  (postclone stage is empty — every plant rides the golden clone; "
                      "nakon has nothing to re-plant. Auth/DNS/hardening still re-runs.)")
                return None, None
        else:
            print("  WARNING: pipeline v2+ state but .nakon-postclone.json is missing — "
                  "regenerating the stage split from nakon-config.json")
            nakon_config_path = comp_dir / "nakon-config.json"
            if not nakon_config_path.exists():
                raise SystemExit(
                    "  ERROR: neither .nakon-postclone.json nor nakon-config.json exists — "
                    "cannot build the post-clone stage config for this mode.")
            teams_v2 = json.loads((comp_dir / "teams.json").read_text())
            pipeline_api.generate_stage_configs(
                comp_dir, teams_v2, boxes,
                unbooted=pipeline_api.unbooted_golden_boxes(comp_dir))
            nakon_config_path = postclone
    else:
        nakon_config_path = comp_dir / "nakon-config.json"
    if not nakon_config_path.exists() and not _is_golden_pipeline(state):
        box_password = state.get("box_password")
        if not box_password:
            raise SystemExit(
                "  ERROR: nakon-config.json is missing and .deploy_state.json has no "
                "box_password — can't regenerate the machine list (nakon authenticates to "
                "every box with it). This mode is unavailable for this competition."
            )
        print("  nakon-config.json missing — regenerating from the pinned service/vuln sets...")
        box_username, _credlist = load_users_config(comp_dir)
        nakon_config_path = pipeline_api.generate_nakon_config(
            teams, boxes, difficulty, comp_dir, box_password, box_username=box_username
        )
    return nakon_config_path, pipeline_api.build_nakon_bundle(nakon_config_path)


def scored_ports_for(comp_dir):
    """box_name -> sorted scored TCP ports, from box_services.json pins.

    Bare catalog names resolve through quotient's own _SERVICE_TO_CHECK — the same
    mapping that builds event.conf — so the probe tests exactly what Quotient will
    connect to. plant_only pins emit no scored check (their score rides a separate
    score/tcp pin that carries the port). Best-effort by design: a missing or
    unreadable box_services.json means fewer probed ports, never a crash — the SSH /
    guest-agent leg still gates the verdict."""
    path = comp_dir / "box_services.json"
    if not path.exists():
        return {}
    try:
        pins_by_box = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    # Vendored in-repo; private name is the single source of port truth for event.conf.
    from quotient.setup import _SERVICE_TO_CHECK
    ports = {}
    for box_name, pins in pins_by_box.items():
        out = set()
        for pin in pins or []:
            if isinstance(pin, dict) and pin.get("plant_only"):
                continue
            name = pin if isinstance(pin, str) else pin.get("name")
            port = pin.get("port") if isinstance(pin, dict) else None
            if not port:
                mapped = _SERVICE_TO_CHECK.get(name)
                port = mapped[1].get("Port") if mapped else None
            if port:
                try:
                    out.add(int(port))
                except (TypeError, ValueError):
                    pass
        if out:
            ports[box_name] = sorted(out)
    return ports


def ports_closed_from_engine(ctx, ip, ports, per_port_timeout=3):
    """The subset of `ports` the ENGINE cannot connect to on `ip` — Quotient's own
    vantage, so an in-path firewall is traversed exactly as scoring traverses it.
    Empty set = every scored port accepts a connection."""
    if not ports:
        return set()
    probe = "; ".join(
        f"timeout {per_port_timeout} bash -c 'echo > /dev/tcp/{ip}/{p}' 2>/dev/null "
        f"&& echo P{p}=OK || echo P{p}=CLOSED"
        for p in ports)
    try:
        r = ssh_to_engine(ctx, probe, timeout=per_port_timeout * len(ports) + 20)
    except Exception:
        return set(ports)
    if r.returncode != 0:
        return set(ports)
    tokens = set((r.stdout or "").split())
    return {p for p in ports if f"P{p}=OK" not in tokens}


RESET_PROBE_ATTEMPTS = 3
RESET_PROBE_GAP_S = 20


def probe_box_health(ctx, t, ports, node):
    """Health verdict for one box after a reset rung: ('healthy'|'pam-trap'|'unhealthy',
    detail). Healthy = the management transport answers (SSH via the gateway for Linux,
    guest agent for Windows) AND every scored port accepts a connection from the engine.

    The SSH leg doubles as the PAM planted-box trap detector: a tz-ready rollback boots
    a post-plant disk, and a box that fell into the trap dies at SSH preauth with its
    services possibly still listening — ports alone would call it healthy right before
    it fails every auth-based scored check."""
    windows = box_platform(t["box"]) == "windows"
    detail = "no attempt"
    for attempt in range(1, RESET_PROBE_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RESET_PROBE_GAP_S)
        if windows:
            if not wait_for_guest_agent(node, t["vmid"], timeout=20):
                detail = "guest agent silent"
                continue
        else:
            klass = classify_ssh_failure(ctx, t["ip"], user=ctx.get("box_username", "ubuntu"))
            if klass == "pam-trap":
                return "pam-trap", ("SSH dies at the PAM account stage preauth — planted-box "
                                    "restart trap (docs/known-issues.md)")
            if klass != "ok":
                detail = f"ssh {klass}"
                continue
        missing = ports_closed_from_engine(ctx, t["ip"], ports)
        if not missing:
            return "healthy", "guest agent + scored ports" if windows else "ssh + scored ports"
        detail = "closed scored port(s): " + ", ".join(str(p) for p in sorted(missing))
    return "unhealthy", detail


def mode_reset(targets, ctx, node, comp_dir, state, teams, boxes, difficulty):
    """Cheapest-that-works per-box reset: tz-ready rollback -> tz-base rollback + replant
    -> golden rebuild. Each rung only receives the boxes the previous rung left
    unhealthy (engine-vantage probe decides), so a box the cheap rung fixed is never
    escalated and a box it cannot fix is never left behind.

    A PAM-trap verdict escalates one rung rather than straight to rebuild: the trap
    lives on the post-plant disk, the tz-base disk is pre-plant, and rebuild remains
    the documented last resort (docs/known-issues.md)."""
    print(f"\n  Reset ladder: '{SNAP_READY}' rollback -> '{SNAP_BASE}' rollback + replant "
          f"-> golden rebuild. Escalating only the boxes each rung leaves unhealthy.")
    ports_by_box = scored_ports_for(comp_dir)
    levels = {}
    assets = []

    def nakon_assets():
        if not assets:
            assets.extend(prepare_nakon_assets(comp_dir, state, teams, boxes, difficulty))
        return assets

    def _rung(batch, run):
        """(restored, carry). On success carry is empty. On a wholesale failure
        (mode_rollback raises when NOTHING was restored, mode_rebuild on config
        errors) the rung touched nobody, so the whole batch carries to the next
        rung unprobed. KeyboardInterrupt still aborts."""
        try:
            return run(batch) or [], []
        except (Exception, SystemExit) as e:
            print(f"    rung failed wholesale ({type(e).__name__}: {e}) — escalating the batch")
            return [], list(batch)

    def _split_by_snapshot(batch, snap):
        have, missing = [], []
        for t in batch:
            if snap in list_snapshots(t.get("node") or node, t["vmid"]):
                have.append(t)
            else:
                missing.append(t)
        return have, missing

    def _probe_all(batch, level_label):
        """Probe a rung's output; healthy boxes earn their level label, and the boxes
        the next rung must handle are returned."""
        escalate = []
        for t in batch:
            verdict, detail = probe_box_health(ctx, t, ports_by_box.get(t["box_name"], ()), node)
            name = f"{t['team_key']}/{t['box_name']}"
            if verdict == "healthy":
                print(f"    {name}: healthy ({detail})")
                levels[t["vm_name"]] = level_label
            elif verdict == "pam-trap":
                print(f"    {name}: PAM PLANTED-BOX TRAP — {detail}")
                escalate.append(t)
            else:
                print(f"    {name}: unhealthy ({detail})")
                escalate.append(t)
        return escalate

    pending = list(targets)

    # Rung 1: the disk the competition started on.
    have_ready, pending = _split_by_snapshot(pending, SNAP_READY)
    for t in pending:
        print(f"    {t['team_key']}/{t['box_name']}: no '{SNAP_READY}' snapshot — starts at rung 2")
    if have_ready:
        print(f"\n  [reset 1/3] {len(have_ready)} box(es): rollback to '{SNAP_READY}'...")
        restored, carry = _rung(have_ready, lambda b: mode_rollback(
            b, ctx, node, SNAP_READY, comp_dir, state, None, None, reconfigure=False))
        pending += carry + _probe_all(restored, f"'{SNAP_READY}' rollback")

    # Rung 2: the pre-plant disk, re-planted and domain-chained by mode_rollback.
    have_base, pending = _split_by_snapshot(pending, SNAP_BASE)
    for t in pending:
        print(f"    {t['team_key']}/{t['box_name']}: no '{SNAP_BASE}' snapshot — starts at rebuild")
    if have_base:
        print(f"\n  [reset 2/3] {len(have_base)} box(es): rollback to '{SNAP_BASE}' + replant...")
        cfg, bundle = nakon_assets()
        restored, carry = _rung(have_base, lambda b: mode_rollback(
            b, ctx, node, SNAP_BASE, comp_dir, state, cfg, bundle, reconfigure=True))
        pending += carry + _probe_all(restored, f"'{SNAP_BASE}' rollback + replant")

    # Rung 3: golden rebuild — the documented PAM-trap workaround.
    if pending:
        print(f"\n  [reset 3/3] {len(pending)} box(es): rebuild from golden template...")
        cfg, bundle = nakon_assets()
        rebuilt, carry = _rung(pending, lambda b: mode_rebuild(
            b, ctx, node, comp_dir, state, cfg, bundle))
        pending += carry + _probe_all(rebuilt, "golden rebuild")

    print(f"\n{'='*64}")
    print("  Reset summary")
    print(f"{'='*64}")
    still_broken = []
    fixed = []
    for t in targets:
        name = f"{t['team_key']}/{t['box_name']}"
        label = levels.get(t["vm_name"])
        if label:
            fixed.append(t)
            print(f"    {name}: fixed via {label}")
        else:
            still_broken.append(t)
            print(f"    {name}: STILL BROKEN (every rung failed — see the per-box lines above)")
    if still_broken:
        raise SystemExit(
            f"\n  {len(still_broken)} box(es) remain broken after the full reset ladder — "
            f"diagnose by hand (verify-competition.py, engine console) before re-running.")
    return fixed


def quote_sshkeys(public_key):
    """Proxmox's `sshkeys` config param wants the key URL-encoded."""
    from urllib.parse import quote
    return quote(public_key.strip(), safe="")



def engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=False):
    """M4 engine recovery: re-clone the engine VM from the competition's engine
    template and apply per-deploy state fresh. Team boxes, golden templates, and the
    engine template are untouched. The scoring DB starts EMPTY (fresh volumes) —
    re-seed with create-competition.py --from-phase 7 afterwards."""
    if not state.get("engine_template_vmid"):
        raise SystemExit(
            "  ERROR: .deploy_state.json has no engine_template_vmid — this competition "
            "predates the M4 engine template; redeploy the range instead.")
    engine_vmid = int(state.get("scoring_vm_id") or 1000)
    acquire_engine_lock(engine_vmid)
    node = os.environ["TF_VAR_proxmox_node"]

    # Frozen semantics: recovery uses the node's template as-is; a hash mismatch is a
    # loud warning, never a mid-event blocker.
    expected = state.get("engine_template_hash")
    on_node = stored_template_hash(node, int(state["engine_template_vmid"]))
    if expected and on_node and on_node != expected:
        print(f"  WARNING: engine template on the node ({on_node[:12]}) differs from the "
              f"verified hash ({expected[:12]}) — recovering from the node's template "
              f"anyway. If this competition is frozen, investigate after the event.")

    if not assume_yes and not args_yes_engine_recovery():
        return False

    for p in ("postgres_password", "redis_password", "admin_password"):
        if not state.get(p):
            raise SystemExit(f"  ERROR: state has no {p} — cannot re-apply per-deploy "
                             f"engine state. Redeploy the range instead.")

    with timed(comp_dir, "recovery", "engine_recovery_total"):
        env = {**os.environ}
        env["TF_VAR_teams"] = json.dumps(teams)
        env["TF_VAR_boxes_per_team"] = json.dumps(boxes)
        env["TF_VAR_event_name"] = name
        tf_cwd = str(comp_dir / "terraform") if (comp_dir / "terraform" / "terraform.tfstate").exists() else "terraform"
        # -target scopes the plan to the engine + its post-boot null_resources so
        # terraform CANNOT touch team_box. Without it, any team box that drifted from
        # state — which a `--mode rebuild` does by design (API re-clone, not terraform) —
        # gets destroyed/recreated during 'engine recovery', wiping defenders' boxes
        # mid-event (winad-testrun 2026-09-25). -replace forces the engine rebuild.
        with timed(comp_dir, "recovery", "terraform_replace_engine"):
            run_terraform(
                ["apply", "-auto-approve", "-parallelism=1",
                 "-replace=proxmox_virtual_environment_vm.scoring_engine",
                 "-target=proxmox_virtual_environment_vm.scoring_engine",
                 "-target=null_resource.team_nics",
                 "-target=null_resource.reboot_scoring_engine",
                 "-target=null_resource.orchestrate"],
                cwd=tf_cwd, env=env, timeout=2400)
        ctx = pipeline_api.read_terraform_ctx(comp_dir)
        forget_engine_host_key(ctx["scoring_engine_ip"])
        wait_for_ssh(ctx["ssh_key_path"], ctx["vm_username"],
                     ctx["scoring_engine_ip"], timeout=300)
        with timed(comp_dir, "recovery", "engine_from_template"):
            prepare_engine_from_template(ctx, state["postgres_password"],
                                         state["redis_password"])
        push_event_conf(comp_dir, teams, boxes, ctx, name,
                        inject_password=state.get("inject_password"),
                        admin_password=state["admin_password"],
                        scoring_password=state.get("scoring_password"),
                        postgres_password=state["postgres_password"],
                        redis_password=state["redis_password"],
                        box_creds=state.get("box_creds") or {},
                        extra_credlists=({"domain": state["domain_creds"]}
                                         if state.get("domain_creds") else None))
        ensure_nat_forwarding(ctx)

    # The scoring DB is empty now, so phase 7's per-step done-flags are stale: without
    # clearing them the advertised --from-phase 7 re-seed skipped every step.
    state_path = comp_dir / ".deploy_state.json"
    for flag in ("seeded", "engine_unpaused", "injects_created", "injects_fingerprint"):
        state.pop(flag, None)
    # This write lands LAST in engine-recovery, after terraform already re-cloned the
    # engine and the scoring DB was wiped. A torn or short-lived-0644 write here leaves a
    # live engine with unreadable/eavesdroppable resume state and no second chance
    # (winad-testrun 2026-09-25) — always go through the shared atomic writer.
    write_state(state_path, state)

    print(f"\n  Engine recovered from template (vmid {state['engine_template_vmid']}).")
    print("  The scoring DB is EMPTY — re-seed with:")
    print(f"    python3 create-competition.py --competition {comp_dir.name} "
          f"--from-phase 7 --yes")
    return True


def reseed_event(comp_dir):
    """Phase 7 against the freshly recovered engine: seed, unpause, and create injects with
    offsets anchored at NOW. This is how a reset-and-rerun gets a live inject window —
    offsets resolve at phase-7 time, so a rollback-ready alone left every inject closed
    (winad-scrim2 2026-09-26: all 12 closed ~1.5h before T0)."""
    import subprocess
    subprocess.run([sys.executable, "create-competition.py", "--competition", comp_dir.name,
                    "--from-phase", "7", "--yes"], check=True)


def args_yes_engine_recovery():
    answer = input("  Recover the scoring engine from its template? The scoring DB "
                   "(teams/scores/injects) is LOST and must be re-seeded. [y/N] ").strip()
    return answer.lower() in ("y", "yes")


def main():
    parser = argparse.ArgumentParser(
        description="Redeploy a subset of a live competition's boxes without tearing down the "
                    "range. Selection flags combine with AND; with none given, every box in "
                    "the competition is selected.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # one team's boxes, back to the state the competition started in
  redeploy-competition.py --competition cde-2026 --teams 3

  # one box for one team
  redeploy-competition.py --competition cde-2026 --teams 3 --boxes web01

  # one team's linux boxes, rebuilt from tz-base and re-planted by Nakon
  redeploy-competition.py --competition cde-2026 --teams 3 --platform linux \\
      --mode rollback-base

  # see what would be touched, change nothing
  redeploy-competition.py --competition cde-2026 --teams 3 --dry-run

  # one box, cheapest reset that works (rollback -> replant -> rebuild as needed)
  redeploy-competition.py --competition cde-2026 --teams 3 --boxes web01 --mode reset
""",
    )
    parser.add_argument("--competition", help="Competition ID (competitions/<id>). Omit for a "
                                              "menu of deployed competitions.")
    parser.add_argument("--teams", help="Comma-separated teams: 'team2', '2' or the subnet "
                                        "identifier '102' all select the same team.")
    parser.add_argument("--boxes", help="Comma-separated box names from boxes.json, e.g. "
                                        "'web01,db01'.")
    parser.add_argument("--platform", choices=["linux", "windows"],
                        help="Only boxes whose template is this platform.")
    parser.add_argument("--mode", default="rollback-ready",
                        choices=["rollback-ready", "rollback-base", "reconfigure", "rebuild",
                                 "resync", "engine-recovery", "reset"],
                        help="What to do to the selected boxes (default: rollback-ready). "
                             "resync = align credentials with the engine and re-set box "
                             "passwords via the guest agent, touching nothing else. "
                             "engine-recovery = re-clone the engine VM from the engine "
                             "template (fresh empty scoring DB; re-seed with "
                             "--from-phase 7); ignores box selection flags. "
                             "reset = cheapest-that-works ladder per box: tz-ready "
                             "rollback, then tz-base rollback + replant, then golden "
                             "rebuild — escalating only the boxes each rung leaves "
                             "unhealthy, and reporting what each box ended up with.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Print the resolved targets and their snapshots, then exit.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--reset-event", action="store_true", dest="reset_event",
                        help="With rollback-ready/rollback-base: also restart the event from the "
                             "engine template (fresh scoring DB) and re-run phase 7, so scores "
                             "reset and injects re-open anchored at now. Use for scrim reruns.")
    args = parser.parse_args()

    comp_name = args.competition
    if comp_name is not None and not valid_comp_name(comp_name):
        raise SystemExit(f"  ERROR: invalid competition name {comp_name!r} — use [a-z0-9._-], no path separators.")
    if not comp_name:
        candidates = [
            p.name for p in sorted(Path("competitions").iterdir())
            if p.is_dir() and (p / "Compfile").exists() and (p / "teams.json").exists()
        ]
        if not candidates:
            raise SystemExit("No deployed competitions found (need Compfile + teams.json).")
        comp_name = pick_competition(candidates, label="deployed",
                                     action="Select a competition to redeploy from")
        if comp_name is None:
            raise SystemExit("Quitting.")

    comp_dir = Path("competitions") / comp_name
    if not comp_dir.is_dir():
        raise SystemExit(f"  ERROR: no such competition: {comp_dir}")

    name, _scenario, difficulty = load_compfile(comp_dir / "Compfile")
    teams_path = comp_dir / "teams.json"
    boxes_path = comp_dir / "boxes.json"
    state_path = comp_dir / ".deploy_state.json"
    for p in (teams_path, boxes_path):
        if not p.exists():
            raise SystemExit(f"  ERROR: {p} is missing — this competition was never deployed.")

    teams = json.loads(teams_path.read_text())
    boxes = json.loads(boxes_path.read_text())
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    # Multi-node: the placement record is authoritative — env -> engine host and
    # node routes registered before anything node-scoped (snapshots, clones).
    from nodes_ops import activate_placement, read_placement
    placement = read_placement(comp_dir)
    if placement:
        activate_placement(placement)

    if args.mode == "engine-recovery":
        engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=args.yes)
        return
    if args.reset_event and args.mode not in ("rollback-ready", "rollback-base", "reset"):
        raise SystemExit("  ERROR: --reset-event only applies to rollback-ready/rollback-base/reset.")

    targets = select_targets(comp_dir, teams, boxes, args)
    if not targets:
        raise SystemExit("  No boxes matched the given filters — nothing to do.")

    node = os.environ["TF_VAR_proxmox_node"]

    print(f"\n{'='*64}")
    print(f"  Redeploy — {name} ({comp_name})")
    print(f"{'='*64}")
    print(f"  Mode: {args.mode}")
    print(f"  {len(targets)} of {len(teams) * len(boxes)} box(es) selected:\n")
    snaps_by_box = {}
    for t in targets:
        tnode = t.get("node") or node
        snaps = list_snapshots(tnode, t["vmid"])
        snaps_by_box[t["vm_name"]] = snaps
        have = ", ".join(sorted(snaps)) if snaps else "none"
        try:
            cfg = proxmox_api("GET", f"/nodes/{tnode}/qemu/{t['vmid']}/config")["data"]
            tags = ", ".join(sorted(parse_vm_tags(cfg.get("tags")))) or "UNTAGGED"
        except Exception:
            tags = "?"
        print(f"    {describe_target(t)}  [snapshots: {have}]  [tags: {tags}]")
    print()

    if args.mode == "reset":
        print("  reset ladder — cheapest rung each box would start at:")
        for t in targets:
            snaps = snaps_by_box[t["vm_name"]]
            if SNAP_READY in snaps:
                first = f"1/3 ('{SNAP_READY}' rollback)"
            elif SNAP_BASE in snaps:
                first = f"2/3 ('{SNAP_BASE}' rollback + replant)"
            else:
                first = "3/3 golden rebuild — no usable snapshots"
            print(f"    {t['team_key']}/{t['box_name']}: rung {first}")
        print()

    if args.dry_run:
        print("  --dry-run: nothing was changed.")
        return

    needed = {"rollback-ready": SNAP_READY, "rollback-base": SNAP_BASE}.get(args.mode)
    if needed:
        missing = [t for t in targets if needed not in list_snapshots(t.get("node") or node, t["vmid"])]
        if missing:
            print(f"  ERROR: {len(missing)} selected box(es) have no '{needed}' snapshot:")
            for t in missing:
                print(f"    {describe_target(t)}")
            print("\n  " + snapshot_support_hint(node, missing[0]["vmid"]))
            raise SystemExit(1)

    if not args.yes:
        if args.mode not in ("reconfigure", "resync"):
            print("  This DISCARDS everything the defending team(s) have done to these boxes.")
        answer = input(f"  Redeploy {len(targets)} box(es) in mode '{args.mode}'? [y/N] ").strip()
        if answer.lower() not in ("y", "yes"):
            print("  Cancelled — nothing was changed.")
            return

    acquire_engine_lock(int(state.get("scoring_vm_id") or 1000))
    ctx = pipeline_api.read_terraform_ctx(comp_dir)

    nakon_config_path = nakon_bundle = None
    if args.mode in ("rollback-base", "reconfigure", "rebuild"):
        # mode_reset prepares these lazily — a reset that settles at the tz-ready rung
        # must not demand stage files it would never have used.
        nakon_config_path, nakon_bundle = prepare_nakon_assets(
            comp_dir, state, teams, boxes, difficulty)

    if args.mode == "rollback-ready":
        done = mode_rollback(targets, ctx, node, SNAP_READY, comp_dir, state,
                             nakon_config_path, nakon_bundle, reconfigure=False)
    elif args.mode == "rollback-base":
        done = mode_rollback(targets, ctx, node, SNAP_BASE, comp_dir, state,
                             nakon_config_path, nakon_bundle, reconfigure=True)
    elif args.mode == "reconfigure":
        done = mode_reconfigure(targets, ctx, comp_dir, state, nakon_config_path, nakon_bundle)
    elif args.mode == "resync":
        done = mode_resync(targets, ctx, node, comp_dir, state, state_path)
    elif args.mode == "reset":
        done = mode_reset(targets, ctx, node, comp_dir, state, teams, boxes, difficulty)
    else:
        done = mode_rebuild(targets, ctx, node, comp_dir, state, nakon_config_path, nakon_bundle)

    if args.reset_event:
        print("\n  --reset-event: restarting the event from the engine template...")
        if engine_recovery(name, comp_dir, teams, boxes, state, assume_yes=True):
            release_engine_lock()  # the reseed child takes the engine lock itself
            reseed_event(comp_dir)

    print(f"\n{'='*64}")
    print(f"  Redeployed {len(done)} box(es) in mode '{args.mode}'")
    print(f"{'='*64}")
    for t in done:
        print(f"    {describe_target(t)}")
    print(f"\n  Verify with: python3 verify-competition.py competitions/{comp_name}")
    print(f"  Scoreboard:  http://{ctx['scoring_engine_ip']}")


if __name__ == "__main__":
    main()
