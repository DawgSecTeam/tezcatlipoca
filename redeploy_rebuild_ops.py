"""Redeploy mode rebuild: re-clone a box from its golden template and re-plant."""

import json
import os
import re
import pipeline_api

from config_ops import write_state
from constants import SNAP_BASE, SNAP_READY, ownership_tags
from nakon_ops import build_nakon_bundle
from range_ops import (
    clone_marker,
    terraform_dir,
    describe_target,
    destroy_vm_if_exists,
    proxmox_api,
    start_vm,
    take_snapshot,
    wait_for_proxmox_task,
)
from template_ops import stored_template_hash
from timing import timed
from ssh_ops import quote_sshkeys
from utils import compfile_flag, run_terraform
from pathlib import Path
from redeploy_select_ops import box_platform
from redeploy_plant_ops import rerun_domain_configs


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

        # Every team is a Terraform resource (`team_box` for_each covers all teams;
        # satellites get team_box_sat1..4), so EVERY rebuilt box drifts from state. Fine
        # mid-event; nobody runs a full apply against a live range, but the next one will
        # want to replace these.
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


def reconcile_terraform_state(rebuilt, ctx, comp_dir, teams, boxes):
    """`terraform import` each rebuilt box back into state (opt-in --reconcile-state).

    mode_rebuild recreates boxes through the API, so on a v2+ range every rebuilt box
    is a live terraform resource the state no longer matches — the next full
    `terraform apply` sees drift and REPLACES it, destroying the box again (warned
    about since the tool existed). Import re-pins the state to the recreated VM with
    its deterministic vmid, after which a plan reports the box clean. Import ID is
    the vmid; the for_each key lives in the address (slot 0 = team_box, satellites =
    team_box_sat1..4). A failed import is printed, never fatal — the warning path
    (accept the replacement, or `terraform state rm`) remains the fallback."""
    env = {**os.environ,
           "TF_VAR_teams": json.dumps(teams),
           "TF_VAR_boxes_per_team": json.dumps(boxes)}
    tf_cwd = str(terraform_dir(comp_dir))
    node = os.environ["TF_VAR_proxmox_node"]
    for t in rebuilt:
        slot = t.get("slot") or 0
        resource = "proxmox_virtual_environment_vm.team_box" if slot == 0 else \
            f"proxmox_virtual_environment_vm.team_box_sat{slot}"
        addr = f'{resource}["{t["vm_name"]}"]'
        print(f"  Importing {t['vm_name']} (vmid {t['vmid']}) into terraform state...")
        try:
            # The address is usually ALREADY in state (the deploy's apply #2 created
            # it; the rebuild replaced the VM out-of-band) and `terraform import`
            # refuses managed addresses — live-found 2026-10-04: "Resource already
            # managed by Terraform". Forget the stale entry first, then adopt the
            # live VM. A rm of an absent address is fine.
            run_terraform(["state", "rm", addr], cwd=tf_cwd, env=env, timeout=60,
                          check=False)
            # The bpg provider's import ID for VM resources is "node/vmid", not a
            # bare vmid (live-found 2026-10-04: "unexpected format of ID ()").
            run_terraform(["import", addr, f"{node}/{t['vmid']}"], cwd=tf_cwd, env=env,
                          timeout=300)
        except Exception as e:
            print(f"    WARNING: import failed for {addr}: {str(e)[:160]} — the box stays "
                  f"drifted from state; accept the next apply's replacement or "
                  f"`terraform state rm` + import by hand.")
    print("  Confirm with: terraform plan  (run in " + tf_cwd + " — the rebuilt box(es) "
          "should show no changes)")
