"""terraform.tfvars.json assembly and the engine mgmt IP / gateway environment defaults."""

import json
import os
from pathlib import Path

from config_ops import update_env, write_text_atomic
from constants import DEFAULT_ENGINE_MGMT_GW, DEFAULT_ENGINE_MGMT_IP
from nodes_ops import satellite_routes_for, satellite_tfvars
from range_ops import ensure_terraform_workdir


def engine_mgmt_ip_from_env():
    """The engine's static mgmt IP from the env, defaulted and re-exported.

    Engine mgmt IP: static by default. The DHCP engine rebooted onto a different
    address mid-event while terraform's saved output — which deploy/verify/
    credentials all consume — stayed stale (shakedown-5x4: .221→.243→.233).
    An explicit TF_VAR_engine_mgmt_ip="" keeps the old DHCP behavior; the
    chosen value is also exported for template_ops (build-VM ipconfig0)."""
    ip = os.environ.get("TF_VAR_engine_mgmt_ip")
    if ip is None:
        ip = DEFAULT_ENGINE_MGMT_IP
        print(f"  Engine mgmt IP: static {ip} (default — override "
              f"TF_VAR_engine_mgmt_ip, set '' for DHCP)")
        os.environ["TF_VAR_engine_mgmt_ip"] = ip
    return ip


def build_terraform_inputs(terraform, comp_dir, spec, secrets, identity, place):
    """Assemble terraform.tfvars.json + the workdir/ssh fields, into `terraform`.

    terraform.tfvars.json outranks TF_VAR_* env, so it is the authoritative copy of
    this competition's teams/boxes/engine-vmid (concurrent comps on one node must not
    clobber each other through the shared .env). The stale-terraform-state refusal is
    gates.check_stale_terraform_state; prepare() has already run it by the time this
    writes anything."""
    teams_json_src = {
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in secrets.teams.items()
    }
    if place.placement:
        # terraform's per-slot resources key on this: which host builds each team.
        for team_key in teams_json_src:
            teams_json_src[team_key]["slot"] = place.placement["team_slots"][team_key]
    teams_json = json.dumps(teams_json_src)
    boxes_json = json.dumps(spec.boxes)

    update_env({
        "TF_VAR_teams": teams_json,
        "TF_VAR_boxes_per_team": boxes_json,
        "TF_VAR_box_password": secrets.box_password,
        "TF_VAR_box_username": spec.box_username,
        "TF_VAR_scoring_vm_id": str(identity.engine_vmid),
    })

    # teams.json holds every team password: write it with 0600 applied at creation
    # (a write_text-then-chmod leaves it briefly world-readable).
    write_text_atomic(comp_dir / "teams.json", teams_json)

    # Per-competition Terraform working dir + tfvars so concurrent competitions on
    # one node don't share the single terraform/terraform.tfstate or clobber each
    # other via the shared .env. terraform.tfvars.json outranks TF_VAR_* env, so it
    # is authoritative for this comp's teams/boxes/engine-vmid regardless of what
    # another concurrent deploy wrote to .env. The ssh key path is made absolute
    # because it was ../proxmox-relative to the (now deeper) working dir.
    terraform.tf_dir = ensure_terraform_workdir(comp_dir)

    raw_key = os.environ["TF_VAR_ssh_private_key_path"]
    terraform.ssh_key_abs = (raw_key if os.path.isabs(raw_key)
                             else str((Path("terraform") / raw_key).resolve()))
    # Engine mgmt IP: static by default. The DHCP engine rebooted onto a different
    # address mid-event while terraform's saved output — which deploy/verify/
    # credentials all consume — stayed stale (shakedown-5x4: .221→.243→.233).
    # An explicit TF_VAR_engine_mgmt_ip="" keeps the old DHCP behavior; the
    # chosen value is also exported for template_ops (build-VM ipconfig0).
    terraform.engine_mgmt_ip = engine_mgmt_ip_from_env()
    # Default the gateway whenever the engine mgmt IP is static (live-found 2026-09-29:
    # an explicitly-set mgmt IP skipped this branch on the .150 env, and the engine
    # template build's ipconfig0 went out with an empty gw= — PVE 400 "Parameter
    # verification failed").
    if not os.environ.get("TF_VAR_engine_mgmt_gw"):
        os.environ["TF_VAR_engine_mgmt_gw"] = DEFAULT_ENGINE_MGMT_GW
    terraform.tfvars = {
        # teams_json_src carries the placement slot per team (multi-node) — tfvars
        # outranks the env, so this is the copy terraform actually reads.
        "teams": teams_json_src,
        "boxes_per_team": spec.boxes,
        "box_password": secrets.box_password,
        "box_username": spec.box_username,
        "event_name": spec.name,
        "scoring_vm_id": identity.engine_vmid,
        "ssh_private_key_path": terraform.ssh_key_abs,
        # M3.3 two-apply: apply #1 (phase 2) builds the engine + bridges with an empty
        # team_box for_each; apply #2 (phase 4) flips this to true once the golden
        # templates exist. team_nics/reboot keep their full-teams config in apply #1 so
        # the engine already has a NIC on every bridge for the golden plant.
        "build_team_boxes": False,
        "golden_template_ids": [],
        # M4: apply #2 rewrites tfvars with this intact — a resume that skips phase 2
        # must not let the engine clone source fall back to the base image (which would
        # replace the engine with an unbootstrapped full clone mid-pipeline).
        "engine_clone_id": int(secrets.state.get("engine_template_vmid") or 0),
        # Portable-node mode (realm): static engine mgmt IP instead of agent discovery.
        # Persisted via tfvars so resumes don't depend on the env var being re-exported.
        "engine_mgmt_ip": terraform.engine_mgmt_ip,
        "engine_mgmt_gw": os.environ.get("TF_VAR_engine_mgmt_gw", ""),
        # Per-deploy identity: terraform stamps it onto the engine and every team box
        # (main.tf tags), making them reclaimable by THIS run's teardown only.
        "run_tag": secrets.run_id,
    }
    if place.placement:
        # Multi-node: per-slot satellite providers and the engine's jump routes.
        terraform.tfvars["satellites"] = satellite_tfvars(place.placement)
        terraform.tfvars["satellite_routes"] = satellite_routes_for(place.placement)
    terraform.tfvars_path = terraform.tf_dir / "terraform.tfvars.json"
    # Carries TF_VAR_box_password + the per-team passwords.
    write_text_atomic(terraform.tfvars_path, json.dumps(terraform.tfvars, indent=2))
