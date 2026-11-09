"""Instance lifecycle: create from an image with pinned NICs, power, delete, MAC readback.

GCP pins each NIC's private IP at create time (`networkIP`), so the whole per-box
netplan dance the Proxmox flow needs is replaced by the API call itself; the guest
DHCP-leases exactly the pinned address with the subnet's .1 (the engine) as its
default route and the GCE resolver for names. `wait_for_*` boot artifacts land via
the same SSH paths the neutral phases already use (through the engine).
"""

from providers.gcp import compute
from providers.gcp.client import gapi


def machine_type(cpu, memory_mb):
    """E2 custom carries the boxes.json shapes verbatim (cpu x memory_mb exactly)."""
    return f"e2-custom-{int(cpu)}-{int(memory_mb)}"


def nic(subnet_name, ip, external=None):
    """The create order IS the guest's NIC order: mgmt first, then teams, transit, red.

    `external="EPHEMERAL"` gets an auto-assigned public IP (the ONE_TO_ONE_NAT accessConfig
    with NO natIP field — a present natIP names a SPECIFIC reserved address, and a bare
    IP value is refused); any other truthy value must be an address selfLink.
    """
    if external == "EPHEMERAL":
        access = {"name": "External NAT", "type": "ONE_TO_ONE_NAT"}
    elif external:
        access = {"name": "External NAT", "type": "ONE_TO_ONE_NAT", "natIP": external}
    else:
        access = None
    if access is None:
        return {"subnetwork": f"regions/{compute.region()}/subnetworks/{subnet_name}",
                "networkIP": ip}
    return {"subnetwork": f"regions/{compute.region()}/subnetworks/{subnet_name}",
            "networkIP": ip, "accessConfigs": [access]}


def metadata(ssh_user=None, ssh_public_key="", cloud_config="", windows_startup_ps1=""):
    items = []
    if ssh_user and ssh_public_key:
        items.append({"key": "ssh-keys",
                      "value": f"{ssh_user}:{ssh_public_key.strip()}"})
    if cloud_config:
        items.append({"key": "user-data", "value": cloud_config})
    if windows_startup_ps1:
        items.append({"key": "windows-startup-script-ps1",
                      "value": windows_startup_ps1})
    return {"items": items} if items else {}


def create(creds, name, *, image, machine, nics, labels, disk_gb=0,
           network_tags=(), meta=None, can_ip_forward=False, log=print):
    log(f"  instance  : creating {name} ({machine}, {len(nics)} NIC(s))")
    body = {
        "name": name,
        "machineType": f"zones/{compute.zone()}/machineTypes/{machine}",
        "disks": [{"boot": True, "autoDelete": True, "type": "PERSISTENT",
                   "initializeParams": {"sourceImage": image,
                                        **({"diskSizeGb": int(disk_gb)} if disk_gb else {})}}],
        "networkInterfaces": nics,
        "labels": labels,
        "metadata": meta or {"items": []},
    }
    # The project's default compute SA on EVERY instance: without it the guest
    # environment boots auth-dead (serial console: GCE metadata
    # "instance/service-accounts/default/token" not defined) and never runs
    # windows-startup-script-ps1, so a Windows box never gets sshd and the whole
    # bake times out at wait_for_boxes_ssh.
    sa = compute.default_service_account(creds)
    if sa:
        body["serviceAccounts"] = [{
            "email": sa,
            "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
        }]
    if can_ip_forward:
        # The engine is the range's router: the VPC default route's next hop must be
        # allowed to forward, or GCP drops every forwarded packet.
        body["canIpForward"] = True
    if network_tags:
        body["tags"] = {"items": list(network_tags)}
    compute.wait_op(creds, gapi(creds, "POST", f"zones/{compute.zone()}/instances", data=body),
                    timeout=1800)
    return compute.instance(creds, name)


def start(creds, name):
    info = compute.instance(creds, name)
    if info and info.get("status") != "RUNNING":
        compute.wait_op(creds, gapi(creds, "POST",
                                   f"zones/{compute.zone()}/instances/{name}/start"),
                        timeout=900)


def boot_disk(creds, name):
    """The instance's boot PD selfLink — the resource resize calls in take it."""
    info = compute.instance(creds, name) or {}
    source = next((d.get("source") for d in info.get("disks") or [] if d.get("boot")), None)
    if not source:
        raise RuntimeError(f"{name} has no boot disk to grow")
    return source


def resize_boot_disk(creds, disk_url, target_gb):
    """Grow a PD to an absolute size, only when smaller; returns the size it had before."""
    disk = gapi(creds, "GET", disk_url)
    current = int(disk.get("sizeGb") or 0)
    if current < int(target_gb):
        compute.wait_op(creds, gapi(creds, "POST", f"{disk_url}/resize",
                                    data={"sizeGb": str(int(target_gb))}))
    return current


def stop(creds, name, timeout=900):
    info = compute.instance(creds, name)
    if info and info.get("status") != "TERMINATED":
        compute.wait_op(creds, gapi(creds, "POST",
                                   f"zones/{compute.zone()}/instances/{name}/stop"),
                        timeout=timeout)


def delete(creds, name):
    try:
        operation = gapi(creds, "DELETE", f"zones/{compute.zone()}/instances/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return False
        raise
    compute.wait_op(creds, operation, timeout=900)
    return True


def nic_macs(creds, name):
    """MACs after NIC0 (the mgmt NIC), in create order — the netplan's pin indexes.

    This project's instance GET does not expose macAddress (not even as a fields
    selection), so the readback pads empty strings and the netplan pins by NAME —
    GCE udev names NICs ens4+ in create order, which is just as deterministic.
    """
    info = compute.instance(creds, name)
    return [n.get("macAddress") or "" for n in (info.get("networkInterfaces") or [])[1:]]


def primary_nic_info(creds, name):
    """NIC0's addresses (the recorded identity for the engine: public + private)."""
    info = compute.instance(creds, name)
    first = (info.get("networkInterfaces") or [{}])[0]
    external = ""
    for access in first.get("accessConfigs") or []:
        external = access.get("natIP") or ""
    return {"private": first.get("networkIP") or "", "public": external}
