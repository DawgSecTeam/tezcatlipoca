"""Collision gates: what this deploy will create must not already exist as someone else's.

Two families, one module:

  - vmid/bridge slot collisions (check_collisions): everything this deploy will create must
    be free of another run's resources. ONE implementation for single- and multi-node (a
    single-node plan is one share). Ownership is answered by infra.ownership.ownership_verdict
    - the same proof the destroy guard uses - so preflight and teardown can never disagree
    about whose VM a vmid holds. A leftover RANGE is refused (a deploy never destroys one; see
    the function docstring); this competition's own templates are adopted, and an untagged
    interrupted clone of ours is left to phase 1.
  - management-IP collisions (engine_mgmt_ip_gate / jump_mgmt_ip_gate): the engine's static
    mgmt IP and each satellite's jump mgmt IP must not already be answered by a running guest
    on the (cross-node) management L2."""

import os
from dataclasses import dataclass

from common.constants import ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from infra.client import proxmox_api
from common.targets import vm_id_for
from infra.ownership import MARKER, ownership_verdict, parse_vm_tags


@dataclass
class Slot:
    vmid: int
    label: str
    persistent_template: bool = False   # the engine template: expected to survive, reused
    template_slot: bool = False         # a slot a TEMPLATE may legitimately occupy (golden +
                                        # engine template): a template here is this comp's,
                                        # kept for reuse — `--keep-templates`/`--teams-only`


def expected_slots(share, boxes, engine_vmid):
    """Every vmid this share's deploy will create, with its report label.

    Order and wording differ between single- and multi-node only in the report (the vmid math
    is identical: goldens at engine+GOLDEN_VMID_OFFSET+slot*MAX_BOXES_PER_TEAM, teams by
    vm_id_for)."""
    slots = []
    if share.is_engine:
        slots.append(Slot(engine_vmid, f"scoring engine vmid {engine_vmid}"))
        # M4: the engine template's reserved slot (just below the golden block). A TEMPLATE
        # there carrying this competition's tag is adopted — reused or rebuilt by hash, NOT
        # a leftover. Another competition's template there is fatal.
        et = engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET
        # template_slot as well as persistent_template: the --keep-templates teardown keeps
        # this template in MULTI-node mode too, and gating the tolerance on `persistent_template`
        # alone (False when share.multi) made the next deploy refuse its own kept engine
        # template as a leftover — the reuse path could never run (2026-10-14).
        slots.append(Slot(et, f"engine template vmid {et}", template_slot=True,
                          persistent_template=not share.multi))
    teams = [Slot(vm_id_for(t["identifier"], i), f"team vmid {vm_id_for(t['identifier'], i)} "
                  f"({k}/{boxes[i]['name']})")
             for k, t in share.teams.items() for i in range(len(boxes))]
    goldens = []
    for i in range(len(boxes)):
        vid = engine_vmid + GOLDEN_VMID_OFFSET + share.slot * MAX_BOXES_PER_TEAM + i
        goldens.append(Slot(vid, f"golden vmid {vid} ({boxes[i]['name']})" if not share.multi
                            else f"golden vmid (slot {share.slot}) #{vid}",
                            template_slot=True))
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
    """Raise SystemExit on anything a fresh deploy must not create over.

    A deploy never tears a range down. Any planned vmid holding a VM an earlier run of
    this competition owns — team boxes, the engine, a satellite jump VM — is a REFUSAL
    naming the remedy: destroy-competition.py reclaims it from the workspace that owns
    the run, or the operator resumes that run with --from-phase. Two things are not
    leftovers and do not refuse:

      * this competition's own TEMPLATES (engine template + goldens): cached build
        artifacts a --teams-only teardown keeps on purpose, which phase 2/4 adopt or
        rebuild by hash. A slot is a template slot; a hand-converted template in a
        team-box slot is still a leftover.
      * an untagged VM carrying this competition's clone marker: an interrupted clone
        that no run owns (infra.ownership.MARKER). Phase 1 reclaims it."""
    node, comp_name = share.node, plan.comp_name
    comp_tag = f"comp-{comp_name}"
    existing = {vm.get("vmid") for vm in vms}
    vm_by_vmid = {vm.get("vmid"): vm for vm in vms}

    clashes, leftovers, reclaim = [], [], 0
    for slot in expected_slots(share, plan.boxes, plan.engine_vmid):
        if slot.vmid not in existing:
            continue
        vm = vm_by_vmid[slot.vmid]
        tags = parse_vm_tags(vm.get("tags"))
        verdict, _ = ownership_verdict(node, slot.vmid, vm.get("tags"), plan.our_tags)
        if (slot.persistent_template or slot.template_slot) and vm.get("template") \
                and comp_tag in tags:
            print(f"  Preflight{share.label}: {slot.label} holds this competition's "
                  f"template — kept for reuse (rebuilt only on hash drift)")
            continue
        if verdict == MARKER:
            # The clone POST wrote the marker into the description; a crash or reboot
            # kept the tag PUT from ever running, so no run id owns this VM.
            print(f"  Preflight{share.label}: vmid {slot.vmid} is an interrupted clone of "
                  f"this competition (lock={vm.get('lock') or 'none'}) — phase 1 reclaims it")
            reclaim += 1
            continue
        if comp_tag in tags:
            leftovers.append(slot.label)
        else:
            clashes.append(slot.label)

    nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
    existing_bridges = {n.get("iface") for n in nets}
    for bridge in expected_bridges(share, plan.boxes):
        # Bridges carry no per-comp tags, so an existing one can only be assumed to
        # belong to a range someone still owns — never destroyed from here.
        if bridge in existing_bridges:
            leftovers.append(f"bridge {bridge}")

    if clashes or leftovers:
        run = plan.our_run_tag
        other_run = any(
            comp_tag in parse_vm_tags(vm.get("tags"))
            and run and run not in parse_vm_tags(vm.get("tags"))
            for vm in vm_by_vmid.values())
        if share.multi:
            advice = "Pick a free --scoring-vmid and/or non-overlapping team identifiers."
        else:
            advice = ("Pick a free --scoring-vmid and/or non-overlapping TF_VAR_team_identifiers. "
                      "Note the golden block sits at <scoring-vmid>+150 — a colliding golden "
                      "vmid also means picking a different engine vmid.")
        remedy = ""
        if leftovers:
            remedy = (f" A fresh deploy never destroys a leftover range: reclaim it with "
                      f"python3 destroy-competition.py --competition {comp_name} --yes from "
                      f"the workspace that owns the run")
            remedy += (" — the colliding VM(s) carry this competition's comp tag but NOT "
                       "this run's run-id tag: ANOTHER worktree's run of the same competition "
                       "ID (coordinate with its session; never destroy it from here)."
                       if other_run else ", or resume that run with --from-phase N.")
        raise SystemExit(
            f"  ERROR: this competition's infrastructure collides with VMs/bridges already "
            f"on {share.where}"
            + ("" if share.multi else " (another running competition?)")
            + ": " + ", ".join(clashes + leftovers) + ". " + advice + remedy)
    if reclaim:
        print(f"  Preflight{share.label}: {reclaim} interrupted clone(s) of this competition "
              f"— phase 1 reclaims them")
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


