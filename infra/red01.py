"""Gate: red01's reserved vmid/IP must not collide with anything live.

red01 is bad-auto's VM — not terraform-managed, carrying no tezcatlipoca
ownership tags — so it cannot ride the clash gate's slot math. It gets
mgmt-IP-style gates instead (agent scans + a cluster-view vmid check), because
the 2026-10-12 vmid-999 kill was exactly this gap: a validation deploy
defaulting onto the shared red01 slot sailed through preflight and recycled
another session's in-flight red01 (bad-auto's deploy is clone-or-reuse, so a
live VM at the vmid is "reconfigured", re-stamped, and its owner's ground
truth clobbered).

Who owns a live red01 vmid: the bad-auto description marker
(`bad-auto red01 comp=<name> id=<uuid>`), read on the error path so a refusal
NAMES the owner instead of asking the operator to "coordinate with its
session"."""

import re

from infra.client import proxmox_api
from infra.collisions import guest_ipv4s
from utils import compfile_flag, compfile_value

_MARKER_RE = re.compile(r"bad-auto red01 comp=(\S+) id=([0-9a-f]{8,64})")


def _marker_of(vm):
    """(exists-marker, comp-name-or-None) for a live VM, via its description."""
    try:
        desc = (proxmox_api("GET", f"/nodes/{vm['node']}/qemu/{vm['vmid']}/config")
                .get("data", {}).get("description") or "")
    except Exception:                                       # noqa: BLE001 - reported
        return None
    m = _MARKER_RE.search(desc)
    return m.group(1) if m else None


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
    """A live VM at the planned red01 vmid is a REFUSAL — unless this is a
    resume and the occupant's bad-auto marker names THIS competition (our own
    red01 from the attempt being resumed)."""
    occupant = next((vm for vm in _all_vms(plan)
                     if vm.get("vmid") == plan.red01_vmid), None)
    if occupant is None:
        return True
    label = (f"vmid {plan.red01_vmid} ({occupant.get('name') or '?'} on "
             f"{occupant.get('node') or '?'})")
    marker = _marker_of(occupant)
    if plan.resuming and marker == plan.comp_name:
        print(f"  Preflight: red01 {label} is this competition's own "
              f"(resume — bad-auto marker comp={marker})")
        return True
    owner = (f"bad-auto marker comp={marker}" if marker
             else "NO bad-auto marker (not a tezcatlipoca-managed VM — check by hand)")
    raise SystemExit(
        f"  ERROR: red01 vmid {plan.red01_vmid} is already occupied by {label}, "
        f"{owner}. This is the 2026-10-12 kill shape: bad-auto's deploy is "
        f"clone-or-reuse, so deploying onto an occupied red01 vmid reconfigures "
        f"and re-stamps whoever owns it. Set red01_vmid in the Compfile (or pass "
        f"--red-vmid on the scrim) to a vmid outside every other range's block — "
        f"the default derivation <scoring-vmid>+170 is collision-free whenever "
        f"the scoring vmid is.")


def red01_ip_gate(plan):
    """The planned red01 IP must not be answered by a running guest that is not
    this competition's red01 (cluster-wide: the mgmt L2 spans nodes).

    Same unverifiable-is-not-free rule as the engine mgmt-IP gate: guests whose
    agent cannot be asked refuse the DEFAULT red01 IP (nobody chose it) and
    only warn on one the operator set in the Compfile."""
    explicit = bool(compfile_value(plan.comp_dir / "Compfile", "red01_ip", "")
                    or compfile_value(plan.comp_dir / "Compfile",
                                      "assume_breach_red_ip", ""))
    ours_resuming = plan.resuming
    unchecked, taker = [], None
    for vm in _all_vms(plan):
        if vm.get("status") != "running" or vm.get("template") == 1:
            continue
        if vm.get("vmid") == plan.red01_vmid and ours_resuming:
            # our own red01 from the attempt being resumed (the vmid gate
            # verified the marker); it legitimately holds the IP
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
                f"running guest(s) have no working agent to ask ({listing}). This "
                f"is the DEFAULT address; set red01_ip in the Compfile to one you "
                f"have checked yourself.")
        print(f"  Preflight: red01 IP {plan.red01_ip} — UNVERIFIED ({listing}); "
              f"proceeding because the Compfile sets it explicitly")
        return
    print(f"  Preflight: red01 vmid {plan.red01_vmid}, IP {plan.red01_ip} are free")


def red01_gate(plan):
    """Both red01 gates — active only when this comp deploys one (assume_breach)."""
    if not plan.red01_vmid or not plan.red01_ip:
        return
    if not compfile_flag(plan.comp_dir / "Compfile", "assume_breach", 0):
        return
    red01_vmid_gate(plan)
    red01_ip_gate(plan)
