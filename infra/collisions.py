"""Collision gates: what this deploy will create must not already exist as someone else's.

Two families, one module:

  - vmid/bridge slot collisions (check_collisions): this deploy's vmids and bridges must not
    collide with VMs/bridges that are not ours. ONE implementation for single- and multi-node
    (a single-node plan is one share). The "is it ours" question is answered by
    infra.ownership.ownership_verdict - the same proof the destroy guard uses - so preflight
    and teardown can never disagree about whose VM a vmid holds.
  - management-IP collisions (engine_mgmt_ip_gate / jump_mgmt_ip_gate): the engine's static
    mgmt IP and each satellite's jump mgmt IP must not already be answered by a running guest
    on the (cross-node) management L2."""

import os
from dataclasses import dataclass

from constants import ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from infra.client import proxmox_api
from targets import vm_id_for
from infra.ownership import MARKER, OWNED, ownership_verdict, parse_vm_tags


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


def guest_ipv4s(vm):
    """IPv4 addresses a running guest reports through its agent, or None when it can't be asked."""
    try:
        result = proxmox_api(
            "GET", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/agent/network-get-interfaces"
        )["data"]["result"]
    except Exception:
        return None
    return [addr.get("ip-address") for ifc in result or []
            for addr in ifc.get("ip-addresses") or []
            if addr.get("ip-address-type") == "ipv4"]


def engine_mgmt_ip_gate(node, vms, engine_vmid, engine_mgmt_ip, ours_tags=None):
    """Static engine mgmt IP must not collide with any running guest on the mgmt L2.

    Two things this gate got wrong, both live-found on the 2026-10-02 same-type-2box
    practice run — where it printed "engine mgmt IP 10.0.0.250 is free" while a FOREIGN
    live `quotient-engine` was answering that exact address:

    * **The mgmt L2 spans nodes.** The scan did `vm["node"] != node: continue`, but
      10.0.0.0/24 is one flat segment: the foreign engine was on .193 and this deploy
      was on .150. The data was already cluster-wide (`/cluster/resources`) — the filter
      threw away the rows that mattered. The scan is now cluster-wide.
    * **Unverifiable is not free.** A guest whose agent is down was counted and skipped,
      and the gate then claimed the address was free from an absence of evidence. When
      any running guest cannot be checked the address is *unknown*: refusing is correct
      for the DEFAULT ip (it is what every comp gets, and nobody chose it), while an IP
      the operator set explicitly gets a loud warning instead, because they have taken
      responsibility for it.

    Guests without a working agent can't be checked; count them out loud instead of
    claiming the range is clean."""
    unchecked, taken, takers = [], set(), {}
    for vm in vms:
        if vm.get("status") != "running" or vm.get("template") == 1:
            continue
        if vm.get("vmid") == engine_vmid:
            # Our own engine from a prior failed attempt — it holds the planned
            # mgmt IP until phase 1 destroys it seconds from now (live-found
            # 2026-09-29: every retry after a phase-2+ failure re-tripped this
            # gate against our own leftover).
            continue
        # Broader than the vmid check: ANY VM tagged as this competition's (a
        # leftover box, not just the engine) answers the IP only until phase 1
        # recycles it — only a FOREIGN guest squatting the address is a collision.
        if ours_tags and set(ours_tags) <= parse_vm_tags(vm.get("tags")):
            continue
        label = f"{vm.get('vmid')} ({vm.get('name') or '?'} on {vm.get('node') or '?'})"
        addrs = guest_ipv4s(vm)
        if addrs is None:
            unchecked.append(label)
            continue
        for ip in addrs:
            taken.add(ip)
            takers.setdefault(ip, label)
    if engine_mgmt_ip in taken:
        raise SystemExit(
            f"  ERROR: static engine mgmt IP {engine_mgmt_ip} is already answered by a "
            f"running guest ({takers.get(engine_mgmt_ip, 'unknown')}). Pick another "
            f"TF_VAR_engine_mgmt_ip (or set it to '' for DHCP). The management network is "
            f"shared across nodes, so a guest on ANY node counts.")
    # "Explicitly set" means the OPERATOR set it — not that the code exported the
    # default into the same variable seconds ago (deploy_lib.inputs). A default IP the
    # preflight cannot verify is refused outright: the .150 default (.250) drops SSH
    # mid-build on the tailnet path (2026-10-03), and an unverifiable default was a live
    # foreign engine's address once before that (2026-10-02).
    explicit = (bool((os.environ.get("TF_VAR_engine_mgmt_ip") or "").strip())
                and not os.environ.get("TEZ_ENGINE_MGMT_IP_IS_DEFAULT"))
    if unchecked:
        listing = ", ".join(unchecked[:5]) + ("..." if len(unchecked) > 5 else "")
        if not explicit:
            raise SystemExit(
                f"  ERROR: cannot verify the engine mgmt IP {engine_mgmt_ip} — "
                f"{len(unchecked)} running guest(s) have no working agent to ask "
                f"({listing}). This is the DEFAULT address, and on this estate the "
                f"default has already been a live foreign engine's address once "
                f"(2026-10-02). Set TF_VAR_engine_mgmt_ip to an address you have checked "
                f"yourself (or '' for DHCP). On this estate the default (.250) also "
                f"drops SSH mid-build on the .150 tailnet path (2026-10-03) — the "
                f"cyberrange env variants pin 10.0.0.252 instead.")
        print(f"  Preflight: engine mgmt IP {engine_mgmt_ip} — UNVERIFIED "
              f"({len(unchecked)} running guest(s) have no agent: {listing}); proceeding "
              f"because TF_VAR_engine_mgmt_ip is set explicitly")
        return
    print(f"  Preflight: engine mgmt IP {engine_mgmt_ip} is free")


def jump_mgmt_ip_gate(plan):
    """Every satellite's jump IP against running guests on ALL hosting nodes (one mgmt L2)."""
    placement = plan.placement
    jump_ips = [s["jump_mgmt_ip"] for s in placement["satellites"]]
    if not jump_ips:
        return
    skip = {plan.engine_vmid} | {s["jump_vmid"] for s in placement["satellites"]}
    taken, unchecked = set(), 0
    for share in plan.shares:
        for vm in share.raw_vms:
            if vm.get("node") != share.node or vm.get("status") != "running" \
                    or vm.get("template") == 1 or vm.get("vmid") in skip:
                continue
            addrs = guest_ipv4s(vm)
            if addrs is None:
                unchecked += 1
                continue
            taken.update(addrs)
    dupes = sorted(set(jump_ips) & taken)
    if dupes:
        raise SystemExit(
            f"  ERROR: jump mgmt IP(s) {dupes} already answered by a running guest "
            "on the mgmt LAN — set explicit jump_mgmt_ip values in nodes.json.")
    note = (f" ({unchecked} guest(s) unverifiable — agent down)" if unchecked else "")
    print(f"  Preflight: jump mgmt IP(s) {jump_ips} free{note}")
