"""M4: per-competition template lifecycle — hashes, the engine template, reuse/rebuild,
and freeze state.

Lifecycle (per competition, never a cross-competition cache):
    build -> test runs (reuse; rebuild on config change) -> freeze -> competition -> destroy

Every template belongs to exactly one competition: reused across that competition's test
runs, frozen for the event, destroyed at --full teardown. Templates are never patched in
place — every fix goes into the catalog/config and the templates rebuild from it.

Hashes cover ONLY what affects disk contents, and every input is tagged config-class vs
code-class. On a FROZEN competition the templates are never rebuilt: config-class drift
hard-fails (naming the changed fields), code-class drift warns and proceeds off the
frozen templates — a post-freeze log line must never break a mid-event rebuild."""

import hashlib
import inspect
import json
import os
import re
import subprocess
import time
from pathlib import Path

from constants import (
    ENGINE_TEMPLATE_NAME,
    ENGINE_TEMPLATE_VMID_OFFSET,
)
from range_ops import (
    destroy_vm_if_exists,
    proxmox_api,
    start_vm,
    stop_vm,
    wait_for_proxmox_task,
)

FROZEN_FILE = ".frozen.json"
HASHES_FILE = ".template-hashes.json"


def load_template_hashes(comp_dir):
    """The durable per-competition hash record. Lives outside .deploy_state.json on
    purpose: a fresh deploy resets state, but template reuse must survive it."""
    path = Path(comp_dir) / HASHES_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return {}


def save_template_hashes(comp_dir, **entries):
    """Merge entries (engine={hash, inputs}, golden={box: {hash, inputs}}) into the
    record. Atomic rename; 0600 — golden inputs embed box_password."""
    path = Path(comp_dir) / HASHES_FILE
    data = load_template_hashes(comp_dir)
    for key, value in entries.items():
        if key == "golden":
            data.setdefault("golden", {}).update(value)
        else:
            data[key] = value
    data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def bundle_id(bundle_path):
    """The content-addressed id of a built nakon bundle (its manifest's bundle_id)."""
    try:
        return json.loads((Path(bundle_path) / "manifest.json").read_text())["bundle_id"]
    except (OSError, ValueError, KeyError):
        return None


def engine_template_vmid(engine_vmid):
    """Just below the golden block (+150), so one preflight scan covers both."""
    return int(engine_vmid) + ENGINE_TEMPLATE_VMID_OFFSET


def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def code_hash(*functions):
    """sha256 over the sources of the given functions/strings — the code-class input."""
    parts = []
    for f in functions:
        parts.append(inspect.getsource(f) if callable(f) else str(f))
    return sha256_text("\n\x00\n".join(parts))


def _split_inputs(config_inputs, code_inputs):
    return {"config": config_inputs, "code": code_inputs}


def hash_from_inputs(inputs):
    return sha256_text(canonical_json(inputs))


def golden_payload_hash(bundle_path, box_name):
    """Per-box-type payload content hash from the bundle manifest: the plan for
    machine '{box}-golden', its steps' script_sha256s, sorted, canonical-hashed.

    NOT the whole-bundle id — the bundle covers every box type, so a web01 pin
    change would drag app01/dc01/win01's goldens into rebuild too (live-found
    matrix run 4: one config change rebuilt all four). Content-addressed blobs
    make the step shas a complete description of what actually lands on disk."""
    manifest = json.loads((Path(bundle_path) / "manifest.json").read_text())
    target = f"{box_name}-golden"
    for inv in manifest.get("inventory", []):
        if inv.get("name") == target:
            plan = (manifest.get("plans") or {}).get(inv.get("request_key")) or {}
            shas = sorted(s.get("script_sha256") or "" for s in plan.get("steps", []))
            return sha256_text(canonical_json(shas))
    return None


