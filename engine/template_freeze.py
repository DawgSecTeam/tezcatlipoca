"""Per-competition template lifecycle: hashes, the engine template, reuse/rebuild, freeze state."""

import hashlib
import inspect
import json
import re
import subprocess
import time
from pathlib import Path

from infra.client import proxmox_api
from multinode.placement_record import engine_template_vmid


def load_template_hashes(comp_dir):
# Template reuse keys on this; a fresh deploy must not clear it.
    from estate import state
    templates = state.EstateState.load(comp_dir).section("templates")
    return {key: templates[key] for key in ("engine", "golden", "golden_planted", "updated")
            if templates.get(key)}


def golden_plant_checkpoints(comp_dir, run_id=None):
    planted = load_template_hashes(comp_dir).get("golden_planted")
    if not isinstance(planted, dict):
        return {}
    wanted_run = run_id or ""
    return {box: rec for box, rec in planted.items()
            if isinstance(rec, dict) and rec.get("run") == wanted_run}


def clear_golden_plant_checkpoints(comp_dir):
    from estate import state
    document = state.EstateState.load(comp_dir)
    document.section("templates").pop("golden_planted", None)
    document.save()


def save_template_hashes(comp_dir, **entries):
    from estate import state
    document = state.EstateState.load(comp_dir)
    templates = document.section("templates")
    for key, value in entries.items():
        if key in ("golden", "golden_planted"):
            templates.setdefault(key, {}).update(value)
        else:
            templates[key] = value
    templates["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    document.save()


def forget_template_hashes(comp_dir):
    from estate import state
    document = state.EstateState.load(comp_dir)
    templates = document.section("templates")
    for key in ("engine", "golden", "golden_planted", "updated"):
        templates.pop(key, None)
    document.save()


def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def code_hash(*functions):
    parts = []
    for f in functions:
        parts.append(inspect.getsource(f) if callable(f) else str(f))
    return sha256_text("\n\x00\n".join(parts))


def _split_inputs(config_inputs, code_inputs):
    return {"config": config_inputs, "code": code_inputs}


def hash_from_inputs(inputs):
    return sha256_text(canonical_json(inputs))


def golden_payload_hash(bundle_path, box_name):
# Per-box, not whole-bundle: one box's pin change must not rebuild the others' goldens.
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
# Deliberate exclusions: team count/IPs, event.conf, post-clone configs, snapshot names, node endpoint.
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
        {"build_golden_set+apt_prep": None},
    )


def _clean_func():
    from engine.engine_identity_ops import clean_engine_for_template
    return clean_engine_for_template


def template_stamp_current(node, vmid, expected_hash):
# Reuse keys on the template's own stamp, not the state file: the dashboard seeder strips it.
    return bool(expected_hash) and stored_template_hash(node, vmid) == expected_hash


def engine_hash_inputs(base_engine_vm_id, quotient_ref, engine_shape, bootstrap_func):
    return _split_inputs(
        {
            "base_engine_vm_id": int(base_engine_vm_id),
            "quotient_ref": (quotient_ref or "").strip(),
        },
        {
            # The clean step shapes the template disk as much as bootstrap does.
            "bootstrap_scoring_engine": code_hash(bootstrap_func, _clean_func()),
            # Only the engine's own shape counts: the whole deploy path rebuilt on any team-box edit.
            "estate.apply:create_engine": code_hash(engine_shape),
        },
    )


_DESC_RE = re.compile(r"tezcatlipoca-m4 hash=([0-9a-f]{64})")


def stored_template_hash(node, vmid):
    try:
        desc = (proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
                .get("description") or "")
    except Exception:
        desc = ""
    m = _DESC_RE.search(desc)
    return m.group(1) if m else None


def write_template_hash(node, vmid, hash_value, extra=""):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config",
                data={"description": f"tezcatlipoca-m4 hash={hash_value} built={stamp}"
                                     + (f" {extra}" if extra else "")})


def frozen_state(comp_dir):
    from estate import state
    return state.EstateState.load(comp_dir).section("templates").get("frozen") or None


def classify_drift(stored_inputs, current_inputs):
    drift = {"config": [], "code": []}
    for cls in ("config", "code"):
        old = (stored_inputs or {}).get(cls) or {}
        new = (current_inputs or {}).get(cls) or {}
        for field in sorted(set(old) | set(new)):
            if canonical_json(old.get(field)) != canonical_json(new.get(field)):
                drift[cls].append(field)
    return drift


def golden_freeze_gate(name, stored_inputs, current_inputs, frozen_at):
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
# True = rebuild. When frozen: config drift raises, code drift proceeds on the frozen template.
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
    stored = (frozen or {}).get("code") or {}
    stored_commit = str(stored.get("commit") or "").strip()
    if not stored_commit or stored_commit == "unknown":
        return None
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


_CODE_SUFFIXES = (".py", ".sh", ".j2", ".ps1")


def code_path_dirty(porcelain_lines):
# The run writes its own state files; only code-file dirt invalidates a freeze.
    for line in porcelain_lines or []:
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path.endswith(_CODE_SUFFIXES) or path.endswith(("Makefile", "Dockerfile")):
            return True
    return False


def git_commit_info():
# `dirty` is the same definition --freeze refuses on.
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
