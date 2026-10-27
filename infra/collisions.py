"""Collision gates: what this deploy will create must not already exist as someone else's."""

import os
from dataclasses import dataclass

from infra.client import proxmox_api
from common.constants import DEFAULT_ENGINE_MGMT_IP, ENGINE_MGMT_BAND, comp_tag
from common.targets import vm_id_for
from infra.ownership import MARKER, ownership_verdict, parse_vm_tags
from estate import config


@dataclass
class Slot:
    vmid: int
    label: str
    persistent_template: bool = False   # the engine template: expected to survive, reused
    template_slot: bool = False         # a slot a template may legitimately occupy (golden/engine)


def expected_slots(share, plan):
    """Every vmid this share's deploy will create, with its report label."""
    from multinode import placement_record as placement_ops
    boxes, engine_vmid, placement = plan.boxes, plan.engine_vmid, plan.placement
    slots = []
    if share.is_engine:
        slots.append(Slot(engine_vmid, f"scoring engine vmid {engine_vmid}"))
        # The engine template's reserved slot (just below the golden block): this comp's template
        # there is adopted by hash, another competition's is fatal. template_slot too —
        # --keep-templates keeps this in multi-node mode, where persistent_template is False.
        et = ((placement or {}).get("engine_template_vmid")
              or placement_ops.engine_template_vmid(engine_vmid))
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
    """No vmid in two of this run's own bands, and no team block overlapping them."""
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
    """Team bridges, and for in-path firewalls the per-team transit bridge, this share creates."""
    bridges = [f"vmbr{t['identifier']}" for t in share.teams.values()]
    if any(b.get("in_path") for b in boxes) and share.slot == 0:
        bridges += [f"vmbrW{t['identifier']}" for t in share.teams.values()]
    return bridges


