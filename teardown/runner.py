"""run_terminal_teardown: the shared executor behind both terminal-teardown surfaces.

destroy-competition.py (interactive, with banner + confirm) and the scrim
harness (in-process, already committed to tearing down) both come through
here: gates -> confirm -> context -> plan -> engine -> verify_clean. The
caller turns the CleanReport into its own exit code / exception."""

import json
import os
from pathlib import Path

from multinode.placement_record import (activate_placement, engine_record, read_placement,
                                        record_of)
from teardown import (Inventory, TeardownContext, TeardownEngine, load_ownership,
                      refuse_frozen_full_teardown, require_terraform_state)
from teardown.engine import TEARDOWN_JOURNAL
from teardown.modes import build_terminal_plan
from engine.template_freeze import frozen_state


def run_terminal_teardown(comp_dir, teams, boxes, name, *, env=None, full=True,
                          end_of_competition=False, keep_red=False,
                          artifacts_timeout=45, confirm=None, log=print):
    comp_dir = Path(comp_dir)
    competition = comp_dir.name
    frozen = frozen_state(comp_dir)

    # Multi-node: the placement record is authoritative — point the env at the
    # engine's host and register the node routes before anything node-scoped runs.
    placement = read_placement(comp_dir)
    default_node = os.environ.get("TF_VAR_proxmox_node", "pve")
    placement_nodes = [default_node]
    if placement:
        # Every node-scoped API path needs the PVE node NAME, not the nodes-list record name
        # (activate_placement keys the route table by PVE name): the engine entry used the
        # record name, so the inventory, the pre-stop and the leftover sweep all listed
        # /nodes/<record>/qemu and 500'd, and a finished teardown reported NOT CLEAN
        # (2026-10-14; the satellite entries were already mapped).
        placement_nodes = [engine_record(placement).node] + [
            record_of(placement, s["name"]).node for s in placement["satellites"]]
        activate_placement(placement)
        sats = ", ".join(f"{s['name']} (slot {s['slot']}, teams {','.join(s['teams'])})"
                         for s in placement["satellites"])
        log(f"  Multi-node placement: engine on '{placement['engine_node']}'"
            + (f"; satellites: {sats}" if sats else ""))

    # Every refusal fires BEFORE any destruction — a frozen competition's
    # templates are the verified artifacts the event runs on; a state without
    # its run id names no ownership; a missing tfstate makes destroy a no-op.
    refuse_frozen_full_teardown(competition, frozen, full, end_of_competition)
    deployed_state, ownership = load_ownership(comp_dir)
    require_terraform_state(comp_dir, deployed_state)
    log(f"  Ownership: run id '{ownership.run_id}' — only VMs carrying this deploy's "
        f"FULL tag set ({sorted(ownership.tags)}) will be touched.")

    if confirm and not confirm(competition):
        return None

    env = dict(env if env is not None else os.environ)
    env["TF_VAR_teams"] = json.dumps(teams)
    env["TF_VAR_boxes_per_team"] = json.dumps(boxes)
    env["TF_VAR_event_name"] = name

    ctx = TeardownContext(
        comp_dir=comp_dir, env=env, teams=teams, boxes=boxes,
        deployed_state=deployed_state, ownership=ownership, placement=placement,
        nodes=placement_nodes, default_node=default_node,
        inventory=Inventory(placement_nodes),
        full=full, keep_red=keep_red, artifacts_timeout=artifacts_timeout)
    plan = build_terminal_plan()
    engine = TeardownEngine(ctx, log=log)
    results = engine.execute(plan)
    return engine.verify_clean(plan, results)


# records steps the CALLER already completed (the scrim harness runs the evidence pull/collect/report and its superior red teardown itself) so the standard plan skips them instead of re-running them against dead boxes
def record_evidence_done(comp_dir, test_dir=None, red_vmid=None):
    from config.config_ops import write_text_atomic
    from teardown import Refusal

    comp_dir = Path(comp_dir)
    done = {}
    if test_dir:
        done["artifacts"] = {"detail": "collected by the harness teardown",
                             "test_dir": str(test_dir)}
    if red_vmid:
        done["red01"] = {"detail": "harness teardown_red verified the vmid gone",
                         "vmid": red_vmid}
    if not done:
        return
    _, ownership = load_ownership(comp_dir)     # Refusal: same verdict the runner would give
    write_text_atomic(comp_dir / TEARDOWN_JOURNAL,
                      json.dumps({"run_id": ownership.run_id, "done": done}, indent=2),
                      mode=0o600)
