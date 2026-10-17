"""Estate teardown in the one order linked clones survive.

A clone shares its base volume — a golden's for a team box, the engine template's for the
deployed engine — so destroying a base first fails with 'base volume ... is still in use by
linked clone' (a teardown diagnostic log shows it), and a bridge cannot go while a VM NIC
still references it. TIERS is that fixed order and is the only thing that decides it;
`_CLONE_KINDS` re-checks it for the case a clone tier REFUSED and carried on.
"""

from common.constants import GOLDEN_TAG
from infra.client import destroy_bridge_if_exists, node_bridges
from infra.ownership import (Ownership, Refusal, bridge_owned_by, destroy_vm_if_exists,
                             ownership_identity)

TIERS = (("team_box", "jump"), ("golden",), ("engine",), ("engine_template",), ("bridge",))

# Only a TEMPLATE carries a role tag; the deployed engine is an ordinary clone proven by the
# ownership set alone, so it must NOT be narrowed (requiring "engine-template" on it would
# make every teardown refuse its own engine).
_ROLE = {"golden": (GOLDEN_TAG,), "engine_template": ("engine-template",)}

# A base kind -> the clone kinds whose survivors keep its volume in use. A refused clone
# stays recorded (that is the tier's contract), so the base must be skipped, not attempted.
_CLONE_KINDS = {"golden": ("team_box", "jump"), "engine_template": ("engine",)}

_KEPT_WHEN_TEAMS_ONLY = ("golden", "engine_template")


def _clones_remain(state, kind, nodes):
    """Whether any linked clone of a `kind` base is still recorded."""
    return any(nodes is None or row["node"] in nodes
               for clone in _CLONE_KINDS[kind] for row in state.resources(clone))


def _entry(row):
    return {"kind": row["kind"], "node": row["node"], "name": row["name"],
            "vmid": row["vmid"], "bridge": row["bridge"]}


def destroy_tier(state, kind, *, nodes=None, only=None, log=print):
    """Destroy every recorded resource of one kind; returns (destroyed, refused, skipped).

    `only` narrows to a set of vmids (for VMs) or bridge names — the per-team teardown
    destroys one team's resources without touching the others.
    """
    identity = state.data.get("identity") or {}
    comp_name = ownership_identity(state.comp_dir, identity.get("comp_name"))
    ownership = Ownership(comp_name, identity.get("run_id") or "")
    destroyed, refused, skipped = [], [], []
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
                destroy_vm_if_exists(row["node"], row["vmid"], expect_tags=expect)
            else:
                # A bridge row carries its NAME ("vmbr120"); `bridge` is only set by the
                # per-team paths. Reading row["bridge"] alone passed None and the delete
                # silently no-opped while the row was forgotten and reported destroyed —
                # caught live left vmbr151 active through a "clean" teardown.
                name = row["bridge"] or row["name"]
                # A bridge carries no tags, so its comment is the whole ownership proof:
                # never remove one that cannot be attributed to this run.
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
            # A refusal is a verdict the caller needs, not a step failure: leave the
            # entry in state and carry on with the rest of the tier.
            refused.append({"entry": _entry(row), "reason": e.detail})
            log(f"  REFUSED {kind} {row['name']}: {e.detail}")
            continue
        state.forget(kind, name=row["name"], vmid=row["vmid"], bridge=row["bridge"])
        destroyed.append(_entry(row))
        log(f"  destroyed {kind} {row['name']}")
    return destroyed, refused, skipped


def destroy_estate(state, *, full=True, nodes=None, log=print):
    """Destroy the state's estate in TIERS order; returns the teardown summary."""
    identity = state.data.get("identity") or {}
    comp_name = ownership_identity(state.comp_dir, identity.get("comp_name"))
    run_id = identity.get("run_id") or ""
    refused, skipped, killed = [], [], []
    for tier in TIERS:
        for kind in tier:
            if not full and kind in _KEPT_WHEN_TEAMS_ONLY:
                skipped += [_entry(row) for row in state.resources(kind)
                            if nodes is None or row["node"] in nodes]
                continue
            if kind in _CLONE_KINDS and _clones_remain(state, kind, nodes):
                # A previous tier refused a clone; its base volume is still in use, so
                # deleting the base could only fail the whole estate step.
                held = [_entry(row) for row in state.resources(kind)
                        if nodes is None or row["node"] in nodes]
                skipped += held
                log(f"  skipped {kind} — a linked clone is still recorded "
                    f"({', '.join(sorted(_CLONE_KINDS[kind]))})")
                continue
            destroyed, tier_refused, tier_skipped = destroy_tier(state, kind, nodes=nodes, log=log)
            killed += destroyed
            refused += tier_refused
            skipped += tier_skipped
    # A bridge created but never recorded (the state write failed) is still ours: its comment
    # proves it, and the tier above only walks the record. No estate (or no run id) means
    # there is nothing to attribute, so the node is not even asked.
    for node in (nodes or []) if run_id and state.resources() else []:
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
    return {"destroyed": killed, "skipped": skipped, "refused": refused}