def golden_hash_inputs(box, golden_machine, payload_hash, box_password, box_username,
                       ssh_public_key, apt_cache):
    """The exact field list feeding a box type's golden hash.

    IN (config): base template vmid, the box's disk-size override, the golden-stage
    configuration list (names + pinned vars — pin changes rebuild the golden), the
    PER-BOX payload content hash (this box's plan's script sha256s), the box
    username/password (baked into /etc/shadow + cloud-init + credlist accounts),
    the operator SSH pubkey (cloud-init sshkeys), and the apt-proxy flag (95tz-proxy
    file). IN (code): golden_ops.build_golden_set and the apt-prep script. OUT,
    with reasons: team count/identifiers/IPs (per-team, applied after cloning),
    event.conf (engine-side), repair/final-stage configs (planted post-clone, never
    on the golden), snapshot names, node endpoint."""
    return _split_inputs(
        {
            "base_template_vmid": None,  # filled by caller (needs the node)
            "disk_gb": box.get("disk_gb"),
            "golden_configs": canonical_json(golden_machine["configurations"]),
            "payload_hash": payload_hash,
            "box_username": box_username,
            "box_password": box_password,
            "ssh_public_key": (ssh_public_key or "").strip(),
            "apt_cache": bool(apt_cache),
        },
        {"build_golden_set+apt_prep": None},  # filled by caller
    )


def tf_resource_block(tf_text, rtype, rname):
    """The text of one `resource "rtype" "rname" { ... }` block (brace-matched), or the
    whole file if it can't be found — hashing too much is safe, too little is not."""
    m = re.search(r'resource\s+"' + re.escape(rtype) + r'"\s+"' + re.escape(rname) + r'"\s*\{', tf_text)
    if not m:
        return tf_text
    depth, i = 0, m.end() - 1
    while i < len(tf_text):
        if tf_text[i] == "{":
            depth += 1
        elif tf_text[i] == "}":
            depth -= 1
            if depth == 0:
                return tf_text[m.start():i + 1]
        i += 1
    return tf_text


def _clean_func():
    from engine_ops import clean_engine_for_template
    return clean_engine_for_template


def engine_hash_inputs(base_engine_vm_id, quotient_ref, main_tf_text, bootstrap_func):
    """Engine hash fields. config: base image vmid + the pinned Quotient ref (the hash
    must be computable BEFORE building, so the ref is config, not a post-boot capture —
    the realized HEAD is recorded for traceability only). code: the bootstrap script and
    main.tf (the engine's CPU/memory/disk shape lives there; a shape change is code-class
    drift, warn-only on a frozen competition — documented residual risk). OUT: engine
    mgmt IP, event.conf, postgres/redis passwords (all applied per-deploy; the clean step
    removes them from the template), node endpoint (state's endpoint guard covers it)."""
    return _split_inputs(
        {
            "base_engine_vm_id": int(base_engine_vm_id),
            "quotient_ref": (quotient_ref or "").strip(),
        },
        {
            # The clean step shapes the template disk as much as bootstrap does.
            "bootstrap_scoring_engine": code_hash(bootstrap_func, _clean_func()),
            # Only the engine's own resource block shapes the engine disk; hashing the
            # whole file rebuilt the engine template on any team-box edit.
            "main.tf:scoring_engine": sha256_text(tf_resource_block(
                main_tf_text, "proxmox_virtual_environment_vm", "scoring_engine")),
        },
    )


# ---- template description: where a template's hash lives on the node ----

_DESC_RE = re.compile(r"tezcatlipoca-m4 hash=([0-9a-f]{64})")


def _set_description(node, vmid, text):
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config",
                data={"description": text})


def _get_description(node, vmid):
    try:
        return (proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
                .get("description") or "")
    except Exception:
        return ""


def stored_template_hash(node, vmid):
    m = _DESC_RE.search(_get_description(node, vmid))
    return m.group(1) if m else None


def write_template_hash(node, vmid, hash_value, extra=""):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _set_description(node, vmid, f"tezcatlipoca-m4 hash={hash_value} built={stamp}"
                                 + (f" {extra}" if extra else ""))


