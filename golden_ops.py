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

from constants import GOLDEN_TAG, SNAP_BASE
from engine_ops import ensure_nat_forwarding
from hardening_ops import fix_dns_on_boxes, prep_apt_on_boxes, setup_ubuntu_auth
from nakon_ops import build_nakon_bundle, run_nakon
from range_ops import (
    clone_marker,
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
from ssh_ops import ssh_via_gateway, wait_for_boxes_ssh, wait_for_cloud_init
from template_ops import stored_template_hash, write_template_hash
from utils import is_unmanaged, run_concurrent
from windows_ops import bootstrap_windows_box, is_windows_template


def _quote_sshkeys(public_key):
    """Proxmox's sshkeys config param wants the key URL-encoded (as redeploy does)."""
    from urllib.parse import quote
    return quote(public_key.strip(), safe="")


def golden_vmid_for(engine_vmid, box_idx):
    """Golden boxes sit just above the engine's slot; preflight gates the span for
    collisions with team vmid space (engine vmids above ~1060 shift the golden block)."""
    return int(engine_vmid) + 150 + box_idx


def golden_ip_for(team1_identifier, box_idx):
    """Above the .1 gateway, below .255, on team1's subnet — free because golden boxes
    are converted to (stopped) templates before any real team box exists."""
    return f"192.168.{team1_identifier}.{240 + box_idx}"


def _is_template(node, vmid):
    try:
        return bool(proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("template"))
    except Exception:
        return False


def _vm_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    return any(v["vmid"] == vmid for v in vms)


def _template_vmid_map(node):
    """name -> vmid for stopped VMs tagged `template` (mirrors main.tf's template_ids).
    /cluster/resources joins tags with ';' (config GET uses ','), so split on both."""
    vms = proxmox_api("GET", "/cluster/resources?type=vm")["data"]
    out = {}
    for vm in vms:
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


def golden_targets(engine_vmid, teams, boxes):
    """One target per box type: vmid/IP/name pre-derived, box_idx positional (mirrors
    enumerate_targets' invariant — build from the full box list, never a filtered one)."""
    team1_identifier = str(teams["team1"]["identifier"])
    return [
        {
            "box": box,
            "box_idx": box_idx,
            "vmid": golden_vmid_for(engine_vmid, box_idx),
            "ip": golden_ip_for(team1_identifier, box_idx),
            "vm_name": f"golden-{box['name']}",
            "machine": f"{box['name']}-golden",
            "gateway": f"192.168.{team1_identifier}.1",
            "bridge": f"vmbr{team1_identifier}",
        }
        for box_idx, box in enumerate(boxes)
        if not is_unmanaged(box)  # no golden for firewall/appliance boxes; box_idx stays positional
    ]


def build_golden_set(node, teams, boxes, ctx, comp_dir, engine_vmid, box_password,
                     golden_config_path, key, scoring_user, scoring_ip, jobs=1,
                     golden_hashes=None, unbooted=frozenset()):
    """Clone, plant (strict), and convert the golden set. Returns {box_name: vmid}.

    Resume routing: every golden vmid already a template -> skip (the build is done);
    golden vmids exist as plain VMs -> a previous attempt died mid-build, so roll back
    the ones tz-base marks as planted and re-plant; missing ones are cloned fresh.
    A PARTIALLY converted set is fine when every converted vmid's description hash
    matches its expected hash (M4 selective rebuild / added box type); without
    expected hashes, the historical no-clean-resume refusal stands."""
    targets = golden_targets(engine_vmid, teams, boxes)
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
        destroy_vm_if_exists(node, t["vmid"], expect_tags={"tezcatlipoca", f"comp-{comp_dir.name}"},
                             legacy_name=t["vm_name"])
        src = templates.get(t["box"]["template"])
        if src is None:
            raise RuntimeError(f"no stopped template named '{t['box']['template']}' on the node")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone", data={
            "newid": t["vmid"], "name": t["vm_name"], "full": 1,
            "description": clone_marker(comp_dir.name)})["data"]
        wait_for_proxmox_task(node, upid)
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
            wait_for_proxmox_task(node, upid)
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
        bootstrap_windows_box(node, t["vmid"], t["ip"], t["gateway"], "8.8.8.8", box_password)

    win_results = run_concurrent(windows_targets, _boot_win, max_workers=4)
    for t, r in zip(windows_targets, win_results):
        if isinstance(r, Exception):
            raise r

    wait_for_boxes_ssh(ctx, targets, timeout=300)
    wait_for_cloud_init(ctx, targets, timeout=240)
    setup_ubuntu_auth(linux_targets, ctx)

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

    bundle = build_nakon_bundle(golden_config_path)
    result = run_nakon(key, scoring_user, scoring_ip, bundle, golden_config_path,
                       only=[t["machine"] for t in targets],
                       timeout=max(2400, 2400 * len(targets)), strict=True,
                       jobs=jobs, run_tag="golden")
    if result.failed:
        raise RuntimeError(f"golden plant had {len(result.failed)} FAILED step(s) — strict mode requires green")

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


def destroy_golden_set(node, engine_vmid, num_box_types, expect_tags=None):
    """Tear the golden templates down. Destroy order: AFTER the linked clones (they
    depend on the template's base disk)."""
    for box_idx in range(num_box_types):
        destroy_vm_if_exists(node, golden_vmid_for(engine_vmid, box_idx),
                             expect_tags=expect_tags)
