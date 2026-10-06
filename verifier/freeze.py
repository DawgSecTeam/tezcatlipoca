"""M4 freeze/unfreeze: record (or remove) .frozen.json after a passing verify."""

import json
import os
import subprocess
import time

from windows_ops import is_windows_template

from verifier.context import REPO_ROOT
from verifier.loaders import load_boxes


def git_dirty_lines(repo_root=None):
    """Uncommitted paths in this checkout (`git status --porcelain` at the repo root).

    Returns a list of porcelain lines, or None when git cannot answer (not a repo, git
    missing, non-zero rc) — an unverifiable tree must never read as clean. Scoped to
    REPO_ROOT rather than the cwd so the same answer holds wherever verify was invoked."""
    try:
        out = subprocess.run(["git", "status", "--porcelain"],
                             cwd=str(repo_root or REPO_ROOT),
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def freeze_hashes(comp_dir):
    """The template hashes a freeze would record (None when nothing is recorded yet)."""
    path = comp_dir / ".template-hashes.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def do_freeze(comp_dir, args, gate, coverage_passed):
    """M4 freeze: record the template hashes this verify just passed against, plus the
    code commit, timestamp, and gate results. Preconditions: every gate PASS including
    plant-coverage and services; Windows/domain lineups additionally require the
    operator's --windows-domain-validated attestation (that the run exercised them)."""
    from template_ops import code_path_dirty, git_commit_info

    hashes = freeze_hashes(comp_dir)
    if not hashes or not (hashes.get("engine") or {}).get("hash") or not hashes.get("golden"):
        print("  FREEZE refused — no template hash record (.template-hashes.json); "
              "deploy once on the M4 pipeline first.")
        return False
    if not coverage_passed:
        print("  FREEZE refused — plant-coverage did not pass.")
        return False
    if not all(gate.values()):
        print(f"  FREEZE refused — failing gates: "
              f"{', '.join(k for k, v in gate.items() if not v)}")
        return False
    boxes = load_boxes(comp_dir)
    roles = comp_dir / "domain_roles.json"
    needs_domain = (roles.exists()
                    or any(is_windows_template(b.get("template") or "") for b in (boxes or [])))
    if needs_domain and "domains" not in gate and not args.windows_domain_validated:
        print("  FREEZE refused — this lineup uses Windows/domains; pass "
              "--windows-domain-validated to attest that this run's Windows/domain "
              "validation (DomainSIDs, machine SIDs, three-pass ordering) passed.")
        return False
    # A freeze taken while deploy-path CODE is uncommitted pins a commit that does not
    # contain the verified code (the deploy-time check is a warning, not a gate trip — see
    # template_ops.frozen_code_drift). Generated/run state (comp JSON, placement.json,
    # nodes.json, terraform state, .env backups) is expected to be dirty after a run and
    # must not block; it is noted, not refused.
    dirty = git_dirty_lines()
    if dirty is None:
        print("  FREEZE warning — could not read the git worktree state (git unavailable "
              "or not a checkout); the frozen record may not match the code tree.")
    elif dirty:
        code_dirty = [d for d in dirty if code_path_dirty([d])]
        if code_dirty:
            shown = ", ".join(d[:70] for d in code_dirty[:3]) + (" …" if len(code_dirty) > 3 else "")
            print(f"  FREEZE refused — {len(code_dirty)} uncommitted code path(s) in the "
                  f"worktree ({shown}). Freeze LAST, after the final commit: the frozen record "
                  f"pins the commit the run was verified on. To back out before the competition "
                  f"starts: --unfreeze --confirm-unfreeze, commit, then --freeze again.")
            return False
        shown = ", ".join(d[:60] for d in dirty[:3]) + (" …" if len(dirty) > 3 else "")
        print(f"  FREEZE note — {len(dirty)} uncommitted non-code path(s) ignored for the "
              f"freeze ({shown}); only deploy-path code (.py/.tf/.sh/.j2/.ps1) blocks it.")
    record = {
        "frozen_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "code": git_commit_info(),
        "hashes": {"engine": hashes["engine"], "golden": hashes["golden"]},
        "verify_report": {"gates": gate, "plant_coverage": coverage_passed},
        "windows_domain_validated": bool(args.windows_domain_validated),
    }
    path = comp_dir / ".frozen.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    print(f"  FROZEN — {path} written. Engine + {len(hashes['golden'])} golden hash(es) "
          f"recorded. Rebuild is now refused on config drift; --full teardown needs "
          f"--end-of-competition.")
    return True


def do_unfreeze(comp_dir, confirm):
    if not confirm:
        print("  UNFREEZE refused — pass --confirm-unfreeze. Unfreezing mid-event "
              "defeats the freeze; it is meant for use BEFORE the competition starts.")
        return False
    path = comp_dir / ".frozen.json"
    if not path.exists():
        print("  Nothing to unfreeze.")
        return True
    path.unlink()
    print("  UNFROZEN — .frozen.json removed.")
    return True
