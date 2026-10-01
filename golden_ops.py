"""Golden-set build and template conversion (M3.1).

Plant once per box type on a "golden" VM, convert it to a template, then let Terraform
apply #2 link-clone every team from those templates. The heavy work (package installs,
services, non-disruptive misconfigs) lands once and rides the clone as bytes on disk.

Why the API and not Terraform: `qm template` conversion happens outside the VM resource's
lifecycle, so a Terraform-managed golden box would drift the moment it converts. This
module mirrors clone_ops' API path (clone, config, start, wait); Terraform only consumes
the finished templates as `clone { full = false }` sources via the golden_template_ids
tfvars map that deploy() persists.

Identity note: linked clones of a BOOTED golden share its machine SID across teams.
Harmless for members, but a new forest takes its domain SID from the promoted DC's
machine SID, so DCs cloned from one booted golden gave every team the same DomainSID
(winad-testrun 2026-09-25). Domain-controller box types therefore get an UNBOOTED
golden: a full clone of the sysprepped base converted straight to a template, so each
team's clone runs specialize itself (fresh SID). Their planted configs move to the
per-team repair stage (nakon_ops.generate_stage_configs), which still runs pre-domain.
"""

import json
import os
import re
import shlex
import time

from constants import GOLDEN_CLONE_TIMEOUT, GOLDEN_TAG, SNAP_BASE
from engine_ops import ensure_nat_forwarding
from hardening_ops import ensure_alpine_services, fix_dns_on_boxes, prep_apt_on_boxes, setup_ubuntu_auth
from nakon_ops import build_nakon_bundle, run_nakon
from range_ops import (
    clone_marker,
    cluster_vms_for,
    gc_orphan_volumes,
    delete_snapshot,
    destroy_vm_if_exists,
    list_snapshots,
    proxmox_api,
    rollback_snapshot,
    start_vm,
    stop_vm,
    take_snapshot,
    wait_for_proxmox_task,
)
from nodes_ops import golden_vmid_for_slot
from ssh_ops import ssh_via_gateway, wait_for_boxes_ssh, wait_for_cloud_init
from template_ops import stored_template_hash, write_template_hash
from utils import compfile_flag, is_unmanaged, run_concurrent
from windows_ops import bootstrap_windows_box, is_windows_template


def _quote_sshkeys(public_key):
    """Proxmox's sshkeys config param wants the key URL-encoded (as redeploy does)."""
    from urllib.parse import quote
    return quote(public_key.strip(), safe="")


def golden_vmid_for(engine_vmid, box_idx):
    """Golden boxes sit just above the engine's slot; preflight gates the span for
    collisions with team vmid space (engine vmids above ~1060 shift the golden block).
    Slot 0 of golden_vmid_for_slot — the satellite slots shift by box-stride (nodes_ops)."""
    return golden_vmid_for_slot(engine_vmid, 0, box_idx)


def golden_ip_for(anchor_identifier, box_idx):
    """Above the .1 gateway, below .255, on the anchor team's subnet — free because
    golden boxes are converted to (stopped) templates before any real team box
    exists. Satellite slots anchor on their first local team (the jump answers .1
    there and routes to the engine)."""
    return f"192.168.{anchor_identifier}.{240 + box_idx}"


def _is_template(node, vmid):
    try:
        return bool(proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("template"))
    except Exception:
        return False


def _vm_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    return any(v["vmid"] == vmid for v in vms)


def _template_vmid_map(node):
    """name -> vmid for stopped VMs tagged `template`, ON `node` (mirrors main.tf's
    per-slot template data sources). Asks the host that owns `node`
    (range_ops.cluster_vms_for): with a multi-node placement that is the satellite's
    own endpoint — a primary-side cluster query would never see its templates on
    independent hosts. /cluster/resources joins tags with ';' (config GET uses ','),
    so split on both."""
    vms = cluster_vms_for(node)
    out = {}
    for vm in vms:
        if vm.get("node") and vm["node"] != node:
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if "template" in tags and vm.get("status") == "stopped":
            out[vm["name"]] = vm["vmid"]
    return out


