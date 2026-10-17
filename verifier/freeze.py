"""freeze/unfreeze: record (or remove) the freeze record after a passing verify."""

import subprocess
import time

from verifier.context import REPO_ROOT


def git_dirty_lines(repo_root=None):
# Uncommitted paths at the repo root, or None when git cannot answer — an unverifiable tree must never read as clean.
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
    from engine.template_freeze import load_template_hashes
    return load_template_hashes(comp_dir) or None


def do_freeze(comp_dir, args, gate, coverage_passed):
# Records the template hashes this verify just passed against plus the code commit; refused unless every gate passed.
    from engine.template_freeze import code_path_dirty, git_commit_info

    hashes = freeze_hashes(comp_dir)
    if not hashes or not (hashes.get("engine") or {}).get("hash") or not hashes.get("golden"):
        print("  FREEZE refused — no template hash record in the state document; "
              "deploy once on the M4 pipeline first.")
        return False
    if not coverage_passed:
        print("  FREEZE refused — plant-coverage did not pass.")
        return False
    if not all(gate.values()):
        print(f"  FREEZE refused — failing gates: "
              f"{', '.join(k for k, v in gate.items() if not v)}")
        return False
    # A freeze taken while deploy-path CODE is uncommitted pins a commit that does not contain the
    # verified code; generated/run state is expected to be dirty after a run and only noted.
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
    }
    from estate import state
    document = state.EstateState.load(comp_dir)
    document.section("templates")["frozen"] = record
    document.save()
    print(f"  FROZEN — {state.state_path(comp_dir)} written. Engine + "
          f"{len(hashes['golden'])} golden hash(es) recorded. Rebuild is now refused on "
          f"config drift; a full teardown needs --end-of-competition.")
    return True


def do_unfreeze(comp_dir, confirm):
    if not confirm:
        print("  UNFREEZE refused — pass --confirm-unfreeze. Unfreezing mid-event "
              "defeats the freeze; it is meant for use BEFORE the competition starts.")
        return False
    from estate import state
    document = state.EstateState.load(comp_dir)
    if not document.section("templates").pop("frozen", None):
        print("  Nothing to unfreeze.")
        return True
    document.save()
    print("  UNFROZEN — the freeze record is gone.")
    return True
