"""The GCP provider: Compute Engine ranges — one VPC per run, labels for ownership.

Mapping (docs/architecture.md keeps the full table):
- bridges            -> a per-run VPC with a subnet per team (+ mgmt/red/transit)
- team isolation     -> the engine as the subnet .1 router (iptables) + GCP's implied
                        cross-subnet deny; egress rides Cloud NAT
- PVE tags/marker     -> labels on every instance/image (`naming`); creates are atomic,
                        so the interrupted-clone marker class has no analog
- qm template / clone -> custom images; every team box boots its golden image
- tz snapshots        -> machine images per box (tz-base / tz-ready); GCP requires the
  instance stopped for a machine image, so taking a restore point reboots the box —
  restore-point semantics are disk-equivalent, which is what redeploy restores anyway
- guest agent channel -> none on GCP; the boxes carry their identity in metadata and
  the driver reaches the engine/red01 through their (firewalled) public IPs. Every
  through-the-engine SSH path the phases use runs unchanged.
"""

import hashlib
import json
from pathlib import Path

from common import utils
from common.constants import RED_SEGMENT_GW, RED_SEGMENT_IP, RED_SEGMENT_SUBNET, GOLDEN_TAG
from common.ssh_ops import forget_engine_host_key, ssh_via_gateway, wait_for_ssh
from common.timing import timed
from providers.gcp import compute, image_ops, instance_ops, network_ops, naming
from providers.gcp.naming import labels as ownership_labels

PLATFORM = "gcp"

# The engine's and red01's management NICs live on the mgmt subnet (ops ingress).
MGMT_ENGINE_IP = network_ops.MGMT_ENGINE_IP
MGMT_RED01_IP = "10.224.0.20"
RED01_TAG = "tz-red01"


def _creds():
    return compute.session()


def _prefix(ctx):
    return naming.prefix(ctx.comp_name, ctx.run_id)


def _ssh_public_key():
    from estate import config
    pub = Path(str(config.ssh_key_path()) + ".pub")
    return pub.read_text().strip() if pub.is_file() else ""


def _instance_name(prefix, target):
    return naming.instance_name(prefix, target["vm_name"])


# ---- phase 1 ----

def reclaim_interrupted(ctx):
    # GCP creates are atomic and labeled up front, so the PVE clone-marker reclaim (a
    # VM built between the clone POST and the tag PUT) has no analog: anything that
    # exists carries the full ownership set, which preflight and teardown classify.
    print("  Reclaim: gcp creates are atomic and labeled — no marker reclaim needed")


# ---- phase 2: the engine template image ----

