"""red01's reserved-vmid/IP preflight gates and occupant-identity verdicts.

red01 is a tezcatlipoca-owned VM: its ownership proof is the run's tag set, the
same one every other VM this range creates carries.
"""

from infra.client import proxmox_api
from infra.collisions import guest_ipv4s
from infra.ownership import parse_vm_tags


def _vm_tags(vm):
    try:
        raw = (proxmox_api("GET", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/config")
               .get("data", {}).get("tags") or "")
    except Exception:                                       # noqa: BLE001 - unprovable
        return None
    return parse_vm_tags(raw)


def _all_vms(plan):
    seen, out = set(), []
    for share in plan.shares:
        for vm in share.raw_vms:
            key = (vm.get("vmid"), vm.get("node"))
            if key not in seen:
                seen.add(key)
                out.append(vm)
    return out


def red01_vmid_gate(plan):
    """A live VM at the planned red01 vmid is a refusal unless this run owns it."""
    occupant = next((vm for vm in _all_vms(plan)
                     if vm.get("vmid") == plan.red01_vmid), None)
    if occupant is None:
        return True
    label = (f"vmid {plan.red01_vmid} ({occupant.get('name') or '?'} on "
             f"{occupant.get('node') or '?'})")
    tags = _vm_tags(occupant)
    if tags is None:
        raise SystemExit(f"  ERROR: red01 {label} could not be classified (its tags "
                         f"are unreadable) — refusing to deploy onto a slot this run "
                         f"cannot prove it owns")
    if plan.resuming and set(plan.our_tags) <= set(tags):
        print(f"  Preflight: red01 {label} is this run's (resume)")
        return True
    owner = ("another run of this competition" if f"comp-{plan.comp_name}" in tags
             else "a foreign VM")
    raise SystemExit(
        f"  ERROR: red01 vmid {plan.red01_vmid} is already occupied by {label} — it "
        f"belongs to {owner} (tags: {';'.join(sorted(tags)) or 'none'}). Set red01_vmid "
        f"in the Compfile to a vmid outside every other range's block; the derived "
        f"red01 vmid sits one golden stride past THIS placement's last golden band.")


def red01_ip_gate(plan):
    """The planned red01 IP must not be answered by a guest this run does not own.

    An unverifiable address is refused unless the operator chose it explicitly.
    """
    explicit = plan.red01_ip_explicit
    unchecked, taker = [], None
    for vm in _all_vms(plan):
        if vm.get("status") != "running" or vm.get("template") == 1:
            continue
        if vm.get("vmid") == plan.red01_vmid and plan.resuming:
            continue
        label = f"{vm.get('vmid')} ({vm.get('name') or '?'} on {vm.get('node') or '?'})"
        addrs = guest_ipv4s(vm)
        if addrs is None:
            unchecked.append(label)
            continue
        if plan.red01_ip in addrs:
            taker = taker or label
    if taker:
        raise SystemExit(
            f"  ERROR: red01 IP {plan.red01_ip} is already answered by a running "
            f"guest ({taker}) — the management network is shared across nodes. "
            f"Set red01_ip in the Compfile to an address you have checked.")
    if unchecked:
        listing = ", ".join(unchecked[:5]) + ("..." if len(unchecked) > 5 else "")
        if not explicit:
            raise SystemExit(
                f"  ERROR: cannot verify red01 IP {plan.red01_ip} — {len(unchecked)} "
                f"running guest(s) have no working agent to ask ({listing}). Set "
                f"red01_ip in the Compfile to one you have checked yourself.")
        print(f"  Preflight: red01 IP {plan.red01_ip} — UNVERIFIED ({listing}); "
              f"proceeding because the Compfile sets it explicitly")
        return
    print(f"  Preflight: red01 vmid {plan.red01_vmid}, IP {plan.red01_ip} are free")


def red01_gate(plan):
    """Both red01 gates, active when this comp has resolved a slot."""
    if not plan.red01_vmid or not plan.red01_ip:
        return
    red01_vmid_gate(plan)
    red01_ip_gate(plan)


def vm_at(vmid):
    try:
        vms = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception:                                       # noqa: BLE001 - reported by caller
        return None
    return next((v for v in vms if int(v.get("vmid") or -1) == int(vmid)), None)


def occupant_verdict(vmid, comp_name=None):
    """Who holds `vmid`: absent/ours/foreign/unmarked/unknown; 'unmarked' and 'unknown' never read as clean."""
    try:
        vms = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception:                                       # noqa: BLE001 - reported as unknown
        return "unknown"
    vm = next((v for v in vms if int(v.get("vmid") or -1) == int(vmid)), None)
    if vm is None:
        return "absent"
    tags = _vm_tags(vm)
    if not tags:
        return "unmarked"
    if comp_name and f"comp-{comp_name}" not in tags:
        return "foreign"
    return "ours"


def occupant_label(vmid):
    vm = vm_at(vmid)
    tags = _vm_tags(vm) if vm else None
    return (f"tags {';'.join(sorted(tags))}" if tags
            else "no ownership tags (a recycled vmid or a foreign VM — check by hand)")
