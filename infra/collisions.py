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

from infra.client import proxmox_api
from common.targets import vm_id_for
from infra.ownership import MARKER, ownership_verdict, parse_vm_tags
from estate import config


@dataclass
class Slot:
    vmid: int
    label: str
    persistent_template: bool = False   # the engine template: expected to survive, reused
    template_slot: bool = False         # a slot a TEMPLATE may legitimately occupy (golden +
                                        # engine template): a template here is this comp's,
                                        # kept for reuse — `--keep-templates`/`--teams-only`


def expected_slots(share, plan):
    """Every vmid this share's deploy will create, with its report label.

    Reads the placement object's own `engine_template_vmid`, `golden_vmids` and satellite
    `jump_vmid`; a single-node plan (no placement) derives them from the one module that owns
    the arithmetic. Order and wording differ between single- and multi-node only in the
    report."""
    from multinode import placement_record as placement_ops
    boxes, engine_vmid, placement = plan.boxes, plan.engine_vmid, plan.placement
    slots = []
    if share.is_engine:
        slots.append(Slot(engine_vmid, f"scoring engine vmid {engine_vmid}"))
        # M4: the engine template's reserved slot (just below the golden block). A TEMPLATE
        # there carrying this competition's tag is adopted — reused or rebuilt by hash, NOT
        # a leftover. Another competition's template there is fatal.
        et = ((placement or {}).get("engine_template_vmid")
              or placement_ops.engine_template_vmid(engine_vmid))
        # template_slot as well as persistent_template: the --keep-templates teardown keeps
        # this template in MULTI-node mode too, and gating the tolerance on `persistent_template`
        # alone (False when share.multi) made the next deploy refuse its own kept engine
        # template as a leftover — the reuse path could never run .
        slots.append(Slot(et, f"engine template vmid {et}", template_slot=True,
                          persistent_template=not share.multi))
    teams = [Slot(vm_id_for(t["identifier"], i), f"team vmid {vm_id_for(t['identifier'], i)} "
                  f"({k}/{boxes[i]['name']})")
             for k, t in share.teams.items() for i in range(len(boxes))]
    recorded_goldens = ((placement or {}).get("golden_vmids") or {}).get(share.name)
    goldens = []
    for i in range(len(boxes)):
        vid = (recorded_goldens[i] if recorded_goldens
               else placement_ops.golden_vmid_for_slot(engine_vmid, share.slot, i))
        goldens.append(Slot(vid, f"golden vmid {vid} ({boxes[i]['name']})" if not share.multi
                            else f"golden vmid (slot {share.slot}) #{vid}",
                            template_slot=True))
    if share.multi:
        slots += goldens
        if share.slot > 0:
            jump = next((s["jump_vmid"] for s in (placement or {}).get("satellites") or []
                         if s["slot"] == share.slot),
                        placement_ops.jump_vmid_for(engine_vmid, share.slot))
            slots.append(Slot(jump, f"jump vmid {jump}"))
        slots += teams
    else:
        slots += teams + goldens
    return slots


