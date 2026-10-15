"""Written per-competition inputs: the team roster with its passwords, and the absolute
SSH key path the phases dial with.

Replaces the terraform.tfvars.json assembly. That file existed to outrank the ambient
TF_VAR_* env for concurrent competitions on one node; the state document now carries the
authoritative roster and secrets, so all that remains here is the on-disk teams.json the
teardown and identifier defaults read, plus the resolved key path.
"""

import json
import os

from config.config_ops import write_text_atomic
from common.constants import DEFAULT_ENGINE_MGMT_GW, DEFAULT_ENGINE_MGMT_IP
from estate import config


def engine_mgmt_ip_from_env(override=None):
# The engine's static management IP, defaulted and re-exported.
#
#     Static by default: a DHCP engine rebooted onto a different address mid-event while
#     the saved record stayed stale (shakedown-5x4: .221→.243→.233). An explicit empty
#     value keeps the old DHCP behavior. The default is MARKED as a default on purpose —
#     the preflight's "explicitly set" escape hatch must not be satisfied by this code's
#     own export (the .150 default is SSH-poisoned on the tailnet path, and reading this
#     export as an operator choice killed a deploy 12 minutes in). `override` is this
#     run's own --engine-mgmt-ip: it must beat both the ambient env and the node catalog,
#     because two concurrent ranges on one node cannot share a management address.
    if override:
        os.environ["TF_VAR_engine_mgmt_ip"] = override
        return override
    ip = os.environ.get("TF_VAR_engine_mgmt_ip")
    if ip is None:
        ip = DEFAULT_ENGINE_MGMT_IP
        os.environ["TEZ_ENGINE_MGMT_IP_IS_DEFAULT"] = "1"
        print(f"  Engine mgmt IP: static {ip} (default — override "
              f"TF_VAR_engine_mgmt_ip, set '' for DHCP)")
        os.environ["TF_VAR_engine_mgmt_ip"] = ip
    return ip


def build_estate_inputs(ctx, comp_dir):
# Write teams.json (0600 at creation), bind the SSH key path, open the state document.
    teams_json = json.dumps({
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in ctx.teams.items()})
    write_text_atomic(comp_dir / "teams.json", teams_json)
    ctx.ssh_key_abs = str(config.ssh_key_path())
    # Default the gateway whenever the engine mgmt IP is static: an explicitly-set mgmt IP
    # once skipped this and the engine template's ipconfig0 went out with an empty gw=
    # (live-found 2026-09-29 — PVE 400 "Parameter verification failed").
    if not os.environ.get("TF_VAR_engine_mgmt_gw"):
        os.environ["TF_VAR_engine_mgmt_gw"] = DEFAULT_ENGINE_MGMT_GW
    # A context built by hand (tests, recovery paths) may not carry its comp dir yet.
    ctx.comp_dir = ctx.comp_dir or comp_dir
    document = ctx.document()
    # The document is self-describing from the start: teardown, verify and redeploy all
    # read the host and engine coordinates from here rather than from the environment.
    cfg = config.active()
    document.section("identity").update({
        "comp_name": ctx.comp_name,
        "name": ctx.name,
        "run_id": ctx.run_id,
        "scoring_vm_id": ctx.engine_vmid,
        "engine_mgmt_ip": ctx.engine_mgmt_ip or "",
        "endpoint": cfg.proxmox.endpoint.rstrip("/"),
        "node": cfg.proxmox.node,
        "datastore": cfg.proxmox.datastore,
    })
    document.section("roster")["boxes"] = ctx.boxes
    document.save()