def engine_template(ctx):
    from engine.engine_bootstrap_ops import bootstrap_scoring_engine
    from engine.engine_identity_ops import clean_engine_for_template, stamp_engine_build
    from engine import template_freeze
    from estate import config
    from common.utils import compfile_value, stages_red
    from deploy_lib.phases._estate import has_in_path_firewall, slot_team_identifiers

    creds = _creds()
    prefix = _prefix(ctx)
    # The build VM lives on this run's OWN mgmt subnet (behind the same ingress rule the
    # engine gets), so the VPC must exist before the template does — create_estate later
    # re-runs ensure_network and finds it all standing.
    network_ops.ensure_network(ctx.document(), comp_name=ctx.comp_name, run_id=ctx.run_id,
                               team_identifiers=slot_team_identifiers(ctx),
                               in_path=has_in_path_firewall(ctx), red=stages_red(ctx.comp_dir))
    name = naming.image_name(prefix, "engine")
    base = image_ops.resolve_base(creds, "engine-base")
    quotient_ref = compfile_value(ctx.comp_dir / "Compfile", "quotient_ref")
    # The PVE hash key is the base template's vmid; gcp's base identity is the resolved
    # image URL, normalized to an int so the SAME freeze machinery drives drift.
    base_key = int.from_bytes(hashlib.sha256(base.encode()).digest()[:8], "big")
    engine_inputs = template_freeze.engine_hash_inputs(base_key, quotient_ref,
                                                        bootstrap_scoring_engine)
    engine_hash = template_freeze.hash_from_inputs(engine_inputs)
    stored = template_freeze.load_template_hashes(ctx.comp_dir)

    existing = compute.image(creds, name)
    rebuild = True
    if existing is not None:
        if image_ops.image_hash(existing) == image_ops.hash_value(engine_hash):
            print(f"  Engine image hash matches — reusing {name}")
            rebuild = False
        elif not template_freeze.frozen_gate(
                ctx.comp_dir, (stored.get("engine") or {}).get("inputs"),
                engine_inputs, "engine template"):
            print(f"  Engine image {name}: frozen — keeping the stamped image")
            rebuild = False
    if rebuild:
        if existing is not None:
            print("  Engine image hash differs — rebuilding...")
            image_ops.delete_image(creds, name)
        vm_name = f"{prefix}-enginebuild"
        instance_ops.delete(creds, vm_name)
        username = config.active().engine.username
        build = instance_ops.create(
            creds, vm_name, image=base,
            machine=instance_ops.machine_type(4, 8192),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "mgmt"), MGMT_ENGINE_IP,
                                   external="EPHEMERAL")],
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "engine-build"),
            disk_gb=40, network_tags=(network_ops.ENGINE_TAG,),
            meta=instance_ops.metadata(username, _ssh_public_key()))
        build_ip = (build["networkInterfaces"][0].get("accessConfigs") or [{}])[0].get("natIP")
        forget_engine_host_key(build_ip)
        print(f"    engine build VM {build_ip} (from {base})")
        if not wait_for_ssh(Path(ctx.ssh_key_abs), username, build_ip, timeout=900):
            raise RuntimeError(f"engine-template build VM {build_ip} never accepted SSH")
        build_ctx = {"ssh_key_path": str(ctx.ssh_key_abs), "vm_username": username,
                     "box_username": username, "scoring_engine_ip": build_ip,
                     "engine_build": {"name": vm_name}}
        stamp_engine_build(build_ctx, template_freeze.engine_template_vmid(ctx.engine_vmid))
        if not _box_settled(build_ctx, build_ip, vmid=ctx.engine_vmid):
            print("    WARNING: engine build VM never fully settled — proceeding")
            utils.record_degradation("engine build VM never fully settled", build_ip)
        bootstrap_scoring_engine(build_ctx, ctx.postgres_password, ctx.redis_password,
                                 quotient_ref=quotient_ref)
        clean_engine_for_template(build_ctx,
                                  template_freeze.engine_template_vmid(ctx.engine_vmid))
        instance_ops.stop(creds, vm_name)
        image_ops.create_from_instance(
            creds, name, compute.instance(creds, vm_name),
            labels={**ownership_labels(ctx.comp_name, ctx.run_id, "engine-template"),
                    image_ops.HASH_LABEL: engine_hash})
        instance_ops.delete(creds, vm_name)
        print(f"    {name} is the engine template image (hash stamped)")
    ctx.state["engine_template_vmid"] = template_freeze.engine_template_vmid(ctx.engine_vmid)
    ctx.state["engine_template_hash"] = engine_hash
    template_freeze.save_template_hashes(
        ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.save_state()
    document = ctx.document()
    document.record("engine_template", compute.zone(), name)
    document.save()
    return name


def _box_settled(build_ctx, ip, vmid):
    """cloud-init + apt quiet, over the SSH channel (there is no guest agent on gcp)."""
    from guests.box_settle_ops import _box_settled_via_ssh
    return bool(_box_settled_via_ssh(build_ctx, {"vmid": vmid, "ip": ip}))


# ---- phase 2: networks + the engine ----

def create_estate(ctx):
    from deploy_lib.phases._estate import has_in_path_firewall, slot_team_identifiers
    from common.utils import stages_red
    from estate import config

    creds = _creds()
    prefix = _prefix(ctx)
    teams = slot_team_identifiers(ctx)
    document = ctx.document()
    with timed(ctx.comp_dir, 2, "create_network"):
        net = network_ops.ensure_network(
            document, comp_name=ctx.comp_name, run_id=ctx.run_id,
            team_identifiers=teams, in_path=has_in_path_firewall(ctx),
            red=stages_red(ctx.comp_dir))
    engine_name = _instance_name(prefix, {"vm_name": "quotient-engine"})
    engine_image = naming.image_name(prefix, "engine")
    username = config.active().engine.username
    if compute.instance(creds, engine_name) is None:
        nics = [instance_ops.nic(net["mgmt_subnet"], MGMT_ENGINE_IP,
                                 external=_engine_address(creds, prefix))]
        for identifier in teams:
            nics.append(instance_ops.nic(naming.subnet_name(prefix, "t", identifier),
                                         f"192.168.{identifier}.1"))
        if has_in_path_firewall(ctx):
            for identifier in teams:
                nics.append(instance_ops.nic(naming.subnet_name(prefix, "w", identifier),
                                             f"172.31.{identifier}.1"))
        if stages_red(ctx.comp_dir):
            nics.append(instance_ops.nic(naming.subnet_name(prefix, "red"), RED_SEGMENT_GW))
        instance_ops.create(
            creds, engine_name,
            image=f"projects/{creds['project_id']}/global/images/{engine_image}",
            machine=instance_ops.machine_type(4, 8192), nics=nics,
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "engine",
                                     unit=ctx.engine_vmid),
            disk_gb=40, network_tags=(network_ops.ENGINE_TAG,),
            meta=instance_ops.metadata(username, _ssh_public_key()))
        document.record("engine", compute.zone(), engine_name, vmid=ctx.engine_vmid)
        document.save()
    # The engine's PUBLIC ip is the run's mgmt address (firewalled to ingress_sources)
    # and the address every later tool records as the scoring engine.
    primary = instance_ops.primary_nic_info(creds, engine_name)
    if not primary["public"]:
        raise RuntimeError("the engine's external IP is missing — the mgmt NIC create failed")
    ctx.engine_mgmt_ip = primary["public"]
    ctx.state["engine_mgmt_ip"] = primary["public"]
    document.section("identity")["engine_mgmt_ip"] = primary["public"]
    ctx.save_state()
    # The netplan pins NICs by MAC in create order (mgmt first); the netplan's index 0
    # is the FIRST TEAM NIC, so handler MACs start after NIC0, as on proxmox.
    ctx.engine_nic_macs = instance_ops.nic_macs(creds, engine_name)
    print(f"  Engine: {engine_name} at {primary['public']} (mgmt)")


