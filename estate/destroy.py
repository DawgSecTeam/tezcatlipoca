"""Estate teardown in the one order linked clones survive."""

from common.constants import GOLDEN_TAG, comp_tag
from infra.client import destroy_bridge_if_exists, node_bridges
from infra.ownership import (MARKER, OWNED, Ownership, Refusal, bridge_owned_by,
                             destroy_vm_if_exists, ownership_identity, vm_occupancy)

TIERS = (("team_box", "jump"), ("golden",), ("engine",), ("engine_template",), ("bridge",))

# Only a TEMPLATE carries a role tag; narrowing the deployed engine to "engine-template" would make teardown refuse its own engine.
_ROLE = {"golden": (GOLDEN_TAG,), "engine_template": ("engine-template",)}

# Clone kinds whose survivors keep the base volume in use; a refused clone stays recorded.
_CLONE_KINDS = {"golden": ("team_box", "jump"), "engine_template": ("engine",)}

_KEPT_WHEN_TEAMS_ONLY = ("golden", "engine_template")


def _clones_remain(state, kind, nodes):
    return any(nodes is None or row["node"] in nodes
               for clone in _CLONE_KINDS[kind] for row in state.resources(clone))


def _entry(row):
    return {"kind": row["kind"], "node": row["node"], "name": row["name"],
            "vmid": row["vmid"], "bridge": row["bridge"]}


# A template is the competition's cache, not the recording run's property: every later deploy
# of the same competition re-stamps it, so the record's owner is not a destruction proof.
def _template_disposition(node, row, expect, comp_name, role):
    """(tags this VM may be destroyed under, reason to leave it) for a recorded template."""
    verdict, tags, vm = vm_occupancy(node, row["vmid"], expect)
    if verdict in (OWNED, MARKER) or verdict == "absent":
        return None, None
    if comp_tag(comp_name) not in tags or role not in tags:
        return None, None                 # foreign: let the strict guard refuse it
    owners = sorted(t for t in tags if t.startswith("run-"))
    if owners:
        return None, (f"{vm.get('name')} (vmid {row['vmid']}) carries {', '.join(owners)} — "
                      f"another run of this competition owns it")
    # A kept template with no run tag belongs to the competition alone; a full teardown is
    # the documented path that reclaims it, and its own run's identity is unprovable anyway.
    return Ownership(comp_name).with_role(role).tags, None


def destroy_tier(state, kind, *, nodes=None, only=None, log=print):
    """Destroy every recorded resource of one kind; `only` narrows to a set of vmids or bridge names."""
    identity = state.data.get("identity") or {}
    comp_name = ownership_identity(state.comp_dir, identity.get("comp_name"))
    ownership = Ownership(comp_name, identity.get("run_id") or "")
    destroyed, refused, skipped, kept = [], [], [], []
    for row in state.resources(kind):
        if nodes is not None and row["node"] not in nodes:
            skipped.append(_entry(row))
            continue
        if only is not None:
            identifier = row["vmid"] if row["vmid"] is not None \
                else (row["bridge"] or row["name"])
            if identifier not in only:
                continue
        try:
            if row["vmid"] is not None:
                expect = ownership.with_role(*_ROLE.get(kind, ())).tags
                if kind in _ROLE:
                    reclaim, owner = _template_disposition(
                        row["node"], row, expect, comp_name, _ROLE[kind][0])
                    if owner:
                        kept.append({**_entry(row), "reason": owner})
                        state.forget(kind, name=row["name"], vmid=row["vmid"],
                                     bridge=row["bridge"])
                        log(f"  kept {kind} {row['name']}: {owner}")
                        continue
                    if reclaim is not None:
                        log(f"  reclaiming {kind} {row['name']}: vmid {row['vmid']} "
                            f"carries no run tag")
                        expect = reclaim
                destroy_vm_if_exists(row["node"], row["vmid"], expect_tags=expect)
            else:
                # row["bridge"] is only set by the per-team paths, so reading it alone passed None and the delete silently no-opped.
                name = row["bridge"] or row["name"]
                comments = node_bridges(row["node"])
                if name in comments and not bridge_owned_by(
                        comments[name], comp_name, identity.get("run_id") or ""):
                    refused.append({"entry": _entry(row),
                                    "reason": f"bridge {name} on {row['node']} is not this "
                                              f"run's (comment: {comments[name]!r})"})
                    log(f"  REFUSED {kind} {row['name']}: bridge {name} is not attributable "
                        f"to this run — not removed")
                    continue
                if not destroy_bridge_if_exists(row["node"], name):
                    refused.append({"entry": _entry(row),
                                    "reason": f"bridge {name} is still on {row['node']} "
                                              f"after its delete"})
                    log(f"  REFUSED {kind} {row['name']}: bridge {name} survived the delete")
                    continue
        except Refusal as e:
            # A refusal is a verdict the caller needs: leave the entry in state and carry on.
            refused.append({"entry": _entry(row), "reason": e.detail})
            log(f"  REFUSED {kind} {row['name']}: {e.detail}")
            continue
        state.forget(kind, name=row["name"], vmid=row["vmid"], bridge=row["bridge"])
        destroyed.append(_entry(row))
        log(f"  destroyed {kind} {row['name']}")
    return destroyed, refused, skipped, kept


def destroy_estate(state, *, full=True, nodes=None, log=print):
    identity = state.data.get("identity") or {}
    comp_name = ownership_identity(state.comp_dir, identity.get("comp_name"))
    run_id = identity.get("run_id") or ""
    refused, skipped, killed, kept = [], [], [], []
    # Snapshot BEFORE the tiers forget their entries: the unrecorded-bridge sweep below reads
    # `state.resources()`, which is empty by then (and the node list is needed either way).
    recorded_nodes = sorted({r["node"] for r in state.resources() if r.get("node")})
    for tier in TIERS:
        for kind in tier:
            if not full and kind in _KEPT_WHEN_TEAMS_ONLY:
                skipped += [_entry(row) for row in state.resources(kind)
                            if nodes is None or row["node"] in nodes]
                continue
            if kind in _CLONE_KINDS and _clones_remain(state, kind, nodes):
                # A previous tier refused a clone; its base volume is still in use.
                held = [_entry(row) for row in state.resources(kind)
                        if nodes is None or row["node"] in nodes]
                skipped += held
                log(f"  skipped {kind} — a linked clone is still recorded "
                    f"({', '.join(sorted(_CLONE_KINDS[kind]))})")
                continue
            destroyed, tier_refused, tier_skipped, tier_kept = destroy_tier(
                state, kind, nodes=nodes, log=log)
            killed += destroyed
            refused += tier_refused
            skipped += tier_skipped
            kept += tier_kept
    # A bridge created but never recorded (the state write failed) is still ours if its comment proves it.
    sweep_nodes = nodes if nodes is not None else recorded_nodes
    for node in sweep_nodes if run_id else []:
        for iface, comment in node_bridges(node).items():
            if not iface.startswith("vmbr") or not bridge_owned_by(
                    comment, comp_name, run_id):
                continue
            if any(r["node"] == node and (r["bridge"] or r["name"]) == iface
                   for r in state.resources("bridge")):
                continue
            if destroy_bridge_if_exists(node, iface):
                log(f"  destroyed unrecorded bridge {iface} on {node}")
                killed.append({"kind": "bridge", "node": node, "name": iface, "vmid": None,
                               "bridge": iface})
    state.save()
    return {"destroyed": killed, "skipped": skipped, "refused": refused, "kept": kept}