def running_guest_ips(vms, skip_vmids, ours_tags=None):
    """(takers, unchecked): the first running guest to answer each address, and the guests
    whose agent cannot say. Both mgmt-IP gates scan the mgmt L2 this way."""
    takers, unchecked = {}, []
    for vm in vms:
        if vm.get("status") != "running" or vm.get("template") == 1 or vm.get("vmid") in skip_vmids:
            continue
        # Broader than the vmid skip: ANY VM tagged as this deploy's answers the IP only
        # because a leftover range holds it — only a FOREIGN guest squatting the address
        # is a collision (a leftover of ours is refused by check_collisions).
        if ours_tags and set(ours_tags) <= parse_vm_tags(vm.get("tags")):
            continue
        label = f"{vm.get('vmid')} ({vm.get('name') or '?'} on {vm.get('node') or '?'})"
        addrs = guest_ipv4s(vm)
        if addrs is None:
            unchecked.append(label)
            continue
        for ip in addrs:
            takers.setdefault(ip, label)
    return takers, unchecked


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
    # Our own engine from a prior failed attempt holds the planned mgmt IP until that run
    # is destroyed or resumed (live-found 2026-09-29: every retry after a phase-2+ failure
    # re-tripped this gate against our own leftover). Kept by vmid, not only by tag: a state
    # file predating run ids gives the resume no run tag to match, and the collision gate
    # that would otherwise catch it is off on resume. A fresh deploy with that engine still
    # up is refused by check_collisions before this gate runs.
    takers, unchecked = running_guest_ips(vms, {engine_vmid}, ours_tags)
    if engine_mgmt_ip in takers:
        raise SystemExit(
            f"  ERROR: static engine mgmt IP {engine_mgmt_ip} is already answered by a "
            f"running guest ({takers[engine_mgmt_ip]}). Pick another "
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
    vms = [vm for share in plan.shares for vm in share.raw_vms if vm.get("node") == share.node]
    takers, unchecked = running_guest_ips(vms, skip, plan.our_tags)
    dupes = sorted(set(jump_ips) & set(takers))
    if dupes:
        raise SystemExit(
            f"  ERROR: jump mgmt IP(s) {dupes} already answered by a running guest "
            "on the mgmt LAN — set explicit jump_mgmt_ip values in nodes.json.")
    note = (f" ({len(unchecked)} guest(s) unverifiable — agent down)" if unchecked else "")
    print(f"  Preflight: jump mgmt IP(s) {jump_ips} free{note}")
