"""Images: base resolution through gcp.image_map, custom images (goldens, engine
template), and machine images (the tz restore points).

Goldens become custom images derived from each golden instance's boot disk —
the `qm template` equivalent — and every reuse/rebuild decision reads the hash
the image itself carries as a LABEL (`tezcatlipoca-m4 hash=`), node-side truth
like the PVE template description.
"""

from providers.gcp import compute
from providers.gcp.client import gapi

HASH_LABEL = "tezcatlipoca-m4"
# GCP label values cap at 63 chars; a sha256 is 64 hex. The stamp keeps the digest's WEAKEST
# bit (the last char) dropped — both write and compare go through this, so reuse semantics
# are unchanged.
HASH_MAX = 63


def hash_value(digest):
    return str(digest)[:HASH_MAX]


def resolve_base(creds, template_name):
    """boxes.json's template name -> an absolute sourceImage URL.

    A map entry is {"project": ..., "image": "<family or concrete build name>"}.
    Stock families resolve to their newest non-deprecated image (builds move daily);
    a name ending in -v<digits> (…) `-v20261105` is a concrete build and is used as-is.
    """
    from estate import config
    entry = (config.active().get("gcp.image_map") or {}).get(template_name)
    if not entry:
        raise SystemExit(
            f"  ERROR: template {template_name!r} has no [gcp.image_map] entry — the gcp "
            f"platform cannot pick its base image. Add `{template_name} = "
            f"{{ project = \"ubuntu-os-cloud\", image = \"ubuntu-2404-lts-amd64\" }}` under "
            f"[gcp.image_map] in the profile, or deploy this competition on proxmox.")
    project, image = entry.get("project"), entry.get("image")
    if not project or not image:
        raise SystemExit(f"  ERROR: [gcp.image_map.{template_name}] needs 'project' and 'image'.")
    if image.rsplit("-", 1)[-1][:1] == "v" and image.rsplit("-", 1)[-1][1:].isdigit():
        return f"projects/{project}/global/images/{image}"
    info = compute.newest_family_image(creds, project, image)
    if info.get("status") == "DEPRECATED":
        raise SystemExit(
            f"  ERROR: image family {project}/{image} ({info.get('name')}) is DEPRECATED — pin "
            f"a live family in gcp.image_map before the goldens bake it into a range.")
    return f"projects/{project}/global/images/{info['name']}"


def cloud_config_user(username, password, public_key):
    """The PVE ciuser/cipassword/sshkeys equivalent (cloud-init on the GCE datasource)."""
    key = (public_key or "").strip()
    return (
        "#cloud-config\n"
        "users:\n"
        f"  - name: {username}\n"
        f"    plain_text_passwd: {password}\n"
        "    lock_passwd: false\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {key}\n"
        "ssh_pwauth: true\n"
    )


def image_hash(info):
    """The hash label an image carries (None when it stamps none — a foreign image)."""
    return (info.get("labels") or {}).get(HASH_LABEL)


def create_from_instance(creds, name, instance_info, labels, family=None, log=print):
    """A stopped instance's boot disk becomes a custom image (goldens, engine template).

    The hash label is clamped to GCP's 63-char value cap here — a caller passing the raw
    64-char digest survives instead of failing the create after a 20-minute build.
    """
    source = next(d["source"] for d in instance_info.get("disks", []) if d.get("boot"))
    labels = dict(labels)
    if HASH_LABEL in labels:
        labels[HASH_LABEL] = hash_value(labels[HASH_LABEL])
    if len(name) > 63:
        raise SystemExit(f"  ERROR: image name {name!r} exceeds GCP's 63-char limit")
    body = {"name": name, "sourceDisk": source, "labels": labels}
    if family:
        body["family"] = family
    log(f"  image     : creating {name}")
    compute.wait_op(creds, gapi(creds, "POST", "global/images", data=body), timeout=3600)
    return compute.image(creds, name)


def delete_image(creds, name):
    try:
        operation = gapi(creds, "DELETE", f"global/images/{name}")
    except RuntimeError as e:
        if "404" in str(e):
            return False
        raise
    compute.wait_op(creds, operation)
    return True


def machine_image_mark(creds, name, instance_name, labels, log=print):
    """The tz-base/tz-ready point: a machine image (config + disks) of the stopped instance."""
    log(f"  snapshot  : {name}")
    compute.wait_op(creds, gapi(creds, "POST", f"regions/{compute.region()}/machineImages", data={
        "name": name,
        "sourceInstance": f"zones/{compute.zone()}/instances/{instance_name}",
        "labels": labels}), timeout=3600)
    return name


def list_marks(creds, labels):
    return {mi["name"] for mi in compute.machine_images(creds, list(labels.items()))}


def delete_machine_image(creds, name):
    return compute.delete_op(
        creds, f"https://compute.googleapis.com/compute/v1/projects/"
               f"{creds['project_id']}/regions/{compute.region()}/machineImages/{name}")