# ---- frozen state ----

def frozen_state(comp_dir):
    """The frozen record, or None. Never raises — a torn file reads as unfrozen only
    if unparseable, which the freeze/unfreeze tooling rewrites atomically."""
    path = Path(comp_dir) / FROZEN_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def classify_drift(stored_inputs, current_inputs):
    """{'config': [field...], 'code': [field...]} — the input classes that changed."""
    drift = {"config": [], "code": []}
    for cls in ("config", "code"):
        old = (stored_inputs or {}).get(cls) or {}
        new = (current_inputs or {}).get(cls) or {}
        for field in sorted(set(old) | set(new)):
            if canonical_json(old.get(field)) != canonical_json(new.get(field)):
                drift[cls].append(field)
    return drift


def golden_freeze_gate(name, stored_inputs, current_inputs, frozen_at, golden_bundle):
    """One golden's frozen-record comparison, before phase 1 destroys anything.

    Frozen records written before the per-box payload-hash migration carried the
    whole-bundle bundle_id. Preserve such a record only when the bundle itself is
    unchanged; a changed bundle still enters the normal hard-fail path. Config-class
    drift raises (naming the fields — nothing was destroyed); code-class drift warns
    and proceeds off the frozen template. Returns the drift classification so
    callers can act on code-only drift (phase 1 keeps the frozen golden)."""
    stored_config = (stored_inputs or {}).get("config") or {}
    current_config = (current_inputs or {}).get("config") or {}
    if (stored_config.get("bundle_id")
            and "payload_hash" not in stored_config
            and stored_config.get("bundle_id") == bundle_id(golden_bundle)):
        stored_inputs = {**stored_inputs,
                         "config": {k: v for k, v in stored_config.items()
                                     if k != "bundle_id"}}
        current_inputs = {**current_inputs,
                          "config": {k: v for k, v in current_config.items()
                                      if k != "payload_hash"}}
    drift = classify_drift(stored_inputs, current_inputs)
    if drift["config"]:
        raise SystemExit(
            f"  ERROR: golden template for '{name}' changed since FREEZE "
            f"({frozen_at}): {', '.join(drift['config'])}. A frozen "
            f"competition runs on the hashes that were verified — nothing was "
            f"destroyed. Unfreeze explicitly "
            f"(verify-competition.py --unfreeze --confirm-unfreeze) only BEFORE "
            f"the competition starts.")
    if drift["code"]:
        print(f"  WARNING: '{name}' golden code drifted since freeze "
              f"({', '.join(drift['code'])}) — proceeding on the frozen template.")
    return drift


def frozen_gate(comp_dir, stored_inputs, current_inputs, what):
    """Enforce frozen semantics for USING a template whose inputs drifted.

    Frozen: config-class drift hard-fails naming the fields (the event must run on the
    hashes that were verified); code-class drift warns and proceeds off the frozen
    template. Not frozen: rebuild is allowed — returns True ('rebuild permitted')."""
    drift = classify_drift(stored_inputs, current_inputs)
    if not drift["config"] and not drift["code"]:
        return False
    frozen = frozen_state(comp_dir)
    if frozen is None:
        return True
    if drift["config"]:
        raise SystemExit(
            f"  ERROR: {what} config changed since FREEZE ({frozen.get('frozen_at')}): "
            f"{', '.join(drift['config'])}. A frozen competition runs on the hashes that "
            f"were verified — rebuild is refused and nothing was destroyed. Unfreeze "
            f"explicitly (verify-competition.py --unfreeze --confirm-unfreeze) only "
            f"BEFORE the competition starts.")
    print(f"  WARNING: {what} code drifted since freeze ({', '.join(drift['code'])}) — "
          f"proceeding on the frozen template. Config inputs are unchanged.")
    return False


# ---- engine template ----

def find_engine_template(node, engine_vmid):
    """vmid of this competition's engine template, or None. Identified by the reserved
    vmid; ownership-tagged like everything else we create."""
    vmid = engine_template_vmid(engine_vmid)
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return None
    return vmid if cfg.get("template") else None


