"""Reclaim an orphaned run: the VMs a discarded comp dir left behind.

Every other destroy path reads its `Ownership` from a comp dir's state, so a workspace that is
already gone strands its VMs — preflight refuses them as a leftover range and teardown refuses
for want of a run id. This closes that hole with the SAME proof, never a weaker one: the caller
names the comp tag and the run tag, and a VM is touched only when its own tag set carries both
plus `tezcatlipoca`. Two deliberate narrowings: the clone-marker adoption the other paths allow
is refused (`require_owned=True` — a marker names the competition, not the run, so it can be a
live worktree's interrupted clone), and templates (golden / engine-template) are irreversible
and need `allow_templates` because there is no freeze record left to consult. Nothing else about
the range is addressed: no bridges (a bridge carries no tags, so it can never be attributed), no
state document, no terminal plan."""

from infra.client import proxmox_api
from infra.ownership import Ownership, Refusal, destroy_vm_if_exists, parse_vm_tags

TEMPLATE_ROLES = ("tezcatlipoca-golden", "engine-template")


def refuse_when_state_exists(comp, run_tag):
# The orphan mode is for a run whose comp dir is gone. If some comp dir still owns this run id,
# that dir is the sanctioned path — and the only one that can consult the freeze record.
    from common.comp_paths import comps_root
    from estate import state as estate_state
    root = comps_root()
    if not root.is_dir():
        return
    for candidate in sorted(root.iterdir()):
        if not candidate.is_dir():
            continue
        if (estate_state.read_flat(candidate) or {}).get("run_id") == run_tag:
            raise Refusal(
                saw=f"{candidate} holds state for {run_tag}",
                expected="a run whose comp dir no longer exists",
                remedy=f"teardown the range from its own state: "
                       f"destroy-competition.py --comp-dir {candidate} --yes")


def reclaim(comp, run_tag, *, node, allow_templates=False, log=print):
# Destroy the VMs carrying {tezcatlipoca, comp-<comp>, <run_tag>}. Returns destroyed /
#     kept-templates / notes / remaining; `remaining` non-empty means the reclaim did not finish.
    ownership = Ownership(comp, run_tag)
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    destroyed, kept, notes = [], [], []
    for vm in sorted(vms, key=lambda v: v["vmid"]):
        tags = parse_vm_tags(vm.get("tags"))
        if f"comp-{comp}" in tags and not ownership.tags <= tags:
            others = sorted(t for t in tags if t.startswith("run-"))
            notes.append(f"vmid {vm['vmid']} ({vm.get('name')}) is comp-{comp} but carries "
                         f"{', '.join(others) or 'no run tag'}, not {run_tag} — not touched")
            continue
        if not ownership.tags <= tags:
            continue
        role = tags & set(TEMPLATE_ROLES)
        if role and not allow_templates:
            kept.append({"vmid": vm["vmid"], "name": vm.get("name"), "role": sorted(role)})
            continue
        log(f"  vmid {vm['vmid']} ({vm.get('name')}) — "
            f"{'+'.join(sorted(role)) or 'plain vm'}")
        destroy_vm_if_exists(node, vm["vmid"],
                             expect_tags=ownership.with_role(*role).tags, require_owned=True)
        destroyed.append({"vmid": vm["vmid"], "name": vm.get("name"),
                          "role": "+".join(sorted(role)) or "vm"})
    remaining = [v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                 if v["vmid"] in {d["vmid"] for d in destroyed}]
    return {"destroyed": destroyed, "kept_templates": kept, "notes": notes,
            "remaining": remaining, "scanned": len(vms)}
