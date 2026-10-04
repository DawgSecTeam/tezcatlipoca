"""Gate: this deploy's vmids/bridges must not collide with VMs/bridges that are not ours.

ONE implementation for single- and multi-node (a single-node plan is one share). The "is it
ours" question is answered by vm_ownership.ownership_verdict - the same proof the destroy guard
uses - so preflight and teardown can never disagree about whose VM a vmid holds."""

from dataclasses import dataclass

from constants import ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from pve_api import proxmox_api
from targets import vm_id_for
from vm_ownership import MARKER, OWNED, ownership_verdict, parse_vm_tags


@dataclass
class Slot:
    vmid: int
    label: str
    persistent_template: bool = False   # the engine template: expected to survive, reused


def expected_slots(share, boxes, engine_vmid):
    """Every vmid this share's deploy will create, with its report label.

    Order and wording differ between single- and multi-node only in the report (the vmid math
    is identical: goldens at engine+GOLDEN_VMID_OFFSET+slot*MAX_BOXES_PER_TEAM, teams by
    vm_id_for)."""
    slots = []
    if share.is_engine:
        slots.append(Slot(engine_vmid, f"scoring engine vmid {engine_vmid}"))
        # M4: the engine template's reserved slot (just below the golden block). A VM there
        # tagged as ours is the competition's persistent template — expected to survive
        # phase 1 and be reused, NOT a leftover to clean. Foreign = fatal.
        et = engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET
        slots.append(Slot(et, f"engine template vmid {et}", persistent_template=not share.multi))
    teams = [Slot(vm_id_for(t["identifier"], i), f"team vmid {vm_id_for(t['identifier'], i)} "
                  f"({k}/{boxes[i]['name']})")
             for k, t in share.teams.items() for i in range(len(boxes))]
    goldens = []
    for i in range(len(boxes)):
        vid = engine_vmid + GOLDEN_VMID_OFFSET + share.slot * MAX_BOXES_PER_TEAM + i
        goldens.append(Slot(vid, f"golden vmid {vid} ({boxes[i]['name']})" if not share.multi
                            else f"golden vmid (slot {share.slot}) #{vid}"))
    if share.multi:
        slots += goldens
        if share.slot > 0:
            slots.append(Slot(engine_vmid + 130 + share.slot,
                              f"jump vmid {engine_vmid + 130 + share.slot}"))
        slots += teams
    else:
        slots += teams + goldens
    return slots


def expected_bridges(share, boxes):
    """Team bridges (and, for in-path firewalls, the per-team transit bridge vmbrW<id>) this
    share creates. Transit bridges are engine-node resources (slot-0 teams)."""
    bridges = [f"vmbr{t['identifier']}" for t in share.teams.values()]
    if any(b.get("in_path") for b in boxes) and share.slot == 0:
        bridges += [f"vmbrW{t['identifier']}" for t in share.teams.values()]
    return bridges


def check_collisions(share, plan, vms):
    """Raise SystemExit on any foreign clash; report this competition's own leftovers."""
    node, comp_name, our_tags = share.node, plan.comp_name, plan.our_tags
    existing = {vm.get("vmid") for vm in vms}
    vm_by_vmid = {vm.get("vmid"): vm for vm in vms}

    def is_ours(vmid):
        # A retry of THIS competition's failed deploy meets its own leftovers: a VM carrying
        # the full ownership set is ours by creation (main.tf / clone_ops / golden_ops all tag)
        # and phase 1's ownership-checked cleanup destroys it. A same-comp VM MISSING the run id
        # belongs to a different run (another worktree's) and is a collision.
        vm = vm_by_vmid.get(vmid)
        if vm is None:
            return False
        verdict, _ = ownership_verdict(node, vmid, vm.get("tags"), our_tags)
        if verdict == MARKER:
            # Interrupted clone: untagged (the tag PUT never ran) but carrying the clone
            # marker written in the clone POST itself — ours; phase 1 unlocks + destroys it.
            print(f"  Preflight{share.label}: vmid {vmid} is an interrupted clone of this "
                  f"competition (lock={vm.get('lock') or 'none'}) — phase 1 will clean it")
        return verdict in (OWNED, MARKER)

    clashes, foreign, ours = [], 0, 0
    for slot in expected_slots(share, plan.boxes, plan.engine_vmid):
        if slot.vmid not in existing:
            continue
        if is_ours(slot.vmid):
            if slot.persistent_template:
                print(f"  Preflight: engine template vmid {slot.vmid} present "
                      f"(M4 persistent — reused)")
            else:
                ours += 1
        else:
            foreign += 1
            clashes.append(slot.label)

    try:
        nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
        existing_bridges = {n.get("iface") for n in nets}
    except Exception:
        existing_bridges = set()
    for bridge in expected_bridges(share, plan.boxes):
        # Bridges carry no per-comp tags. Tolerate one only when the VM leftovers are
        # unambiguously all ours (a retry) — a foreign bridge stays fatal.
        if bridge in existing_bridges:
            if foreign == 0 and ours > 0:
                ours += 1
            else:
                clashes.append(f"bridge {bridge}")

    if clashes:
        run = plan.our_run_tag
        other_run = any(
            f"comp-{comp_name}" in str(vm.get("tags") or "")
            and run and run not in parse_vm_tags(vm.get("tags"))
            for vm in vm_by_vmid.values())
        if share.multi:
            advice = ("Pick a free --scoring-vmid and/or non-overlapping team identifiers.")
        else:
            advice = ("Pick a free --scoring-vmid and/or non-overlapping TF_VAR_team_identifiers. "
                      "Note the golden block sits at <scoring-vmid>+150 — a colliding golden "
                      "vmid also means picking a different engine vmid.")
        raise SystemExit(
            f"  ERROR: this competition's infrastructure collides with VMs/bridges already "
            f"on {share.where}"
            + ("" if share.multi else " (another running competition?)")
            + ": " + ", ".join(clashes) + ". " + advice
            + (" The colliding VM(s) carry this competition's comp tag but NOT this "
               "run's run-id tag: ANOTHER worktree's run of the same competition ID "
               "(coordinate with its session — never destroy it from here)." if other_run else ""))
    if ours:
        print(f"  Preflight{share.label}: {ours} leftover VM(s)/bridge(s) tagged as this "
              f"competition's — phase 1 cleans or "
              + ("reuses them" if share.multi else "(M4 hash-matching templates) reuses them"))
    elif not share.multi:
        print(f"  Preflight: engine vmid {plan.engine_vmid}, engine template vmid "
              f"{plan.engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET}, golden vmids "
              f"{plan.engine_vmid + GOLDEN_VMID_OFFSET}+, all team vmids, and team bridges are free")