def _engine_address(creds, prefix):
    info = compute.address(creds, naming.address_name(prefix, "engine"))
    if not info:
        raise RuntimeError("the engine's reserved address is missing — ensure_network ran"
                          " out of order")
    # accessConfigs.natIP takes the address RESOURCE selfLink, never the bare IP value.
    return info["selfLink"]


# ---- phase 4: goldens ----

def build_golden_sets(ctx, stored, coverage_recorder):
    """Per box type: instance from the base image -> plant (the SSH machinery the
    proxmox flow uses) -> tz-base machine image -> custom image stamped with the
    golden hash, mirroring `qm template`. The image's OWN hash label decides reuse,
    the same node-side truth the PVE template description carries."""
    from common.constants import SNAP_BASE
    from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
    from engine import template_freeze
    from golden import golden_target_ops
    from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
    from guests.ubuntu_auth_ops import setup_ubuntu_auth
    from nakonops.nakon_bundle_ops import build_nakon_bundle
    from nakonops.nakon_run_ops import run_nakon

    creds = _creds()
    prefix = _prefix(ctx)
    targets = golden_target_ops.golden_targets(ctx.engine_vmid, ctx.teams, ctx.boxes,
                                               slot=0, anchor_identifier=None)
    machine_cfg = json.loads(ctx.golden_config_path.read_text())["machines"]
    by_box = {m["name"].removesuffix("-golden"): m for m in machine_cfg}
    golden_ids = {}

    for target in targets:
        box = target["box"]["name"]
        image = naming.image_name(prefix, "golden", box)
        machine = _instance_name(prefix, target)
        digest = (ctx.golden_hashes or {}).get(box) or ""
        existing = compute.image(creds, image)
        if existing is not None:
            if image_ops.image_hash(existing) == image_ops.hash_value(digest):
                print(f"  Golden {box}: image hash matches — reusing {image}")
                golden_ids[box] = target["vmid"]
                continue
            if not template_freeze.frozen_gate(
                    ctx.comp_dir, (stored.get("golden") or {}).get(box, {}).get("inputs"),
                    (ctx.golden_inputs or {}).get(box), f"golden {box}"):
                print(f"  Golden {box}: frozen — keeping the stamped image {image}")
                golden_ids[box] = target["vmid"]
                continue
            print(f"  Golden {box}: hash differs — rebuilding {image}")
            image_ops.delete_image(creds, image)

        built = _build_one_golden(ctx, creds, prefix, target, machine, by_box, coverage_recorder)
        image_labels = ownership_labels(ctx.comp_name, ctx.run_id, GOLDEN_TAG,
                                        unit=target["vmid"])
        if digest:
            image_labels = {**image_labels, image_ops.HASH_LABEL: digest}
        image_ops.create_from_instance(creds, image, built, labels=image_labels)
        instance_ops.delete(creds, machine)
        golden_ids[box] = target["vmid"]
        print(f"  Golden {box}: image {image} ready")
    return golden_ids, {0: golden_ids}