def unbooted_golden_boxes(comp_dir):
    """Box types whose golden must stay sysprep-generalized (never booted): the
    domain-controller role in domain_roles.json. See the module identity note.

    An absent file is a no-domain lineup (empty set). A PRESENT but malformed or
    invalid file raises: treating it as no-domain would hand the DC a booted shared
    golden — the exact duplicate-DomainSID failure this split exists to prevent."""
    path = comp_dir / "domain_roles.json"
    if not path.exists():
        return set()
    try:
        roles = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"  ERROR: {path} is unreadable/malformed ({str(e)[:80]}) — "
                         "cannot decide which goldens must stay unbooted")
    if not isinstance(roles, dict) or not all(
            isinstance(name, str) and isinstance(role, str)
            for name, role in roles.items()):
        raise SystemExit(
            f"  ERROR: {path} must map box names to 'dc' or 'member' strings")
    bad = {name: role for name, role in roles.items() if role not in ("dc", "member")}
    if bad:
        raise SystemExit(
            f"  ERROR: {path} has invalid role value(s): "
            + ", ".join(f"{name}={role!r}" for name, role in sorted(bad.items()))
            + " (expected 'dc' or 'member')")
    boxes_path = comp_dir / "boxes.json"
    if boxes_path.exists():
        names = {b["name"] for b in json.loads(boxes_path.read_text())}
        unknown = [name for name in roles if name not in names]
        if unknown:
            raise SystemExit(
                f"  ERROR: {path} names box(es) absent from boxes.json: "
                + ", ".join(sorted(unknown)))
    return {name for name, role in roles.items() if role == "dc"}


def golden_targets(engine_vmid, teams, boxes, slot=0, anchor_identifier=None):
    """One target per box type: vmid/IP/name pre-derived, box_idx positional (mirrors
    enumerate_targets' invariant — build from the full box list, never a filtered one).

    slot 0 anchors on team1's subnet (historical behavior exactly); a satellite slot
    anchors on its first local team's subnet — the satellite's goldens must sit on a
    bridge that exists on the satellite's host, with the jump answering .1 there."""
    if anchor_identifier is None:
        anchor_identifier = str(teams["team1"]["identifier"])
    return [
        {
            "box": box,
            "box_idx": box_idx,
            "vmid": golden_vmid_for_slot(engine_vmid, slot, box_idx),
            "ip": golden_ip_for(anchor_identifier, box_idx),
            "vm_name": f"golden-{box['name']}",
            "machine": f"{box['name']}-golden",
            "gateway": f"192.168.{anchor_identifier}.1",
            "bridge": f"vmbr{anchor_identifier}",
        }
        for box_idx, box in enumerate(boxes)
        if not is_unmanaged(box)  # no golden for firewall/appliance boxes; box_idx stays positional
    ]