def build_engine_template(node, comp_dir, engine_vmid, base_engine_vm_id, ctx,
                          postgres_password, redis_password, quotient_ref,
                          hash_value, inputs):
    """Build the per-competition engine template (API-driven, mirroring golden_ops —
    terraform only consumes the finished template as apply #1's clone source).

    Template point: after bootstrap (packages, Docker, Quotient images, apt-cacher-ng)
    and BEFORE any competition state exists. Before conversion: containers stopped and
    every data volume removed (no scoring DB/teams/injects baked in), /opt/quotient/.env
    and host keys removed, /etc/machine-id truncated and cloud-init cleaned — each clone
    gets a fresh identity and fresh host keys. The apt-cacher-ng cache on the template is
    EMPTY (nothing has used the cacher at build time); the cache that speeds prep_apt is
    built on the linked-clone engine during a deploy and dies with it."""
    from engine_ops import bootstrap_scoring_engine, clean_engine_for_template
    from ssh_ops import forget_engine_host_key, wait_for_ssh

    vmid = engine_template_vmid(engine_vmid)
    vm_name = ENGINE_TEMPLATE_NAME
    tags = f"tezcatlipoca,comp-{Path(comp_dir).name},engine-template"
    expect_tags = {"tezcatlipoca", f"comp-{Path(comp_dir).name}", "engine-template"}

    # A previous build's leftover (plain VM, never converted) is a dead attempt:
    # rebuild it from scratch rather than resuming a half-bootstrapped disk. An
    # untagged leftover passes with the ownership warning; anything tagged without
    # ours is refused — the reserved slot is not a license to destroy foreign VMs.
    # Exception (live-found 2026-09-29): a full clone inherits the BASE image's tags,
    # so a config-PUT failure between the clone and the tag PUT leaves a leftover
    # wearing 'cloud-init;template' — the strict guard would refuse it forever. A
    # NOT-yet-converted VM on the reserved slot carrying the reserved NAME is our
    # dead attempt; adopt it loudly. Converted templates keep the strict check.
    from golden_ops import _is_template  # local: golden_ops imports this module back

    if vmid in {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]} \
            and not _is_template(node, vmid):
        vm = next(v for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                  if v["vmid"] == vmid)
        if (vm.get("name") or "") == ENGINE_TEMPLATE_NAME:
            print(f"    vmid {vmid}: unconverted '{ENGINE_TEMPLATE_NAME}' leftover from a "
                  f"dead template build — adopting and rebuilding")
            destroy_vm_if_exists(node, vmid, expect_tags=None)
    destroy_vm_if_exists(node, vmid, expect_tags=expect_tags)

    live_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
    if vmid in live_vmids:
        raise RuntimeError(f"engine template vmid {vmid} is occupied and not ours to remove")
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{base_engine_vm_id}/clone", data={
        "newid": vmid,
        "name": vm_name,
        "full": 1,
    })["data"]
    wait_for_proxmox_task(node, upid)

    mgmt_ip = os.environ.get("TF_VAR_engine_mgmt_ip", "")
    cfg = {
        "net0": "virtio,bridge=vmbr0",
        "tags": tags,
        # cloud-init identity for the build VM's first boot (the base image bakes none;
        # terraform's user_account normally supplies it for the deployed engine).
        "ciuser": ctx.get("vm_username", "ubuntu"),
        "sshkeys": ctx.get("ssh_public_key_quoted", ""),
        "cores": 4,
        "memory": 4096,
    }
    if mgmt_ip:
        # Portable-node mode (realm): boot the build VM on the PLANNED engine mgmt IP —
        # the deployed engine is destroyed by phase 1 by now, so the address is free,
        # and the converted template's config then matches what apply #1 sets on clones.
        gw = os.environ.get("TF_VAR_engine_mgmt_gw", "")
        cfg["ipconfig0"] = f"ip={mgmt_ip}/24" + (f",gw={gw}" if gw else "")
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=cfg)
    print(f"    {vm_name} cloned from base image (vmid {vmid})")

    start_vm(node, vmid)
    build_ip = mgmt_ip
    if not build_ip:
        # Primary-mode: the base image networks itself (as the terraform engine does
        # today); discover the address via the guest agent once the agent is up.
        from range_ops import wait_for_guest_agent
        wait_for_guest_agent(node, vmid, timeout=300)
        build_ip = _discover_vm_ipv4(node, vmid)
        if not build_ip:
            raise RuntimeError("could not discover the engine-template build VM's IP — "
                               "set TF_VAR_engine_mgmt_ip (portable-node mode) or fix "
                               "the agent channel")
    forget_engine_host_key(build_ip)
    if not wait_for_ssh(ctx["ssh_key_path"], ctx.get("vm_username", "ubuntu"), build_ip,
                        timeout=300):
        raise RuntimeError(f"engine-template build VM {build_ip} never accepted SSH")
    build_ctx = {**ctx, "scoring_engine_ip": build_ip,
                 # the settle probe and every bootstrap SSH authenticate as the
                 # engine's own user, not the default box user
                 "box_username": ctx.get("vm_username", "ubuntu")}

    # A fresh base-image clone runs unattended-upgrades on first boot; the old
    # engine bootstrap's killall+sleep-2 preamble raced it and lost (live-found
    # 2026-09-25: apt rc=100, dpkg lock held by the image's own apt). Wait for the
    # real settle condition first. On realm the guest agent returns NULL data, so
    # this exercises the SSH settle fallback.
    from hardening_ops import wait_boxes_settled, _box_settled_via_ssh
    print("    Waiting for the build VM to settle (unattended-upgrades done, dpkg lock free)...")
    unsettled = wait_boxes_settled([{"vmid": vmid, "ip": build_ip}], node, timeout=600,
                                   ssh_fallback=lambda t: _box_settled_via_ssh(build_ctx, t))
    if unsettled:
        print("    WARNING: build VM never fully settled — proceeding (the bootstrap's "
              "Lock::Timeout covers a straggler)")

    print("    Bootstrapping engine template (packages, Docker, Quotient, apt-cacher-ng)...")
    build_info = bootstrap_scoring_engine(build_ctx, postgres_password, redis_password,
                                          quotient_ref=quotient_ref)

    print("    Cleaning engine template for conversion (fresh state per clone)...")
    clean_engine_for_template(build_ctx)
    stop_vm(node, vmid)
    proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/template")["data"]
    write_template_hash(node, vmid, hash_value,
                        extra=f"quotient_ref={quotient_ref or 'default'}")
    print(f"    {vm_name} (vmid {vmid}) is now a template")
    return vmid, build_info


def _discover_vm_ipv4(node, vmid):
    """First non-loopback IPv4 the guest agent reports (primary-mode build VMs DHCP on
    vmbr0, exactly as the terraform engine does). Never raises."""
    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
        for iface in ifaces:
            for a in iface.get("ip-addresses", []):
                if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127."):
                    return a["ip-address"]
    except Exception:
        pass
    return None


def destroy_engine_template(node, engine_vmid, expect_tags=None):
    """Remove this competition's engine template. Callers destroy its clones first.
    A FOREIGN VM in the slot is skipped with a loud warning instead of aborting the
    teardown — same policy as destroy_golden_set."""
    try:
        destroy_vm_if_exists(node, engine_template_vmid(engine_vmid), expect_tags=expect_tags)
    except RuntimeError as e:
        print(f"    WARNING: engine template slot is FOREIGN — skipping it and "
              f"continuing: {e}")


def git_commit_info():
    """The code frozen alongside the hashes, recorded in .frozen.json."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except Exception:
        commit = "unknown"
    try:
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                    text=True, timeout=10).stdout.strip())
    except Exception:
        dirty = None
    return {"commit": commit, "dirty": dirty}
