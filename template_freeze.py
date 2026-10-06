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

from constants import ENGINE_TEMPLATE_VMID_OFFSET
from pve_api import proxmox_api

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


def golden_plant_checkpoints(comp_dir):
    """box name -> golden hash, for goldens that have been planted AND smoke-passed.

    Deliberately a separate key from `golden` (which records *converted* templates):
    this one marks the intermediate state that used to be thrown away. On cde-2026 a
    re-entry rolled every planted golden back to tz-base and re-planted the whole set —
    33 rollbacks (11 each for web01, ftp01, db01) across 10 runs — while only ftp01 was
    ever the problem. Conversion still waits for the whole set; this only avoids
    repeating work that already passed.

    Delete the `golden_planted` key (or the whole file) to force a re-plant."""
    planted = load_template_hashes(comp_dir).get("golden_planted")
    return planted if isinstance(planted, dict) else {}


def save_template_hashes(comp_dir, **entries):
    """Merge entries (engine={hash, inputs}, golden={box: {hash, inputs}}) into the
    record. Atomic rename; 0600 — golden inputs embed box_password."""
    path = Path(comp_dir) / HASHES_FILE
    data = load_template_hashes(comp_dir)
    for key, value in entries.items():
        if key in ("golden", "golden_planted"):
            data.setdefault(key, {}).update(value)
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


def frozen_code_drift(frozen):
    """Warn when the checkout no longer matches the code the freeze recorded.

    `.frozen.json`'s `code` ({commit, dirty} — git_commit_info) was recorded by
    verify --freeze but nothing read it: the drift gate keys on per-template input
    hashes and code-class input drift is warn-only, so a commit taken after --freeze
    ran the event on code the freeze never verified (known-issues "Freeze is not
    commit-safe"). Returns the warning text, or None when the tree still matches.

    Deliberately NOT fatal: resuming a range after a docs/test commit is a normal
    flow, and the operator unfreezes explicitly when a change must invalidate the
    freeze. `frozen` is the parsed .frozen.json (template_ops.frozen_state)."""
    stored = (frozen or {}).get("code") or {}
    stored_commit = str(stored.get("commit") or "").strip()
    if not stored_commit or stored_commit == "unknown":
        return None  # pre-commit-info freeze record, or git unavailable at freeze time
    frozen_at = (frozen or {}).get("frozen_at") or "unknown time"
    now = git_commit_info()
    current_commit = str(now.get("commit") or "").strip()
    if current_commit != stored_commit:
        return (f"  WARNING: the code tree moved since FREEZE ({frozen_at}): frozen at "
                f"{stored_commit[:12]}, now on {current_commit[:12] or 'unknown'} — this "
                f"range is running on code the freeze did not verify. If the change is "
                f"intentional, unfreeze deliberately (verify-competition.py --unfreeze "
                f"--confirm-unfreeze) and re-verify + re-freeze.")
    if now.get("dirty"):
        return (f"  WARNING: the code worktree is DIRTY and no longer matches the FREEZE "
                f"record ({frozen_at}, commit {stored_commit[:12]}) — this range is running "
                f"on uncommitted code. Commit or stash it, or unfreeze deliberately "
                f"(verify-competition.py --unfreeze --confirm-unfreeze).")
    if stored.get("dirty"):
        return (f"  WARNING: the FREEZE record itself ({frozen_at}, commit "
                f"{stored_commit[:12]}) was written from a DIRTY worktree, so the code it "
                f"verified is ambiguous. Re-freeze from a clean tree "
                f"(verify-competition.py --unfreeze --confirm-unfreeze, commit, --freeze).")
    return None


_CODE_SUFFIXES = (".py", ".tf", ".sh", ".j2", ".ps1")


def code_path_dirty(porcelain_lines):
    """True when any `git status --porcelain` entry touches deploy-path code.

    Only uncommitted *code* invalidates a freeze: the frozen record pins the commit the
    run was verified on, while generated/run state (comp JSON, placement.json, nodes.json,
    terraform state, .env backups) is written by the run itself and is never committed as
    part of the workflow. Treating every porcelain line as dirt made `--freeze` refuse in
    any post-deploy worktree (verified 2026-10-02 on the scale8 worktree: a modified
    placement.json, an untracked nodes.json and a .env backup), so the gate is scoped here.
    Pure function of the porcelain lines — see verify-competition.do_freeze."""
    for line in porcelain_lines or []:
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path.endswith(_CODE_SUFFIXES) or path.endswith(("Makefile", "Dockerfile")):
            return True
    return False


def git_commit_info():
    """The code frozen alongside the hashes, recorded in .frozen.json.

    `dirty` means uncommitted *deploy-path code* (see code_path_dirty) — deliberately the
    same definition verify --freeze refuses on, so the recorded flag and the gate agree and
    post-run runtime state does not raise a false warning on every later deploy."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
    except Exception:
        commit = "unknown"
    try:
        porcelain = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                   text=True, timeout=10).stdout
        dirty = code_path_dirty(porcelain.splitlines())
    except Exception:
        dirty = None
    return {"commit": commit, "dirty": dirty}
