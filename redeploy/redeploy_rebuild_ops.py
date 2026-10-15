"""Redeploy mode rebuild: re-clone a box from its golden template and re-plant."""

import json
import re
import pipeline_api

from estate import state as estate_state

from common.constants import SNAP_BASE, SNAP_READY, ownership_tags
from nakonops.nakon_bundle_ops import build_nakon_bundle
from infra.client import proxmox_api, wait_for_proxmox_task
from infra.lifecycle import start_vm, take_snapshot
from infra.ownership import clone_marker, destroy_vm_if_exists
from common.targets import describe_target
from engine.template_freeze import stored_template_hash
from common.timing import timed
from common.ssh_ops import quote_sshkeys
from common.utils import compfile_flag
from pathlib import Path
from redeploy.redeploy_select_ops import box_platform
from redeploy.redeploy_plant_ops import rerun_domain_configs
from estate import config as estate_config


def mode_rebuild(targets, rctx, nakon_config_path, nakon_bundle):
# Recreate VM from template then configure (clones template, not team1 live box).
    ctx, node, comp_dir, state = rctx.ctx, rctx.node, rctx.comp_dir, rctx.state
    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: the state document has no box_password — can't recreate a box with the "
            "login the rest of the range uses. Rebuild is unavailable for this competition."
        )
    ssh_public_key = estate_config.active().ssh.public_key
    box_username = ctx.get("box_username", "ubuntu")

    rebuilt = []
    for t in targets:
        tnode = t.get("node") or node
        box = t["box"]
        windows = box_platform(box) == "windows"
        full_clone = True
        src_vmid = None
        src_label = None
        # Multi-node: the box's golden is its own node's slot copy (state carries
        # per-slot ids); single-node keeps the flat map.
        by_slot = state.get("golden_ids_by_slot") or {}
        if by_slot:
            from multinode.placement_record import read_placement, slot_of_team
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
                f"  ERROR: state has no golden template for '{box['template']}' "
                f"(slot golden ids: {sorted(slot_ids)}) — a rebuild from the ORIGINAL "
                f"template would miss every golden-stage install (services, vulns). "
                f"Redeploy the range instead.")
        print(f"  Rebuilding {describe_target(t)} from {src_label}...")

        # Ownership guard: the state's run id is the destruction anchor. A box that
        # belongs to another run of the same competition ID (a second worktree) is
        # refused — rebuilding over it would eat someone else's range.
        destroy_vm_if_exists(tnode, t["vmid"],
                             expect_tags=ownership_tags(comp_dir.name, state["run_id"]))

        upid = proxmox_api("POST", f"/nodes/{tnode}/qemu/{src_vmid}/clone", data={
            "newid": t["vmid"],
            "name": t["vm_name"],
            "full": 1 if full_clone else 0,
            # /clone takes no tags, and the tagging PUT below only lands after the clone
            # task finishes — a kill mid-clone would otherwise leave an unmarked VM
            # squatting our vmid slot. The description exists from the clone's first
            # instant (infra.ownership.clone_marker; winad-testrun 2026-09-25).
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
            "tags": ";".join(sorted(ownership_tags(comp_dir.name, state["run_id"]))),
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

        # The rebuilt box carries the current run's tags, so the next deploy adopts it
        # rather than cloning a second one into the same slot.
        print(f"    NOTE: {t['vm_name']} was recreated outside the deploy. It carries this "
              f"run's ownership tags, so the next deploy adopts it instead of rebuilding.")

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
        predomain_path = comp_dir / ".nakon-predomain.json"
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
            scoring_user = estate_config.active().engine.username
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
                      f"FAILED step(s) (recorded in the state document)")
                state["nakon_failed_steps"] = list(state.get("nakon_failed_steps") or []) + [
                    f"redeploy {stage}-stage: {line}" for line in result.failed[:20]]
                estate_state.write_flat(comp_dir, state)

        print("  Pre-domain pass (sshd/sudoers) on rebuilt boxes...")
        _stage_pass(predomain_path, "predomain")
        pipeline_api.fix_services_on_boxes(
            comp_dir, [t for t in rebuilt if box_platform(t["box"]) == "linux"],
            ctx, box_creds=state.get("box_creds"))

        try:
            domain_settled = rerun_domain_configs(rebuilt, rctx, nakon_config_path)
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


def reconcile_estate_state(rebuilt, rctx):
# Re-point each rebuilt box's inventory record at the VM that now holds its vmid.
#
#     mode_rebuild recreates boxes through the API, so the recorded entry still names the
#     destroyed VM. Re-recording by deterministic vmid re-pins the inventory to the live
#     box, which is what the teardown destroys and the preflight checks. Idempotent: an
#     entry already naming this vmid is upserted in place.
    from estate import state as estate_state
    document = estate_state.EstateState.load(rctx.comp_dir)
    for t in rebuilt:
        node = t.get("node") or estate_state.load_flat(rctx.comp_dir).get("node") \
            or estate_config.active().proxmox.node
        print(f"  Recording {t['vm_name']} (vmid {t['vmid']}) in the inventory...")
        document.record("team_box", node, t["vm_name"], vmid=t["vmid"])
    document.save()
