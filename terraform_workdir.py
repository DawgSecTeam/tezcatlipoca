"""Per-competition Terraform working dir and terraform-state vmid readers."""

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
_TF_TEMPLATE_DIR = REPO_ROOT / "terraform"
# Only the .tf sources + provider lock are per-competition template files; the
# state, .terraform plugin dir, and terraform.tfvars.json live locally per comp.
_TF_TEMPLATE_FILES = ("main.tf", "variables.tf", "outputs.tf", ".terraform.lock.hcl")


def terraform_dir(comp_dir):
    """Per-competition Terraform working dir (its own state + lock), so two
    competitions can `terraform apply` concurrently on one node instead of
    contending on the single shared terraform/terraform.tfstate."""
    return Path(comp_dir) / "terraform"


# Resource instance addresses that carry a vmid: `...team_box["proxmox:221"]`. The
# satellite variants (team_box_sat1..4) put their teams on other hosts, and the engine
# is a separate resource — all three carry the same suffix shape, so one pattern
# covers every box a deploy phase can produce.
_TF_TEAM_BOX_RESOURCE_NAMES = {"team_box", "team_box_sat1", "team_box_sat2",
                               "team_box_sat3", "team_box_sat4"}


def team_vmids_from_state(comp_dir, run=None):
    """Every team-box vmid in this competition's terraform state, as a sorted list.

    Pure parse of `terraform state pull` — the one artifact that says which machines a
    completed phase actually created. Reads only; never plans, applies, or locks.

    The vmid lives in each instance's `vm_id` attribute, NOT in the resource address:
    team_box keys are box names (`team_box["team1-web01"]`), so an address-shape parser
    finds nothing and a healthy deploy reads as "no machines" (live-found 2026-10-03 —
    the phase-4 checkpoint gate hard-failed a fully-built range that way).

    Raises RuntimeError when the state cannot be read at all (terraform missing, a
    corrupt state, a held lock): "I could not check" must never be reported as "there
    is nothing there", or a resume gate becomes a resume hazard."""
    tf_dir = terraform_dir(comp_dir)
    runner = run or subprocess.run
    try:
        proc = runner(["terraform", "state", "pull"], cwd=str(tf_dir),
                      capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"could not run `terraform state pull` in {tf_dir}: {e}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"`terraform state pull` failed in {tf_dir} (rc={proc.returncode}): "
            f"{detail[-1] if detail else 'no output'}")
    try:
        state = json.loads(proc.stdout or "")
    except ValueError as e:
        raise RuntimeError(
            f"`terraform state pull` returned unparsable JSON in {tf_dir}: {e}")
    vmids = set()
    for resource in state.get("resources") or []:
        if resource.get("type") != "proxmox_virtual_environment_vm":
            continue
        if resource.get("name") not in _TF_TEAM_BOX_RESOURCE_NAMES:
            continue
        for instance in resource.get("instances") or []:
            attrs = instance.get("attributes") or {}
            vmid = attrs.get("vm_id") or attrs.get("id")
            if vmid:
                vmids.add(int(vmid))
    return sorted(vmids)


def ensure_terraform_workdir(comp_dir):
    """Materialize competitions/<id>/terraform/ from the canonical terraform/
    template: (re)copy the .tf sources + provider lock, never touching the local
    tfstate/.terraform/terraform.tfvars.json. Idempotent."""
    dst = terraform_dir(comp_dir)
    dst.mkdir(parents=True, exist_ok=True)
    for name in _TF_TEMPLATE_FILES:
        src = _TF_TEMPLATE_DIR / name
        if src.exists():
            shutil.copy2(src, dst / name)
    return dst


def terraform_plugin_cache_dir():
    """Shared provider-plugin cache so each per-comp `terraform init` links the
    provider from disk instead of re-downloading it."""
    cache = _TF_TEMPLATE_DIR / ".terraform-plugin-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache
