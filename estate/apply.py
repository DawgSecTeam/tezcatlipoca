"""The estate component's creators: bridges, the engine, team boxes."""

import re
import time

import requests

from golden.golden_target_ops import _quote_sshkeys
from infra.client import proxmox_api, wait_for_proxmox_task
from infra.lifecycle import start_vm
from infra.ownership import (MARKER, OWNED, Refusal, clone_marker, destroy_vm_if_exists,
                             ownership_verdict)

ENGINE_CORES = 4
ENGINE_MEMORY_MB = 4096
ENGINE_DISK_GB = 40

_SIZE_TO_GB = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}

_CLONE_RETRIES = 4


def _tags(ownership):
    return ";".join(sorted(ownership.tags))


def _vm_config(node, vmid):
    return proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]


def _live_vmid(node, vmid):
    return any(v["vmid"] == vmid for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"])


def _configure(node, vmid, config):
    """PUT a VM config, retrying the brief post-clone publish window (400 "Parameter verification failed")."""
    for attempt in range(_CLONE_RETRIES):
        try:
            proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=config)
            return
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text[:300] if e.response is not None else ""
            if status != 400 or attempt == _CLONE_RETRIES - 1:
                raise RuntimeError(f"config PUT failed on vmid {vmid}: {body}") from e
            time.sleep(2 * (attempt + 1))


def _clone(node, source_vmid, vmid, name, ownership):
    """Linked-clone `source_vmid` into `vmid`, setting the clone marker in the POST: /clone takes no tags, so a crash mid-clone must not leave a VM no destroy path can claim."""
    data = {"newid": vmid, "name": name, "full": 0,
            "description": clone_marker(ownership.comp)}
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{source_vmid}/clone", data=data)["data"]
    wait_for_proxmox_task(node, upid)


def _adopt(node, vmid, ownership, name):
    """True when vmid already holds a VM this ownership owns; rebuild an interrupted clone, else Refusal."""
    if not _live_vmid(node, vmid):
        return False
    verdict, missing = ownership_verdict(
        node, vmid, _vm_config(node, vmid).get("tags"), ownership.tags)
    if verdict == OWNED:
        print(f"    vmid {vmid} ({name}) already carries this run's tags — adopting")
        return True
    if verdict == MARKER:
        print(f"    vmid {vmid} ({name}) is an interrupted clone of this competition — "
              f"rebuilding")
        destroy_vm_if_exists(node, vmid, expect_tags=ownership.tags)
        return False
    raise Refusal(
        saw=f"vmid {vmid} ({name}) already exists",
        expected=f"a free vmid, or one carrying this run's full tag set {sorted(ownership.tags)}",
        remedy="reclaim the range with destroy-competition.py, or resume the run that owns it",
        detail=f"vmid {vmid} ({name}) is already occupied and is not this run's "
               f"(missing tags: {sorted(missing)}). A deploy never destroys a leftover range.")


def _root_disk(config):
    for key in sorted(k for k in config if k.startswith(("scsi", "virtio", "sata", "ide"))):
        value = config[key]
        if "media=cdrom" in value or "cloudinit" in value or "size=" not in value:
            continue
        match = re.search(r"size=(\d+(?:\.\d+)?)([KMGT]?)", value)
        if match:
            return key, int(float(match.group(1)) * _SIZE_TO_GB[match.group(2)])
    return None, 0


def ensure_disk_size(node, vmid, disk_gb):
    """Grow the VM's root disk; a shrink would destroy data. Returns its key."""
    if not disk_gb:
        return None
    key, current = _root_disk(_vm_config(node, vmid))
    if key is None:
        raise RuntimeError(f"vmid {vmid} has no root disk to size to {disk_gb}G")
    if disk_gb > current:
        proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/resize",
                    data={"disk": key, "size": f"{disk_gb}G"})
    return key


def set_discard(node, vmid, key):
    """Turn on discard for an inherited clone disk: the PUT takes the volume plus options, not the reported `size=`."""
    if key is None:
        return
    current = str(_vm_config(node, vmid).get(key) or "")
    if not current or "discard=on" in current:
        return
    _configure(node, vmid, {key: current.split(",")[0] + ",discard=on"})


def create_bridge(state, node, name, comment=""):
    nets = {n.get("iface") for n in proxmox_api("GET", f"/nodes/{node}/network")["data"]}
    if name in nets:
        return False
    # No `bridge_ports`: PVE resolves each entry of the port LIST, so "no ports" is expressed by omitting it.
    proxmox_api("POST", f"/nodes/{node}/network",
                data={"iface": name, "type": "bridge", "autostart": 1,
                      "comments": comment or name})
    # POST stages the change; PUT applies it, and a VM cannot attach to a staged bridge.
    # The apply is an async task: starting the engine before it finishes failed
    # qmstart with "bridge does not exist" (live: run-119f3058).
    upid = proxmox_api("PUT", f"/nodes/{node}/network")["data"]
    if upid:
        wait_for_proxmox_task(node, upid)
    state.record("bridge", node, name)
    state.save()
    return True


def create_engine(state, ownership, node, *, vmid, template_vmid, team_identifiers,
                  transit_identifiers=(), mgmt_ip="", mgmt_gw="", mgmt_bridge="vmbr0",
                  username="sysadmin", ssh_public_key="", red_bridge=None):
    """The scoring engine: a linked clone of the engine template wired to every team bridge;
    `red_bridge` appends the private red-segment NIC LAST so the netplan ens numbering is unchanged."""
    if not _adopt(node, vmid, ownership, "quotient-engine"):
        _clone(node, template_vmid, vmid, "quotient-engine", ownership)
        config = {"cores": ENGINE_CORES, "memory": ENGINE_MEMORY_MB, "agent": 1,
                  "tags": _tags(ownership)}
        bridges = [mgmt_bridge] + [f"vmbr{t}" for t in team_identifiers] \
                           + [f"vmbrW{t}" for t in transit_identifiers] \
                           + ([red_bridge] if red_bridge else [])
        for index, bridge in enumerate(bridges):
            config[f"net{index}"] = f"virtio,bridge={bridge}"
        if mgmt_ip:
            config.update({
                "ciuser": username,
                "sshkeys": _quote_sshkeys(ssh_public_key),
                "ipconfig0": f"ip={mgmt_ip}/24" + (f",gw={mgmt_gw}" if mgmt_gw else ""),
                "nameserver": "8.8.8.8",
            })
        _configure(node, vmid, config)
        set_discard(node, vmid, ensure_disk_size(node, vmid, ENGINE_DISK_GB))
        start_vm(node, vmid)
    state.record("engine", node, "quotient-engine", vmid=vmid, tags=ownership.tags)
    state.save()
    return vmid


def create_team_box(state, ownership, target, *, source_vmid, box_username, box_password,
                    ssh_public_key):
    box, node, vmid, name = target["box"], target["node"], target["vmid"], target["vm_name"]
    if not _adopt(node, vmid, ownership, name):
        _clone(node, source_vmid, vmid, name, ownership)
        config = {"cores": box["cpu"], "memory": box["memory_mb"], "agent": 1,
                  "tags": _tags(ownership)}
        # pfSense keys its interfaces on vtnet0/vtnet1, so the in-path WAN NIC comes FIRST.
        bridges = ([f"vmbrW{target['identifier']}"] if box.get("in_path") else []) \
            + [f"vmbr{target['identifier']}"]
        for index, bridge in enumerate(bridges):
            config[f"net{index}"] = f"virtio,bridge={bridge}"
        # No cloud-init identity on Windows (bootstrap_windows_box owns it) or on an unmanaged appliance.
        if not box.get("unmanaged") and "win" not in box["template"].lower():
            config.update({
                "ciuser": box_username,
                "cipassword": box_password,
                "sshkeys": _quote_sshkeys(ssh_public_key),
                "ipconfig0": f"ip={target['ip']}/24,gw=192.168.{target['identifier']}.1",
                # Never the team's own dns box: nakon installs bind9 over apt, which needs a working resolver.
                "nameserver": "8.8.8.8",
            })
        _configure(node, vmid, config)
        set_discard(node, vmid, ensure_disk_size(node, vmid, box.get("disk_gb")))
        start_vm(node, vmid)
    state.record("team_box", node, name, vmid=vmid, tags=ownership.tags)
    state.save()
    return vmid


def create_team_boxes(state, ownership, targets, *, sources, box_username, box_password,
                      ssh_public_key):
    """Every target's box, linked-cloned serially — one API writer per node is the proven shape."""
    for target in targets:
        create_team_box(state, ownership, target, source_vmid=sources[target["vm_name"]],
                        box_username=box_username, box_password=box_password,
                        ssh_public_key=ssh_public_key)
    return [target["vmid"] for target in targets]