def build_golden_set(node, teams, boxes, ctx, comp_dir, engine_vmid, box_password,
                     golden_config_path, key, scoring_user, scoring_ip, jobs=1,
                     golden_hashes=None, unbooted=frozenset(),
                     slot=0, anchor_identifier=None):
    """Clone, plant (strict), and convert the golden set. Returns {box_name: vmid}.

    Resume routing: every golden vmid already a template -> skip (the build is done);
    golden vmids exist as plain VMs -> a previous attempt died mid-build, so roll back
    the ones tz-base marks as planted and re-plant; missing ones are cloned fresh.
    A PARTIALLY converted set is fine when every converted vmid's description hash
    matches its expected hash (M4 selective rebuild / added box type); without
    expected hashes, the historical no-clean-resume refusal stands."""
    targets = golden_targets(engine_vmid, teams, boxes, slot=slot,
                             anchor_identifier=anchor_identifier)
    ownership_tags = f"tezcatlipoca,{GOLDEN_TAG},comp-{comp_dir.name}"
    templates = _template_vmid_map(node)

    missing = [t for t in targets if not _vm_exists(node, t["vmid"])]
    planted = [t for t in targets
               if _vm_exists(node, t["vmid"]) and not _is_template(node, t["vmid"])]
    converted = [t for t in targets if _is_template(node, t["vmid"])]
    if converted and len(converted) < len(targets):
        if golden_hashes is not None:
            mismatched = [t["box"]["name"] for t in converted
                          if stored_template_hash(node, t["vmid"])
                          != golden_hashes.get(t["box"]["name"])]
            if mismatched:
                raise RuntimeError(
                    "golden set is partially converted and these converted golden(s) do "
                    f"not match their expected hash: {', '.join(mismatched)} — destroy the "
                    "range and redeploy; a half-converted golden set has no clean resume "
                    "(templates can't be un-templated).")
        else:
            raise RuntimeError(
                "golden set is partially converted — destroy the range and redeploy; a "
                "half-converted golden set has no clean resume (templates can't be "
                "un-templated).")
    if len(converted) == len(targets):
        print(f"  Golden set already converted ({len(converted)} template(s)) — skipping "
              f"build (resume; M4 hash gate passed or rebuild already handled upstream)")
        return {t["box"]["name"]: t["vmid"] for t in targets}

    # Only unconverted slots are worked on: a selective rebuild (one box type's hash
    # changed) must not start, plant, or re-convert the matching templates it keeps.
    done = {t["vmid"] for t in converted}
    cold = [t for t in targets if t["vmid"] not in done and t["box"]["name"] in unbooted]
    booted = [t for t in targets if t["vmid"] not in done and t["box"]["name"] not in unbooted]

    for t in cold:
        # Any plain VM here is a dead attempt that may have booted (and specialized) —
        # never reuse it; the whole point of this slot is a generalized disk.
        # Adoption exception (live-found 2026-09-30, same shape as the engine-template
        # leftover): a clone task that outlived its 1800s task-wait completes AFTER
        # the driver raised, leaving the VM with only the BASE image's inherited tags
        # — the strict guard would refuse it forever. A not-yet-converted VM on the
        # reserved slot carrying our clone-marker description is ours; adopt it loudly.
        try:
            cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{t['vmid']}/config")["data"]
            desc = str(cfg.get("description") or "")
            raw_tags = str(cfg.get("tags") or "")
            tags = {x.strip() for x in raw_tags.replace(";", ",").split(",") if x.strip()}
            if (cfg.get("name") == t["vm_name"] and clone_marker(comp_dir.name) in desc
                    and "tezcatlipoca" not in tags):
                print(f"    vmid {t['vmid']}: untagged dead-attempt clone of "
                      f"'{t['vm_name']}' (our clone marker in description) — adopting")
                destroy_vm_if_exists(node, t["vmid"], expect_tags=None)
        except Exception:
            pass  # slot empty, or unreadable — the strict destroy below decides
        destroy_vm_if_exists(node, t["vmid"], expect_tags={"tezcatlipoca", f"comp-{comp_dir.name}"},
                             legacy_name=t["vm_name"])
        src = templates.get(t["box"]["template"])
        if src is None:
            raise RuntimeError(f"no stopped template named '{t['box']['template']}' on the node")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone", data={
            "newid": t["vmid"], "name": t["vm_name"], "full": 1,
            "description": clone_marker(comp_dir.name)})["data"]
        wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={
            "net0": f"virtio,bridge={t['bridge']}", "tags": ownership_tags})
        proxmox_api("POST", f"/nodes/{node}/qemu/{t['vmid']}/template")
        if golden_hashes:
            write_template_hash(node, t["vmid"], golden_hashes[t["box"]["name"]],
                                extra=f"box={t['box']['name']} unbooted")
        print(f"    {t['vm_name']} (vmid {t['vmid']}) is now an UNBOOTED template "
              f"(generalized — each team's clone specializes its own SID)")
    if not booted:
        return {t["box"]["name"]: t["vmid"] for t in targets}
    missing = [t for t in missing if t in booted]
    planted = [t for t in planted if t in booted]
    targets_all, targets = targets, booted

    if missing:
        print(f"  Cloning {len(missing)} golden box(es) from base templates...")
        existing_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
        for t in missing:
            src = templates.get(t["box"]["template"])
            if src is None:
                raise RuntimeError(f"no stopped template named '{t['box']['template']}' on the node")
            if t["vmid"] in existing_vmids:
                if not proxmox_api("GET", f"/nodes/{node}/qemu/{t['vmid']}/config")["data"].get("lock"):
                    continue
                # An interrupted clone: half-copied disk, never usable — start over.
                destroy_vm_if_exists(node, t["vmid"], expect_tags=set())
            else:
                gc_orphan_volumes(node, t["vmid"])
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone", data={
                "newid": t["vmid"],
                "name": t["vm_name"],
                "full": 1,
                "description": clone_marker(comp_dir.name),
            })["data"]
            wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
            proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={
                "ipconfig0": f"ip={t['ip']}/24,gw={t['gateway']}",
                "net0": f"virtio,bridge={t['bridge']}",
                "tags": ownership_tags,
                # cloud-init identity for the golden box's first boot (the base template
                # bakes no password — terraform's user_account normally supplies it):
                "ciuser": ctx.get("box_username", "ubuntu"),
                "cipassword": box_password,
                "sshkeys": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"]),
            })
            ensure_golden_disk_size(node, t["vmid"], t["box"].get("disk_gb"))
            print(f"    {t['box']['template']} -> {t['vm_name']} (vmid {t['vmid']})")

    rolls = [t for t in planted if SNAP_BASE in list_snapshots(node, t["vmid"])]
    for t in rolls:
        print(f"  Golden re-entry: rolling {t['vm_name']} back to '{SNAP_BASE}' before re-planting...")
        rollback_snapshot(node, t["vmid"], SNAP_BASE)

    print("  Starting golden boxes...")
    for t in targets:
        start_vm(node, t["vmid"])

    linux_targets = [t for t in targets if not is_windows_template(t["box"]["template"])]
    windows_targets = [t for t in targets if is_windows_template(t["box"]["template"])]

    def _boot_win(t):
        # 1800s: a fresh sysprep-specialize first boot can exceed the 900s default
        # outright on a loaded node (live-found 2026-09-30: two consecutive golden
        # rebuilds died at exactly 900s with a second deploy saturating the host).
        bootstrap_windows_box(node, t["vmid"], t["ip"], t["gateway"], "8.8.8.8", box_password,
                              timeout=1800)

    win_results = run_concurrent(windows_targets, _boot_win, max_workers=4)
    for t, r in zip(windows_targets, win_results):
        if isinstance(r, Exception):
            raise r

    wait_for_boxes_ssh(ctx, targets, timeout=300)
    wait_for_cloud_init(ctx, targets, timeout=240)
    setup_ubuntu_auth(linux_targets, ctx)
    if linux_targets:
        print("  Expanding guest root filesystems (LVM layouts need pvresize+lvextend)...")
        expand_guest_root_disks(linux_targets, ctx)

    # Nakon authenticates to the golden boxes by PASSWORD, and an API clone of a base
    # template has no password set (terraform's user_account normally supplies it at
    # team-box creation). Reset it over the key-auth SSH channel before the plant.
    box_username = ctx.get("box_username", "ubuntu")
    print(f"  Setting box passwords on golden Linux boxes (nakon authenticates as {box_username})...")
    for t in linux_targets:
        r = ssh_via_gateway(ctx, t["ip"], f"echo '{box_username}:{box_password}' | sudo chpasswd",
                            timeout=30, user=box_username)
        if r.returncode != 0:
            raise RuntimeError(f"password reset failed on golden {t['ip']}: "
                               f"{(r.stderr or '').strip()[:120]}")

    fix_dns_on_boxes(linux_targets, ctx)

    print("  Prepping apt on golden boxes (pre-plant)...")
    prep_apt_on_boxes(linux_targets, ctx)

    print(f"  Snapshotting golden boxes as '{SNAP_BASE}' (pre-plant rollback point)...")
    for t in targets:
        take_snapshot(node, t["vmid"], SNAP_BASE,
                      description="tezcatlipoca golden: booted, networked, pre-plant")

    ensure_nat_forwarding(ctx)

    # With the alpine_services knob, catalog service steps are EXPECTED to fail on
    # Alpine (vulndb scripts are apt/dnf/yum-only) — the plant runs non-strict and
    # ensure_alpine_services owns those services, with its own hard pass/fail.
    alpine_shim = compfile_flag(comp_dir / "Compfile", "alpine_services")
    # Score-only lineups carry no plantable golden configurations — the golden is a
    # pristine disk by design. An empty plan makes nakon report "no output from the
    # remote plan" per machine, so the plant is skipped outright (live-found
    # 2026-09-30 multinode-spread).
    golden_cfg = json.loads(golden_config_path.read_text())["machines"]
    if not any(m.get("configurations") for m in golden_cfg):
        print("  Golden stage carries no plantable configurations — skipping the "
              "nakon plant; converting pristine goldens")
    else:
        bundle = build_nakon_bundle(golden_config_path)
        result = run_nakon(key, scoring_user, scoring_ip, bundle, golden_config_path,
                           only=[t["machine"] for t in targets],
                           timeout=max(2400, 2400 * len(targets)), strict=not alpine_shim,
                           jobs=jobs, run_tag="golden")
        if result.failed:
            if alpine_shim:
                print(f"  WARNING: golden plant had {len(result.failed)} FAILED step(s) — "
                      f"alpine_services shim owns those (apk/OpenRC)")
            else:
                raise RuntimeError(f"golden plant had {len(result.failed)} FAILED step(s) — strict mode requires green")
        if alpine_shim:
            ensure_alpine_services(comp_dir, targets, ctx)

    print("  Cleaning cloud-init state on golden Linux boxes (clones must re-init)...")
    for t in linux_targets:
        result = ssh_via_gateway(ctx, t["ip"], "sudo cloud-init clean --logs --machine-id",
                                 timeout=30, user=ctx.get("box_username", "ubuntu"))
        if result.returncode != 0:
            print(f"    WARNING: cloud-init clean rc={result.returncode} on {t['ip']} "
                  f"— clones may inherit golden's machine-id")

    print("  Stopping golden boxes and converting to templates...")
    for t in targets:
        stop_vm(node, t["vmid"])
        # qm template refuses a VM holding snapshots — and the tz-base rollback guard's
        # purpose ends with the plant: a converted set is fatal on re-entry by design.
        if SNAP_BASE in list_snapshots(node, t["vmid"]):
            delete_snapshot(node, t["vmid"], SNAP_BASE)
        proxmox_api("POST", f"/nodes/{node}/qemu/{t['vmid']}/template")["data"]
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={"tags": ownership_tags})
        # M4: the hash lands on the template (description) AND in the comp's state —
        # reuse/rebuild decisions read it back at the next deploy's hash gate.
        if golden_hashes:
            write_template_hash(node, t["vmid"], golden_hashes[t["box"]["name"]],
                                extra=f"box={t['box']['name']}")
        print(f"    {t['vm_name']} (vmid {t['vmid']}) is now a template")

    return {t["box"]["name"]: t["vmid"] for t in targets_all}