def check_placement(placement):
    """The one proof over the whole placement object: no vmid appears in two of this run's
    own bands, and no team block overlaps them.

    The gates below prove the CLUSTER is free of other people's resources; this proves the
    OBJECT is free of itself, so four gates no longer each carry a partial version of the
    arithmetic."""
    if not placement or not placement.get("engine_vmid"):
        return
    from multinode import placement_record as placement_ops
    engine_vmid = int(placement["engine_vmid"])
    bands = {
        "engine": {engine_vmid},
        "engine template": {int(placement.get("engine_template_vmid")
                                or placement_ops.engine_template_vmid(engine_vmid))},
    }
    if placement.get("red01"):
        bands["red01"] = {int(placement["red01"]["vmid"])}
    for name, vmids in (placement.get("golden_vmids") or {}).items():
        bands[f"goldens on {name}"] = {int(v) for v in vmids}
    for sat in placement.get("satellites") or []:
        bands[f"jump on {sat['name']}"] = {int(sat["jump_vmid"])}
    seen = {}
    for name, band in bands.items():
        for vmid in band:
            if vmid in seen:
                raise SystemExit(
                    f"  ERROR: the placement record puts vmid {vmid} in two of this run's own "
                    f"bands ({seen[vmid]} and {name}) — the record is inconsistent; destroy "
                    f"and redeploy to re-place.")
            seen[vmid] = name
    placement_ops.check_team_blocks(
        placement, {k: {"identifier": i}
                    for k, i in (placement.get("team_identifiers") or {}).items()})


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
    check_placement(plan.placement)
    for slot in expected_slots(share, plan):
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
        if slot.template_slot and not vm.get("template") and not any(
                t.startswith("comp-") and t != comp_tag for t in tags):
            # This run's own build slot holding an unconverted VM: a template build whose
            # clone task failed before the tag PUT, so it wears the base image's tags and
            # carries neither ours nor a marker. Phase 2 adopts and rebuilds exactly this
            # (`build_engine_template`'s dead-attempt branch, by reserved vmid + name).
            # Without the exemption the deploy cannot clean up its own wreckage: the clone
            # that failed is both the debris and the reason no later attempt can start
            # (caught live refused on their own
            # partial engine-template clones at vmids 2340/2540). Anything tagged into
            # another run stays a refusal: only UNPROVEN ownership is adopted.
            print(f"  Preflight{share.label}: {slot.label} holds an unconverted leftover of "
                  f"this competition's own build (vmid {slot.vmid}) — phase 2 reclaims it")
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
            advice = ("Pick a free --scoring-vmid and/or non-overlapping TEZ_TEAM_IDENTIFIERS. "
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
        from multinode import placement_record as placement_ops
        print(f"  Preflight: engine vmid {plan.engine_vmid}, engine template vmid "
              f"{placement_ops.engine_template_vmid(plan.engine_vmid)}, golden vmids "
              f"{placement_ops.golden_vmid_for_slot(plan.engine_vmid, 0, 0)}+, all team vmids, "
              f"and team bridges are free")


def share_mgmt_bridge(plan, share):
# The bridge this share's management NIC attaches to: the node record's own when it names
#     one (a node whose L2 is vmbr100), else the placement's resolved value, else vmbr0.
    from multinode.placement_record import record_of, resolved_mgmt_bridge
    if plan.placement and share.name:
        rec = record_of(plan.placement, share.name)
        return rec.mgmt_bridge or resolved_mgmt_bridge(plan.placement)
    return resolved_mgmt_bridge(plan.placement)


def check_mgmt_bridge(node, mgmt_bridge):
    """Refuse a management bridge the node does not own.

    Attaching every NIC to a bridge that is not there fails at clone time with a PVE error
    the operator cannot act on; the node's own network list is the proof."""
    nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
    if mgmt_bridge not in {n.get("iface") for n in nets}:
        raise SystemExit(
            f"  ERROR: the management bridge {mgmt_bridge} does not exist on node '{node}' — "
            f"NICs would attach to a bridge that is not there. Set proxmox.mgmt_bridge (or "
            f"this node's mgmt_bridge in nodes.json) to the bridge carrying the estate's "
            f"management L2.")
    print(f"  Preflight: management bridge {mgmt_bridge} exists on node '{node}'")


def scan_vms_for(nodes):
    """Union of every hosting node's cluster view.

    The management L2 is one segment, but on independent hosts /cluster/resources sees only
    its own VMs — so an address scan must ask every node that could answer it."""
    from infra.client import cluster_vms_for
    seen, out = set(), []
    for node in nodes:
        for vm in cluster_vms_for(node):
            key = (vm.get("vmid"), vm.get("node"))
            if key not in seen:
                seen.add(key)
                out.append(vm)
    return out


# The band red01's address is selected from (a tool constant, not a knob). The legacy
# .196-.199 defaults are excluded so a selected value can never be one of them.
RED01_IP_BAND = ("10.0.0.240", "10.0.0.249")
RED01_LEGACY_IPS = ("10.0.0.196", "10.0.0.197", "10.0.0.198", "10.0.0.199")


def select_red_ip(vms, claimed=(), skip_vmids=(), ours_tags=None):
    """The lowest red-band address nothing answers and no claimant holds, or (None, unchecked).

    `claimed` is everything this placement already takes on the L2 — the engine's address, its
    gateway, every jump address — because a scan taken before those VMs exist cannot see them,
    and handing red01 the engine's own address is exactly the collision to exclude."""
    takers, unchecked = running_guest_ips(vms, set(skip_vmids), ours_tags)
    taken = set(takers) | set(claimed) | set(RED01_LEGACY_IPS)
    prefix, lo = RED01_IP_BAND[0].rsplit(".", 1)
    hi = int(RED01_IP_BAND[1].rsplit(".", 1)[1])
    for octet in range(int(lo), hi + 1):
        ip = f"{prefix}.{octet}"
        if ip not in taken:
            return ip, unchecked
    return None, unchecked


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

    Two things this gate got wrong, both caught live on a same-type 2-box comp
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
    # is destroyed or resumed (caught live: every retry after a phase-2+ failure
    # re-tripped this gate against our own leftover). Kept by vmid, not only by tag: a state
    # file predating run ids gives the resume no run tag to match, and the collision gate
    # that would otherwise catch it is off on resume. A fresh deploy with that engine still
    # up is refused by check_collisions before this gate runs.
    takers, unchecked = running_guest_ips(vms, {engine_vmid}, ours_tags)
    if engine_mgmt_ip in takers:
        raise SystemExit(
            f"  ERROR: static engine mgmt IP {engine_mgmt_ip} is already answered by a "
            f"running guest ({takers[engine_mgmt_ip]}). Pick another "
            f"TEZ_ENGINE_MGMT_IP (or set it to '' for DHCP). The management network is "
            f"shared across nodes, so a guest on ANY node counts.")
    # "Explicitly set" means the OPERATOR set it — not that the code exported the
    # default into the same variable seconds ago (deploy_lib.inputs). A default IP the
    # preflight cannot verify is refused outright: the .150 default (.250) drops SSH
    # mid-build on the tailnet path , and an unverifiable default was a live
    # foreign engine's address once before that .
    explicit = (bool((config.active().engine.mgmt_ip or "").strip())
                and not os.environ.get("TEZ_ENGINE_MGMT_IP_IS_DEFAULT"))
    if unchecked:
        listing = ", ".join(unchecked[:5]) + ("..." if len(unchecked) > 5 else "")
        if not explicit:
            raise SystemExit(
                f"  ERROR: cannot verify the engine mgmt IP {engine_mgmt_ip} — "
                f"{len(unchecked)} running guest(s) have no working agent to ask "
                f"({listing}). This is the DEFAULT address, and an unverifiable default "
                f"has been a live foreign engine's address before, on a shared estate. "
                f"Set TEZ_ENGINE_MGMT_IP to an address you have checked "
                f"yourself (or '' for DHCP).")
        print(f"  Preflight: engine mgmt IP {engine_mgmt_ip} — UNVERIFIED "
              f"({len(unchecked)} running guest(s) have no agent: {listing}); proceeding "
              f"because TEZ_ENGINE_MGMT_IP is set explicitly")
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