def _build_one_golden(ctx, creds, prefix, target, machine, by_box, coverage_recorder):
    from common.constants import SNAP_BASE
    from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
    from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
    from guests.ubuntu_auth_ops import setup_ubuntu_auth
    from nakonops.nakon_bundle_ops import build_nakon_bundle
    from nakonops.nakon_run_ops import run_nakon

    box = target["box"]["name"]
    identifier = target["ip"].split(".")[2]  # goldens sit on team1's subnet at .240+
    info = compute.instance(creds, machine)
    if info is None:
        base = image_ops.resolve_base(creds, target["box"]["template"])
        info = instance_ops.create(
            creds, machine, image=base,
            machine=instance_ops.machine_type(target["box"]["cpu"],
                                               target["box"]["memory_mb"]),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "t", identifier),
                                   target["ip"])],
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "golden-build",
                                    unit=target["vmid"]),
            disk_gb=target["box"].get("disk_gb") or 10,
            meta=instance_ops.metadata(
                ctx.box_username, _ssh_public_key(),
                image_ops.cloud_config_user(ctx.box_username, ctx.box_password,
                                           _ssh_public_key())))
        print(f"  Golden {box}: instance {machine} from {base}")
    else:
        print(f"  Golden {box}: adopting the staged instance {machine} (re-planting)")
        instance_ops.start(creds, machine)
    wait_for_boxes_ssh(ctx.tf_ctx, [target], timeout=600)
    wait_for_cloud_init(ctx.tf_ctx, [target], timeout=240)
    setup_ubuntu_auth([target], ctx.tf_ctx)
    fix_dns_on_boxes([target], ctx.tf_ctx)
    prep_apt_on_boxes([target], ctx.tf_ctx)
    image_ops.machine_image_mark(
        creds, naming.machine_image_name(prefix, target["vm_name"], SNAP_BASE),
        machine, ownership_labels(ctx.comp_name, ctx.run_id))

    machines = [{"name": f"{box}-golden",
                 "configurations": (by_box.get(box) or {}).get("configurations", [])}]
    result = None
    if machines[0]["configurations"]:
        bundle = build_nakon_bundle(ctx.golden_config_path)
        result = run_nakon(ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip, bundle,
                          ctx.golden_config_path, only=[f"{box}-golden"],
                          timeout=2400, strict=True, jobs=ctx.nakon_jobs,
                          run_tag="golden")
        if result.failed:
            raise RuntimeError(f"golden plant for {box} had {len(result.failed)} FAILED "
                              f"step(s) — strict mode requires green")
    coverage_recorder(machines, result)
    cloud_clean = ssh_via_gateway(ctx.tf_ctx, target["ip"],
                                  "sudo cloud-init clean --logs --machine-id", timeout=120)
    instance_ops.stop(creds, machine)
    return compute.instance(creds, machine)