def check_collisions(share, plan, vms):
    """Raise SystemExit on anything a fresh deploy must not create over.

    A planned vmid holding a VM an earlier run of this competition owns is a REFUSAL naming the
    remedy; this competition's own templates and an untagged VM carrying its clone marker are not
    leftovers."""
    node, comp_name = share.node, plan.comp_name
    own_tag = comp_tag(comp_name)
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
                and own_tag in tags:
            print(f"  Preflight{share.label}: {slot.label} holds this competition's "
                  f"template — kept for reuse (rebuilt only on hash drift)")
            continue
        if verdict == MARKER:
            print(f"  Preflight{share.label}: vmid {slot.vmid} is an interrupted clone of "
                  f"this competition (lock={vm.get('lock') or 'none'}) — phase 1 reclaims it")
            reclaim += 1
            continue
        if slot.template_slot and not vm.get("template") and not any(
                t.startswith("comp-") and t != own_tag for t in tags):
            # Own build slot holding an unconverted VM (clone failed before the tag PUT, so it
            # wears the base image's tags); phase 2 reclaims it.
            print(f"  Preflight{share.label}: {slot.label} holds an unconverted leftover of "
                  f"this competition's own build (vmid {slot.vmid}) — phase 2 reclaims it")
            continue
        if own_tag in tags:
            leftovers.append(slot.label)
        else:
            clashes.append(slot.label)

    nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
    existing_bridges = {n.get("iface") for n in nets}
    for bridge in expected_bridges(share, plan.boxes):
        # Bridges carry no per-comp tags, so an existing one belongs to a range someone still
        # owns — never destroyed from here.
        if bridge in existing_bridges:
            leftovers.append(f"bridge {bridge}")

    if clashes or leftovers:
        run = plan.our_run_tag
        other_run = any(
            own_tag in parse_vm_tags(vm.get("tags"))
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
    # The node record's own bridge when it names one, else the placement's resolved value.
    from multinode.placement_record import record_of, resolved_mgmt_bridge
    if plan.placement and share.name:
        rec = record_of(plan.placement, share.name)
        return rec.mgmt_bridge or resolved_mgmt_bridge(plan.placement)
    return resolved_mgmt_bridge(plan.placement)


def check_mgmt_bridge(node, mgmt_bridge):
    """Attaching NICs to a bridge that is not there fails at clone time with a PVE error the
    operator cannot act on, so refuse it here."""
    nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
    if mgmt_bridge not in {n.get("iface") for n in nets}:
        raise SystemExit(
            f"  ERROR: the management bridge {mgmt_bridge} does not exist on node '{node}' — "
            f"NICs would attach to a bridge that is not there. Set proxmox.mgmt_bridge (or "
            f"this node's mgmt_bridge in nodes.json) to the bridge carrying the estate's "
            f"management L2.")
    print(f"  Preflight: management bridge {mgmt_bridge} exists on node '{node}'")


def scan_vms_for(nodes):
    """Union of every hosting node's cluster view — on independent hosts a host sees only its
    own VMs, so an address scan must ask every node that could answer."""
    from infra.client import cluster_vms_for
    seen, out = set(), []
    for node in nodes:
        for vm in cluster_vms_for(node):
            key = (vm.get("vmid"), vm.get("node"))
            if key not in seen:
                seen.add(key)
                out.append(vm)
    return out


# red01's band (a tool constant); the legacy .196-.199 defaults are excluded so a selected
# value can never be one of them.
RED01_IP_BAND = ("10.0.0.240", "10.0.0.249")
RED01_LEGACY_IPS = ("10.0.0.196", "10.0.0.197", "10.0.0.198", "10.0.0.199")


def select_red_ip(vms, claimed=(), skip_vmids=(), ours_tags=None):
    """The lowest red-band address nothing answers and no claimant holds, or (None, unchecked).
    `claimed` is everything this placement already takes on the L2 (engine, gateway, jumps).

    An unverifiable guest makes every candidate a guess, so an incomplete scan selects nothing
    (the caller refuses or asks the operator) — the same contract as `select_engine_ip`."""
    takers, unchecked = running_guest_ips(vms, set(skip_vmids), ours_tags)
    if unchecked:
        return None, unchecked
    taken = set(takers) | set(claimed) | set(RED01_LEGACY_IPS)
    prefix, lo = RED01_IP_BAND[0].rsplit(".", 1)
    hi = int(RED01_IP_BAND[1].rsplit(".", 1)[1])
    for octet in range(int(lo), hi + 1):
        ip = f"{prefix}.{octet}"
        if ip not in taken:
            return ip, unchecked
    return None, unchecked


def engine_mgmt_band():
# The declared band, or the profile's engine.mgmt_ip_band override ("START-END").
    raw = (config.active().engine.mgmt_ip_band or "").strip()
    if not raw:
        return ENGINE_MGMT_BAND
    lo, _, hi = raw.partition("-")
    if not (lo.strip() and hi.strip()):
        raise SystemExit(f"  ERROR: engine.mgmt_ip_band {raw!r} is not a START-END range "
                         f"(e.g. {ENGINE_MGMT_BAND[0]}-{ENGINE_MGMT_BAND[1]}).")
    return lo.strip(), hi.strip()


def _red_band_ips():
    prefix, lo = RED01_IP_BAND[0].rsplit(".", 1)
    hi = int(RED01_IP_BAND[1].rsplit(".", 1)[1])
    return {f"{prefix}.{octet}" for octet in range(int(lo), hi + 1)}


def select_engine_ip(vms, claimed=(), skip_vmids=(), ours_tags=None):
    """The lowest free engine-band address, or (None, unchecked) when the scan is incomplete.

    (None, []) means the band is exhausted; the caller keeps the default and the preflight
    decides. An unverifiable guest makes every candidate a guess, so the whole selection is
    skipped (see ENGINE_MGMT_BAND's decision comment)."""
    takers, unchecked = running_guest_ips(vms, set(skip_vmids), ours_tags)
    if unchecked:
        return None, unchecked
    # The default, red01's band and its legacy defaults are never engine candidates, even
    # when a profile band override spans them.
    taken = (set(takers) | set(claimed) | {DEFAULT_ENGINE_MGMT_IP}
             | set(RED01_LEGACY_IPS) | _red_band_ips())
    lo, hi = engine_mgmt_band()
    prefix, start = lo.rsplit(".", 1)
    for octet in range(int(start), int(hi.rsplit(".", 1)[1]) + 1):
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
    """(takers, unchecked): the first running guest to answer each address, and those it cannot tell."""
    takers, unchecked = {}, []
    for vm in vms:
        if vm.get("status") != "running" or vm.get("template") == 1 or vm.get("vmid") in skip_vmids:
            continue
        # Broader than the vmid skip: a VM tagged as this deploy's answers the IP only because a
        # leftover range holds it — only a FOREIGN guest squatting the address is a collision.
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
    """Static engine mgmt IP must not collide with any running guest on the mgmt L2 (cluster-wide,
    since the L2 spans nodes). An unverifiable address is not free: the DEFAULT ip is refused
    (nobody chose it), while an explicitly set one gets a loud warning."""
    # Our own engine from a prior failed attempt holds the planned mgmt IP until that run is
    # destroyed or resumed — skipped by vmid too: a run-id-less state file gives no tag.
    takers, unchecked = running_guest_ips(vms, {engine_vmid}, ours_tags)
    if engine_mgmt_ip in takers:
        raise SystemExit(
            f"  ERROR: static engine mgmt IP {engine_mgmt_ip} is already answered by a "
            f"running guest ({takers[engine_mgmt_ip]}). Pick another "
            f"TEZ_ENGINE_MGMT_IP (or set it to '' for DHCP). The management network is "
            f"shared across nodes, so a guest on ANY node counts.")
    # "Explicitly set" means the OPERATOR set it — not that deploy_lib.inputs exported the
    # default into the same variable seconds ago.
    explicit = (bool((config.active().engine.mgmt_ip or "").strip())
                and not os.environ.get("TEZ_ENGINE_MGMT_IP_IS_DEFAULT")
                and not os.environ.get("TEZ_ENGINE_MGMT_IP_IS_AUTO_SELECTED"))
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
    from multinode.placement_record import record_of
    skip = {plan.engine_vmid} | {s["jump_vmid"] for s in placement["satellites"]}
    vms = [vm for share in plan.shares for vm in share.raw_vms if vm.get("node") == share.node]
    takers, unchecked = running_guest_ips(vms, skip, plan.our_tags)
    dupes = sorted(set(jump_ips) & set(takers))
    if dupes:
        raise SystemExit(
            f"  ERROR: jump mgmt IP(s) {dupes} already answered by a running guest "
            "on the mgmt LAN — set explicit jump_mgmt_ip values in nodes.json.")
    if unchecked and not all(
            record_of(placement, s["name"]).jump_mgmt_ip for s in placement["satellites"]):
        listing = ", ".join(sorted(f"{u['node']}/{u['vmid']}" for u in unchecked))
        raise SystemExit(
            f"  ERROR: jump mgmt IP(s) {jump_ips} could not be verified — {len(unchecked)} "
            f"running guest(s) have no guest agent ({listing}). Set explicit jump_mgmt_ip "
            f"values in nodes.json, or restore the agent channel.")
    note = (f" ({len(unchecked)} guest(s) unverifiable — agent down)" if unchecked else "")
    print(f"  Preflight: jump mgmt IP(s) {jump_ips} free{note}")
