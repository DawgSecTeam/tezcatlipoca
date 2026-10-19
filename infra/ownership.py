"""Ownership-guarded Proxmox VM mutations: the ONE place the ownership-tag proof lives."""

from dataclasses import dataclass, field

from common.constants import ownership_tags
from infra.client import proxmox_api, wait_for_proxmox_task
from estate import config

# Only OWNED and MARKER VMs are ever touched; ownership_verdict is the single classification shared
# by the preflight clash gate and the destroy guard.
OWNED = "owned"
MARKER = "marker"
UNPROVEN = "unproven"
MISSING = "missing"


@dataclass(frozen=True)
class Ownership:
    """The destruction-identity of one deploy: which VMs a teardown may touch."""
    comp: str
    run_id: str = ""
    extra: frozenset = field(default_factory=frozenset)

    @property
    def tags(self):
        return frozenset(ownership_tags(self.comp, self.run_id or None, *self.extra))

    def with_role(self, *role_tags):
        return Ownership(self.comp, self.run_id, self.extra | frozenset(role_tags))


class Refusal(RuntimeError):
    """Subclasses RuntimeError so callers' `except RuntimeError` around the guard keeps catching it."""

    def __init__(self, saw, expected, remedy, detail=None):
        self.saw, self.expected, self.remedy = saw, expected, remedy
        self.detail = detail or f"saw {saw}; expected {expected}; remedy: {remedy}"
        super().__init__(self.detail)

    def __str__(self):
        return self.detail

    def as_line(self):
        import json
        return "TEARDOWN-REFUSAL " + json.dumps(
            {"saw": self.saw, "expected": self.expected, "remedy": self.remedy})


def parse_vm_tags(raw):
    """PVE joins tags with ';' in some views and ',' in others."""
    return {t.strip() for t in str(raw or "").replace(";", ",").split(",") if t.strip()}


def clone_marker(comp_name):
    """/clone takes no `tags` and tagging lands post-task, so the marker rides `description`."""
    return f"tezcatlipoca-clone comp-{comp_name}"


def full_clone_data(newid, name, comp_name):
    """Without `storage` a full clone inherits the source's pool — wrong pool for a golden."""
    data = {"newid": newid, "name": name, "full": 1, "description": clone_marker(comp_name)}
    storage = config.active().proxmox.datastore
    if storage:
        data["storage"] = storage
    return data


def has_clone_marker(node, vmid, comp_name):
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return False
    return clone_marker(comp_name) in str(cfg.get("description") or "")


def ownership_identity(comp_dir, recorded):
    """The recorded name wins: it is the ownership tag the deployed VMs already carry."""
    from common.utils import competition_name
    resolved = competition_name(comp_dir)
    if recorded and recorded != resolved:
        print(f"  NOTE: {comp_dir} records this range's competition as '{recorded}' but its "
              f"Compfile names '{resolved}' — using the RECORDED name, which is the ownership "
              f"tag its VMs carry.")
    return recorded or resolved


def comp_name_from_tags(expect_tags):
    return next((t[len("comp-"):] for t in expect_tags if t.startswith("comp-")), None)


def bridge_comment(comp_name, run_id, prose=""):
    """A bridge's PVE comment — the only ownership a bridge can carry (bridges have no tags)."""
    base = f"tezcatlipoca comp-{comp_name} {run_id}"
    return f"{base} — {prose}" if prose else base


def bridge_owned_by(comment, comp_name, run_id):
    """No run id is no proof: an unattributable bridge is never owned, never removed."""
    if not run_id or not comp_name:
        return False
    text = str(comment or "")
    return all(token in text for token in
               ("tezcatlipoca", f"comp-{comp_name}", run_id))


