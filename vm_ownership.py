"""Ownership-guarded Proxmox VM mutations: the ONE place the ownership-tag proof lives.

Everything that destroys, re-tags, unlocks or garbage-collects a VM on the strength of "this is
ours" goes through here, and both the preflight clash gate (preflight/clashes.py) and the destroy
guard (destroy_vm_if_exists) classify a VM with the same pure function, `ownership_verdict`:

  * OWNED     - carries the FULL ownership set (constants.ownership_tags: tezcatlipoca +
                comp-<name> + this deploy's run-<id>).
  * MARKER    - carries NO tags but its description holds this competition's clone marker (an
                interrupted clone: the clone POST wrote the description, the tag PUT never ran).
  * UNPROVEN  - carries no tags and no marker: ownership cannot be proven.
  * MISSING   - carries tags, but not all of the ownership set (another run's / another comp's).

Only OWNED and MARKER VMs are ever touched. A same-comp VM without this run's tag is a DIFFERENT
worktree's run (2026-10-02 near-miss) and is MISSING, never reclaimed."""

from pve_api import proxmox_api, wait_for_proxmox_task

OWNED = "owned"
MARKER = "marker"
UNPROVEN = "unproven"
MISSING = "missing"


def parse_vm_tags(raw):
    """VM tag string -> set; PVE joins tags with ';' in some views and ',' in others."""
    return {t.strip() for t in str(raw or "").replace(";", ",").split(",") if t.strip()}


def clone_marker(comp_name):
    """Ownership marker written as the clone's `description` IN the clone POST itself.
    /clone takes no `tags`, and the tagging PUT only lands after the clone task finishes —
    a host reboot or kill mid-clone used to leave an untagged, clone-locked VM in our own
    slot that preflight called "foreign" and blocked every later deploy (winad-testrun
    2026-09-25). The description exists from the first instant of the clone."""
    return f"tezcatlipoca-clone comp-{comp_name}"


def has_clone_marker(node, vmid, comp_name):
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return False
    return clone_marker(comp_name) in str(cfg.get("description") or "")


def comp_name_from_tags(expect_tags):
    """The competition name encoded in an ownership set's `comp-<name>` tag, or None."""
    return next((t[len("comp-"):] for t in expect_tags if t.startswith("comp-")), None)


def ownership_verdict(node, vmid, raw_tags, expect_tags):
    """Classify one VM against a full ownership set -> (verdict, missing_tags).

    The single definition of the ownership proof (module docstring). `raw_tags` is the VM's tag
    string from either the cluster resource list or its config. The clone-marker lookup is a
    Proxmox read and only happens for a tagless VM."""
    tags = parse_vm_tags(raw_tags)
    expect = set(expect_tags)
    if not tags:
        comp = comp_name_from_tags(expect)
        if comp and has_clone_marker(node, vmid, comp):
            return MARKER, set()
        return UNPROVEN, expect
    if expect <= tags:
        return OWNED, set()
    return MISSING, expect - tags


def unlock_vm(node, vmid, lock):
    """Clear a stale lock (an interrupted clone's 'clone'). PVE only lets root@pam touch
    `lock`; with a token we fail loudly with the exact command instead of 'foreign VM'."""
    try:
        proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data={"delete": "lock"})
        print(f"    vmid {vmid}: cleared stale '{lock}' lock (interrupted clone)")
    except Exception as e:
        raise RuntimeError(
            f"vmid {vmid} is stuck with lock '{lock}' from an interrupted clone and this API "
            f"token cannot clear it ({e}). On the node run:  qm unlock {vmid} && qm destroy "
            f"{vmid} --destroy-unreferenced-disks 1 --purge 1   then re-run the deploy.") from e


def gc_orphan_volumes(node, vmid):
    """Delete disk volumes named for `vmid` when no VM with that vmid exists. An interrupted
    clone/destroy can strand vm-<vmid>-disk-N zvols; the next clone into the slot then fails
    with 'already exists'. Only called for vmids we just verified are empty and ours by
    computed slot, so every volume here is a leftover of our own."""
    removed = 0
    for st in proxmox_api("GET", f"/nodes/{node}/storage", params={"content": "images"})["data"]:
        try:
            vols = proxmox_api("GET", f"/nodes/{node}/storage/{st['storage']}/content",
                               params={"vmid": vmid})["data"]
        except Exception:
            continue
        for v in vols:
            if int(v.get("vmid") or -1) != vmid:
                continue
            upid = proxmox_api("DELETE", f"/nodes/{node}/storage/{st['storage']}/content/{v['volid']}")["data"]
            if upid:
                wait_for_proxmox_task(node, upid)
            removed += 1
    if removed:
        print(f"    vmid {vmid}: removed {removed} orphaned disk volume(s) (no VM owns them)")
    return removed


def destroy_vm_if_exists(node, vmid, expect_tags=None):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        gc_orphan_volumes(node, vmid)
        return
    if expect_tags is not None:
        # Defense in depth for the parallel cleanup sweep: a VM that carries tags without
        # ours is not ours, whatever the vmid math says (the preflight vmid-clash gate is
        # the other half of the ownership proof). The expected set is the FULL ownership
        # set (constants.ownership_tags): comp tag + per-deploy run tag.
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        raw = str(cfg.get("tags") or "")
        verdict, missing = ownership_verdict(node, vmid, raw, expect_tags)
        if verdict == UNPROVEN:
            # Ownership must be PROVEN. An untagged VM is ours only when it carries this
            # competition's clone marker (an interrupted clone: the tag PUT never ran).
            raise RuntimeError(
                f"refusing to destroy UNTAGGED vmid {vmid} ({vm.get('name')}): ownership "
                f"cannot be proven (no tags, no clone marker of this competition).")
        if verdict == MARKER:
            print(f"    vmid {vmid} untagged but carries this competition's clone marker — "
                  f"interrupted clone, destroying")
        elif verdict == MISSING:
            raise RuntimeError(
                f"refusing to destroy vmid {vmid} ({vm.get('name')}): its tags '{raw}' are "
                f"missing {sorted(missing)} — outside this deploy's ownership set")
    lock = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("lock")
    if lock:
        unlock_vm(node, vmid, lock)
    if vm.get("status") == "running":
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid)
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}",
                       params={"destroy-unreferenced-disks": 1, "purge": 1})["data"]
    wait_for_proxmox_task(node, upid)


def retag_ownership(node, vmid, ownership):
    """Re-stamp an ADOPTED VM's tags to the current run's full ownership set.

    Templates kept across runs (M4 hash reuse) and ranges deployed before run ids
    existed carry the old tag set; without this, the next --full teardown's strict
    guard would skip them as foreign and the next preflight would refuse them as
    clashes. Only re-tags VMs already carrying this competition's comp tag — a
    foreign VM is left alone (loudly)."""
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    tags = parse_vm_tags(cfg.get("tags"))
    if ownership <= tags:
        return
    comp_tag = next((t for t in ownership if t.startswith("comp-")), None)
    if comp_tag and comp_tag not in tags:
        print(f"    WARNING: not re-tagging vmid {vmid}: tags '{cfg.get('tags')}' carry no "
              f"'{comp_tag}' — not provably this competition's")
        return
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config",
                data={"tags": ";".join(sorted(ownership))})
    print(f"    vmid {vmid}: ownership tags updated to {sorted(ownership)}")