_SIZE_TO_GB = {"": 1 / 1024 ** 3, "K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}


def _root_disk_gb(cfg, vmid):
    """(config key, size in GB) of the clone's root disk, or (None, 0).

    The bus varies per base template (scsi0 on the -fix images); cdrom and
    cloud-init entries are not disks. Parsed from the config because the
    clone inherits the template's size verbatim — there is no other record
    of it (svc-matrix: the 15 GB ubuntu template disk filled mid-plant on
    splunk's .deb unpack while team clones got their terraform disk_gb)."""
    for key in sorted(k for k in cfg
                      if k.startswith(("scsi", "virtio", "sata", "ide"))):
        val = cfg[key]
        if "media=cdrom" in val or "cloudinit" in val or "size=" not in val:
            continue
        m = re.search(r"size=(\d+(?:\.\d+)?)([KMGT]?)", val)
        if m:
            return key, int(float(m.group(1)) * _SIZE_TO_GB[m.group(2)])
    return None, 0


def ensure_golden_disk_size(node, vmid, disk_gb):
    """Grow the golden clone's root disk to the box's disk_gb BEFORE first boot.

    Golden clones inherit the base template's disk verbatim (terraform only
    sizes the team clones), so a big plant payload (splunk) can fill the
    template-sized disk mid-golden-build. Grow-only — a shrink would destroy
    data — and before start_vm. Guest-side expansion is a separate post-boot
    step (expand_guest_root_disks): cloud-init only grows a plain partition+fs,
    so LVM layouts need growpart + pvresize + lvextend + resize2fs."""
    if not disk_gb:
        return
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    key, cur_gb = _root_disk_gb(cfg, vmid)
    if key is None:
        print(f"    WARNING: golden {vmid} has no root disk — disk_gb={disk_gb} not applied")
        return
    if disk_gb <= cur_gb:
        return
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/resize",
                data={"disk": key, "size": f"{disk_gb}G"})
    print(f"    golden {vmid}: {key} grown {cur_gb}G -> {disk_gb}G")