def apply_team_boxes(ctx, sources):
    creds = _creds()
    prefix = _prefix(ctx)
    from common.utils import is_unmanaged
    for target in ctx.all_targets:
        box = target["box"]
        if is_unmanaged(box):
            # in-path appliances are refused at preflight's matrix check; reaching here
            # means a lineup dodged it — refuse rather than half-deploy a dead box.
            raise SystemExit(f"  ERROR: unmanaged box {box['name']} has no gcp path — the "
                             f"platform matrix refuses this lineup")
        name = _instance_name(prefix, target)
        if compute.instance(creds, name) is not None:
            continue
        golden_image = naming.image_name(prefix, "golden", box["name"])
        instance_ops.create(
            creds, name,
            image=f"projects/{creds['project_id']}/global/images/{golden_image}",
            machine=instance_ops.machine_type(box["cpu"], box["memory_mb"]),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "t", target["identifier"]),
                                   target["ip"])],
            labels=ownership_labels(ctx.comp_name, ctx.run_id, "team_box",
                                    unit=target["vmid"]),
            disk_gb=box.get("disk_gb") or 10,
            meta=instance_ops.metadata(
                ctx.box_username, _ssh_public_key(),
                image_ops.cloud_config_user(ctx.box_username, ctx.box_password,
                                           _ssh_public_key())))
        ctx.document().record("team_box", compute.zone(), name, vmid=target["vmid"])
    ctx.document().save()


def windows_bootstrap(ctx, target, features=()):
    raise SystemExit(
        "  ERROR: Windows boxes on the gcp platform are not implemented yet (the "
        "boot-time channel moves from the QEMU guest agent to startup-script-ps1 on "
        "GCE stock images) — deploy this competition on proxmox, or trim the Windows "
        "boxes. Tracked as the gcp provider's Windows workstream.")


# ---- restore points (machine images) ----

def restore_point_create(ctx, target, name, description=""):
    creds = _creds()
    prefix = _prefix(ctx)
    instance = _instance_name(prefix, target)
    info = compute.instance(creds, instance)
    if info is None:
        print(f"    WARNING: snapshot {name} skipped — {instance} does not exist")
        return False
    machine_image = naming.machine_image_name(prefix, target["vm_name"], name)
    was_running = info.get("status") != "TERMINATED"
    if was_running:
        instance_ops.stop(creds, instance, timeout=1800)
    try:
        image_ops.machine_image_mark(
            creds, machine_image, instance,
            ownership_labels(ctx.comp_name, ctx.run_id, unit=target["vmid"]))
    finally:
        if was_running:
            instance_ops.start(creds, instance)
    return True


def restore_points_list(ctx, target):
    creds = _creds()
    prefix = _prefix(ctx)
    head = f"{prefix}-{target['vm_name']}-"
    marks = set()
    for name in image_ops.list_marks(creds, ownership_labels(ctx.comp_name, ctx.run_id)):
        if name.startswith(head):
            marks.add(name[len(head):])
    return marks


def restore_point_rollback(ctx, target, name):
    raise SystemExit(
        "  ERROR: redeploy's rollback modes are not implemented on gcp yet — a machine "
        "image restores by RECREATING the instance, which the redeploy engine does not "
        "do today. Use --mode reconfigure, or redeploy the range. Tracked as the gcp "
        "provider's redeploy workstream.")


def restore_point_hint(ctx, target):
    if not restore_points_list(ctx, target):
        return (f"no machine images for {target['vm_name']} — either this range predates "
                f"snapshotting or the machine-images quota filled. Use --mode "
                f"reconfigure on gcp.")
    return ""


# ---- resume guards ----

def live_unit_numbers():
    """Every machine's unit label in this zone: guard_resume_existence asks whether the
    roster's recorded units exist, and an instance's tz-unit label is its live proof."""
    creds = compute.session()
    units = set()
    for info in compute.instances(creds):
        raw = (info.get("labels") or {}).get(naming.LABEL_UNIT)
        if raw:
            try:
                units.add(int(raw))
            except ValueError:
                continue
    return units


