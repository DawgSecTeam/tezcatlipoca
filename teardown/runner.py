"""run_terminal_teardown: the shared executor behind both terminal-teardown surfaces."""

import json
import os
from pathlib import Path

from multinode.placement_record import (activate_placement, engine_record, read_placement,
                                        record_of)
from teardown import (Inventory, TeardownContext, TeardownEngine, load_ownership,
                      refuse_frozen_full_teardown)
from teardown.engine import save_teardown_journal
from teardown.modes import build_terminal_plan
from engine.template_freeze import frozen_state
from common.utils import competition_name
from estate import config


def run_terminal_teardown(comp_dir, teams, boxes, name, *, env=None, full=True,
                          end_of_competition=False, keep_red=False,
                          artifacts_timeout=45, confirm=None, log=print):
    comp_dir = Path(comp_dir)
    competition = competition_name(comp_dir)
    frozen = frozen_state(comp_dir)

    # Multi-node: the placement record is authoritative — point the process config at the
    # engine's host and register the node routes before anything node-scoped runs.
    placement = read_placement(comp_dir)
    default_node = config.active().proxmox.node
    placement_nodes = [default_node]
    if placement:
        # node-scoped API paths need the PVE node NAME, not the record name (activate_placement
        # keys routes by PVE name): using the record name 500'd every path and made a finished
        # teardown report NOT CLEAN.
        placement_nodes = [engine_record(placement).node] + [
            record_of(placement, s["name"]).node for s in placement["satellites"]]
        activate_placement(placement)
        sats = ", ".join(f"{s['name']} (slot {s['slot']}, teams {','.join(s['teams'])})"
                         for s in placement["satellites"])
        log(f"  Multi-node placement: engine on '{placement['engine_node']}'"
            + (f"; satellites: {sats}" if sats else ""))

    # Every refusal fires BEFORE any destruction: a frozen comp's templates are the verified
    # artifacts the event runs on, and a state without its run id names no ownership.
    refuse_frozen_full_teardown(competition, frozen, full, end_of_competition)
    deployed_state, ownership = load_ownership(comp_dir)
    log(f"  Ownership: run id '{ownership.run_id}' — only VMs carrying this deploy's "
        f"FULL tag set ({sorted(ownership.tags)}) will be touched.")

    if confirm and not confirm(competition):
        return None

    env = dict(env if env is not None else os.environ)
    if placement:
        # Callers snapshot os.environ BEFORE activate_placement runs, so their dict still named
        # the profile's host while the range lives on the placement's host; activation has moved
        # the process, so project the resolved identity back onto this dict.
        env.update(config.proxmox_env())

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


# records steps the CALLER already completed (the scrim harness) so the standard plan skips them
def record_evidence_done(comp_dir, test_dir=None, red_vmid=None):

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
    save_teardown_journal(comp_dir, {"run_id": ownership.run_id, "done": done})