def expand_guest_root_disks(targets, ctx):
    """Guest-side companion to ensure_golden_disk_size: cloud-init only expands a
    PLAIN partition+fs on first boot — LVM layouts (ubuntu cloud images) keep their
    original root LV, and btrfs layouts (default on Fedora cloud images — live-found
    2026-09-29: findmnt reports '/dev/sda3[/root]', whose bracketed subvolume suffix
    broke the partition-digit parse AND resize2fs can't grow btrfs) need their own
    grow. growpart + pvresize + lvextend / btrfs resize / xfs_growfs / resize2fs
    after first boot; every step no-ops when there is nothing to grow, and boxes
    without growpart (Alpine) are skipped. Windows goldens are out of scope (they
    boot at their template's own size)."""
    script = (
        "command -v growpart >/dev/null || { echo 'growpart unavailable - skipping'; exit 0; }; "
        "set -e; "
        # base-ubuntu24.04-fix golden clones can run MINUTES with the LV mounted but
        # no /dev/dm-N node (udev late under boot load — live-found 2026-09-29:
        # resize2fs "No such file or directory" on the live root LV at T+40s, node
        # present at T+16min). dmsetup mknodes materializes the node immediately.
        "command -v dmsetup >/dev/null 2>&1 && dmsetup mknodes 2>/dev/null || true; "
        "ROOT_SRC=$(findmnt -no SOURCE /); "
        "SRC=${ROOT_SRC%%\\[*}; "  # strip a btrfs subvolume suffix: /dev/sda3[/root] -> /dev/sda3
        "case \"$(stat -f -c %T /)\" in "
        "btrfs) GROW_FS=\"btrfs filesystem resize max /\";; "
        "xfs) GROW_FS=\"xfs_growfs /\";; "
        "*) GROW_FS=\"resize2fs \\\"$SRC\\\"\";; "
        "esac; "
        "case \"$SRC\" in "
        "/dev/mapper/*|/dev/dm-*) "
        "PV=$(pvs --noheadings -o pv_name | tr -d ' ' | head -1); "
        "DISK=$(basename \"$PV\" | sed 's/[0-9]*$//'); "
        "PART=$(basename \"$PV\" | grep -o '[0-9]*$'); "
        "if [ -n \"$DISK\" ] && [ -n \"$PART\" ]; then growpart \"/dev/$DISK\" \"$PART\" || true; fi; "
        "pvresize \"$PV\" || true; "
        "LV=$(lvs --noheadings -o lv_path | tr -d ' ' | head -1); "
        "lvextend -l +100%FREE \"$LV\" || true; "
        ";; "
        "/dev/*) "
        "DISK=$(basename \"$SRC\" | sed 's/[0-9]*$//'); "
        "PART=$(basename \"$SRC\" | grep -o '[0-9]*$'); "
        "if [ -n \"$DISK\" ] && [ -n \"$PART\" ]; then growpart \"/dev/$DISK\" \"$PART\" || true; fi; "
        ";; "
        "*) echo 'unrecognized root layout - skipping'; exit 0; ;; "
        "esac; "
        "$GROW_FS; "
        "df -h /"
    )
    for t in targets:
        # The grow itself is retried: a just-booted golden can transiently lack its
        # device-mapper node (live-found 2026-09-29: the ubuntu golden's /dev/dm-0
        # appeared seconds after resize2fs died with "No such file or directory").
        r = None
        for attempt in range(3):
            r = ssh_via_gateway(ctx, t["ip"], f"sudo -H sh -c {shlex.quote(script)}",
                                timeout=120, user=ctx.get("box_username", "ubuntu"))
            if r.returncode == 0:
                break
            print(f"    {t['ip']}: root-disk expansion attempt {attempt + 1} failed "
                  f"(rc={r.returncode}) — retrying")
            time.sleep(15)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            # Expansion is an ENOSPC guard, not a correctness gate. Fail ONLY when the
            # root fs is measurably too small for the plants (the regression-4x1 shape:
            # a 10G LV inside a 30G disk). When the size can't be measured — the same
            # boot-window instability that broke the grow usually breaks the probe too
            # (live-found 2026-09-29 x3: ssh to the fresh golden flaky for minutes,
            # every box healthy afterwards) — warn and continue; a genuinely undersized
            # root dies loudly at first big plant, named by the plant coverage gate.
            size_gb = 0.0
            try:
                probe = ssh_via_gateway(ctx, t["ip"], "df -BK / | awk 'NR==2{print $2}'",
                                        timeout=60, user=ctx.get("box_username", "ubuntu"))
                size_gb = int((probe.stdout or "0").strip().splitlines()[-1]) / 1024 ** 2
            except (ValueError, IndexError, Exception):
                size_gb = 0.0
            need_gb = max(int(t.get("disk_gb") or 10) - 4, 1)
            if size_gb and size_gb < need_gb:
                raise RuntimeError(f"root-disk too small on golden {t['ip']} "
                                   f"({size_gb:.0f}G < {need_gb}G) and expansion failed "
                                   f"(rc={r.returncode}): {(r.stderr or out).strip()[:200]}")
            note = f"{size_gb:.0f}G measured" if size_gb else "size unmeasurable"
            print(f"    {t['ip']}: WARNING expansion failed ({note}) — continuing; "
                  f"first big plant will surface a truly undersized root")
        df_lines = [l for l in out.splitlines() if l.startswith("/dev/")]
        print(f"    {t['ip']}: root-disk expansion done"
              + (f" ({df_lines[-1].split()[2] if len(df_lines[-1].split()) > 2 else 'used=?'} used)" if df_lines else ""))


def destroy_golden_set(node, engine_vmid, num_box_types, expect_tags=None, slot=0):
    """Tear the golden templates down. Destroy order: AFTER the linked clones (they
    depend on the template's base disk). Slot-aware: each node's copy of the golden
    set dies on its own host. A slot held by a FOREIGN VM (ownership guard refusal)
    is skipped with a loud warning — one squatted vmid must not strand the rest of
    the teardown (loadtest-2026-09-30: a satellite template squatting golden slot
    1333 aborted the whole pass, leaving everything else half-destroyed)."""
    skipped = []
    for box_idx in range(num_box_types):
        try:
            destroy_vm_if_exists(node, golden_vmid_for_slot(engine_vmid, slot, box_idx),
                                 expect_tags=expect_tags)
        except RuntimeError as e:
            skipped.append(str(e))
            print(f"    WARNING: golden slot {box_idx} is FOREIGN — skipping it and "
                  f"continuing: {e}")
    if skipped:
        print(f"  {len(skipped)} golden slot(s) skipped as foreign — they belong to "
              f"another deployment; inspect them manually.")