def vm_occupancy(node, vmid, expect_tags):
    try:
        vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        vm = next((v for v in vms if int(v["vmid"]) == int(vmid)), None)
        if vm is None:
            return "absent", set(), None
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:                                   # noqa: BLE001 - reported by the caller
        return "unknown", set(), None
    tags = parse_vm_tags(cfg.get("tags"))
    verdict, _missing = ownership_verdict(node, vmid, tags, expect_tags)
    return verdict, tags, vm


def ownership_verdict(node, vmid, raw_tags, expect_tags):
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
    try:
        proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data={"delete": "lock"})
        print(f"    vmid {vmid}: cleared stale '{lock}' lock (interrupted clone)")
    except Exception as e:
        raise RuntimeError(
            f"vmid {vmid} is stuck with lock '{lock}' from an interrupted clone and this API "
            f"token cannot clear it ({e}). On the node run:  qm unlock {vmid} && qm destroy "
            f"{vmid} --destroy-unreferenced-disks 1 --purge 1   then re-run the deploy.") from e


def gc_orphan_volumes(node, vmid):
    """Interrupted clone/destroy strands vm-<vmid>-disk-N zvols; the next clone into the slot then
    fails 'already exists'. Only called for vmids just verified empty and ours by slot."""
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
            try:
                upid = proxmox_api("DELETE", f"/nodes/{node}/storage/{st['storage']}/content/{v['volid']}")["data"]
                if upid:
                    wait_for_proxmox_task(node, upid)
            except Exception as e:  # noqa: BLE001 - stale index entries are not fatal
                # A content entry whose file is already gone fails the delete task
                # ("unable to open file") and the retry then 500s ("got no worker
                # upid"); the volume occupies nothing, and the clone that follows
                # reports a genuinely stuck volume with its own message.
                print(f"    vmid {vmid}: orphan volume {v['volid']} could not be removed ({e})")
                continue
            removed += 1
    if removed:
        print(f"    vmid {vmid}: removed {removed} orphaned disk volume(s) (no VM owns them)")
    return removed


def destroy_vm_if_exists(node, vmid, expect_tags=None, require_owned=False):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        gc_orphan_volumes(node, vmid)
        return
    if expect_tags is not None:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        raw = str(cfg.get("tags") or "")
        verdict, missing = ownership_verdict(node, vmid, raw, expect_tags)
        if verdict == UNPROVEN:
            raise Refusal(
                saw=f"UNTAGGED vmid {vmid} ({vm.get('name')})",
                expected="proof of ownership: this competition's tags or its clone marker",
                remedy="none — ownership cannot be proven, so this VM is never destroyed here",
                detail=f"refusing to destroy UNTAGGED vmid {vmid} ({vm.get('name')}): ownership "
                       f"cannot be proven (no tags, no clone marker of this competition).")
        if verdict == MARKER and require_owned:
            # The marker proves the competition, not the run — require_owned callers never adopt it.
            raise Refusal(
                saw=f"UNTAGGED vmid {vmid} ({vm.get('name')}) with only this competition's clone marker",
                expected="the full ownership set (this competition's tags + this run's tag)",
                remedy="none — the marker names no run, so this VM is never destroyed here",
                detail=f"refusing to destroy vmid {vmid} ({vm.get('name')}): it carries only the "
                       f"clone marker, which names no run, so it does not prove which run "
                       f"this VM belongs to")
        if verdict == MARKER:
            print(f"    vmid {vmid} untagged but carries this competition's clone marker — "
                  f"interrupted clone, destroying")
        elif verdict == MISSING:
            expected = sorted(set(missing) | set(parse_vm_tags(raw)))
            raise Refusal(
                saw=f"vmid {vmid} ({vm.get('name')}) carrying tags '{raw}'",
                expected=f"the full ownership set {expected}",
                remedy="none — it belongs to another run/competition and is never touched here",
                detail=f"refusing to destroy vmid {vmid} ({vm.get('name')}): its tags '{raw}' are "
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
    """Re-stamps adopted VMs whose old tag set the strict guard would skip as foreign.
    Only re-tags VMs already carrying this competition's comp tag."""
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