# ---- recorded-estate teardown ----

def destroy_estate(document, *, full=True, nodes=None, log=print):
    creds = compute.session()
    identity = document.data.get("identity") or {}
    comp = identity.get("comp_name") or ""
    run_id = identity.get("run_id") or ""
    prefix = naming.prefix(comp, run_id)
    destroyed, refused, skipped, kept = [], [], [], []

    for kind, role in (("team_box", None), ("jump", None), ("engine", None),
                       ("golden", GOLDEN_TAG), ("engine_template", "engine-template")):
        for row in document.resources(kind):
            name = row["name"]
            if kind in ("golden", "engine_template") and not full:
                skipped.append(dict(row))
                log(f"  kept {kind} {name} (--teams-only: templates stay)")
                continue
            if kind in ("golden", "engine_template"):
                info = compute.image(creds, name)
                if info is None:
                    document.forget(kind, name=name)
                    continue
                if not naming.owned(info, comp, run_id, role or naming._ALL):
                    refused.append({"entry": dict(row),
                                    "reason": f"{name} is not this run's "
                                              f"(labels {info.get('labels')})"})
                    continue
                image_ops.delete_image(creds, name)
            else:
                info = compute.instance(creds, name)
                if info is None:
                    document.forget(kind, name=name)
                    continue
                if not naming.owned(info, comp, run_id):
                    refused.append({"entry": dict(row),
                                    "reason": f"{name} is not this run's "
                                              f"(labels {info.get('labels')})"})
                    continue
                instance_ops.delete(creds, name)
            document.forget(kind, name=name)
            destroyed.append(dict(row))
            log(f"  destroyed {kind} {name}")

    # restore points first-refusal-free: machine images can only be this run's names
    for mark in [m for m in image_ops.list_marks(creds, ownership_labels(comp, run_id))]:
        image_ops.delete_machine_image(creds, mark)
        log(f"  destroyed machine image {mark}")

    if full:
        for row in document.resources("network"):
            if network_ops.delete_network(creds, row["name"], log=log):
                destroyed.append(dict(row))
                document.forget("network", name=row["name"])
        for row in document.resources("subnet"):
            document.forget("subnet", name=row["name"])
    document.save()
    return {"destroyed": destroyed, "skipped": skipped, "refused": refused, "kept": kept}


def sweep_tagged_leftovers(nodes, ownership, keep_templates=False):
    """The label-scope net: instances carrying the FULL ownership set a crash left
    unrecorded. A kept template's role label stays out of this sweep."""
    creds = compute.session()
    ours = [i for i in compute.instances(creds)
            if naming.owned(i, ownership.comp, ownership.run_id)]
    if not ours:
        return []
    print(f"  Leftover sweep: {len(ours)} instance(s) carrying this run's labels")
    for info in ours:
        if keep_templates and (info.get("labels") or {}).get(naming.LABEL_ROLE):
            continue
        instance_ops.delete(creds, info["name"])
        print(f"    swept {info['name']}")
    return []


def pre_stop_boxes(teams, boxes, default_node, team_nodes=None, ownership=None):
    # The delete stops the instance itself; the PVE ACPI-hang hazard has no analog.
    return None


# ---- red01 ----

