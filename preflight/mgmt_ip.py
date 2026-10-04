"""Gates: the engine's static mgmt IP and each satellite's jump mgmt IP must not already be
answered by a running guest on the (cross-node) management L2."""

import os

from pve_api import proxmox_api
from vm_ownership import parse_vm_tags


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
    explicit = bool((os.environ.get("TF_VAR_engine_mgmt_ip") or "").strip())
    if unchecked:
        listing = ", ".join(unchecked[:5]) + ("..." if len(unchecked) > 5 else "")
        if not explicit:
            raise SystemExit(
                f"  ERROR: cannot verify the engine mgmt IP {engine_mgmt_ip} — "
                f"{len(unchecked)} running guest(s) have no working agent to ask "
                f"({listing}). This is the DEFAULT address, and on this estate the "
                f"default has already been a live foreign engine's address once "
                f"(2026-10-02). Set TF_VAR_engine_mgmt_ip to an address you have checked "
                f"yourself (or '' for DHCP).")
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