def red01_deploy(comp_dir, **kwargs):
    from red import red01_ops
    from red.tlaloc_ops import red_disk_gb
    from estate import config

    own = kwargs.get("ownership")
    if own is None:
        raise RuntimeError("red01_deploy needs the run's Ownership")
    creds = _creds()
    comp = utils.competition_name(comp_dir)
    prefix = naming.prefix(comp, own.run_id)
    unit = red01_ops.red01_vmid_for(comp_dir, kwargs.get("engine_vmid"),
                                    kwargs.get("placement"))
    name = naming.instance_name(prefix, "tlaloc-red01")
    info = compute.instance(creds, name)
    if info is not None:
        if not naming.owned(info, comp, own.run_id):
            raise RuntimeError(f"red01 slot {name} is occupied by a machine that is not "
                               f"this run's (labels {info.get('labels')}) — a deploy never "
                               f"reconfigures or destroys another run's range")
        print(f"  red01      : adopting {name} (this run's)")
    else:
        pub = _pub_of(kwargs.get("ssh_key"))
        instance_ops.create(
            creds, name,
            image=image_ops.resolve_base(creds, "red01-base"),
            machine=instance_ops.machine_type(4, 8192),
            nics=[instance_ops.nic(naming.subnet_name(prefix, "mgmt"), MGMT_RED01_IP,
                                   external="EPHEMERAL"),
                  instance_ops.nic(naming.subnet_name(prefix, "red"), RED_SEGMENT_IP)],
            labels=ownership_labels(comp, own.run_id, "red01", unit=unit),
            disk_gb=int(red_disk_gb(comp_dir) or 60),
            network_tags=(RED01_TAG,),
            meta=instance_ops.metadata(red01_ops.RED_USER, pub,
                                       _red01_user_data(pub)))
        print(f"  red01      : instance {name} (mgmt via public NAT, red {RED_SEGMENT_IP})")
    primary = instance_ops.primary_nic_info(creds, name)
    if not primary["public"]:
        raise RuntimeError("red01's external IP is missing — its mgmt NIC create failed")
    red01_ops.record_identity(comp_dir, kwargs.get("placement"), vmid=unit,
                              ip=primary["public"], node=compute.zone())
    return {"ok": True, "vmid": unit, "ip": primary["public"], "node": compute.zone(),
            "segment_ip": RED_SEGMENT_IP, "segment_subnet": RED_SEGMENT_SUBNET}


def _pub_of(key):
    pub = Path(str(key) + ".pub")
    if not pub.is_file():
        raise RuntimeError(f"the SSH public key {pub} is missing — red01 cannot be built "
                           f"without the key that reaches it")
    return pub.read_text().strip()


def _red01_user_data(pub):
    # cloud-init owns identity; the static red-segment ROUTE rides the same file so it
    # survives reboots exactly like the proxmox netplan (the pinned NICs need no more).
    return (
        "#cloud-config\n"
        "users:\n"
        "  - name: sysadmin\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {pub}\n"
        "write_files:\n"
        "  - path: /etc/netplan/51-tlaloc-red.yaml\n"
        "    permissions: '0600'\n"
        "    content: |\n"
        "      network:\n"
        "        version: 2\n"
        "        ethernets:\n"
        "          ens5:\n"
        "            routes:\n"
        f"              - to: 192.168.0.0/16\n                via: {RED_SEGMENT_GW}\n"
    )


def red01_destroy(comp_dir, **kwargs):
    from red import red01_ops
    from estate import state as estate_state

    own = kwargs.get("ownership")
    creds = _creds()
    comp = utils.competition_name(comp_dir)
    unit = red01_ops.recorded_identity(comp_dir).get("vmid")
    if unit and own:
        mine = [i for i in compute.instances(creds)
                if i["name"].endswith("tlaloc-red01")
                and naming.owned(i, comp, own.run_id)]
        if len(mine) > 1:
            raise RuntimeError("more than one red01 carries this run's labels — refusing")
        if mine:
            instance_ops.delete(creds, mine[0]["name"])
            print(f"  red01      : destroyed {mine[0]['name']}")
    removed = {"vm": bool(unit), "rules": False}
    if kwargs.get("engine_ip") and kwargs.get("ssh_key") and kwargs.get("engine_user"):
        removed["rules"] = red01_ops.remove_engine_rules(
            kwargs["engine_ip"], kwargs["ssh_key"], engine_user=kwargs["engine_user"]
        ).get("ok", False)
    document = estate_state.EstateState.load(comp_dir)
    document.data.pop("red", None)
    document.save()
    return removed


def red01_occupant_verdict(vmid, comp, run_id=None):
    creds = _creds()
    mine = [i for i in compute.instances(creds)
            if i["name"].endswith("tlaloc-red01") and naming.owned(i, comp, run_id)]
    return "ours" if mine else "absent"
