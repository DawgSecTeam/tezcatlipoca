"""Written per-competition inputs: the team roster with its passwords, and the resolved SSH key path."""

import json
import os

import providers

from config.config_ops import write_text_atomic
from common.constants import DEFAULT_ENGINE_MGMT_GW, DEFAULT_ENGINE_MGMT_IP
from estate import config


def engine_mgmt_ip_from_env(override=None):
# Static by default: DHCP engines drifted mid-event; empty restores DHCP. The _IS_DEFAULT
# marker stops this export from satisfying the preflight's "explicitly set" hatch — reading
# a default as an operator choice killed a deploy. `override` beats env and the node
# catalog: concurrent ranges on one node cannot share a management address.
    if override:
        config.active().activate({"engine.mgmt_ip": override})
        return override
    if not config.active().is_set("engine.mgmt_ip"):
        config.active().activate({"engine.mgmt_ip": DEFAULT_ENGINE_MGMT_IP})
        os.environ["TEZ_ENGINE_MGMT_IP_IS_DEFAULT"] = "1"
        print(f"  Engine mgmt IP: static {DEFAULT_ENGINE_MGMT_IP} (default — override "
              f"TEZ_ENGINE_MGMT_IP, set '' for DHCP)")
    return config.active().engine.mgmt_ip


def build_estate_inputs(ctx, comp_dir):
    teams_json = json.dumps({
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in ctx.teams.items()})
    write_text_atomic(comp_dir / "teams.json", teams_json)
    ctx.ssh_key_abs = str(config.ssh_key_path())
    # An explicit mgmt IP once skipped this default and the template's ipconfig0 went out
    # with an empty gw= — caught live: PVE 400 "Parameter verification failed".
    if not config.active().engine.mgmt_gw:
        config.active().activate({"engine.mgmt_gw": DEFAULT_ENGINE_MGMT_GW})
    # A context built by hand (tests, recovery paths) may not carry its comp dir yet.
    ctx.comp_dir = ctx.comp_dir or comp_dir
    document = ctx.document()
    # teardown/verify/redeploy read host and engine coordinates from this section, not env.
    cfg = config.active()
    platform = getattr(ctx, "platform", "") or "proxmox"
    if platform == "gcp":
        endpoint, node, datastore = (providers.endpoint_label(platform),
                                      cfg.gcp.zone, "")
    else:
        endpoint, node, datastore = (cfg.proxmox.endpoint.rstrip("/"),
                                      cfg.proxmox.node, cfg.proxmox.datastore)
    identity = {
        "comp_name": ctx.comp_name,
        "name": ctx.name,
        "run_id": ctx.run_id,
        "platform": platform,
        "scoring_vm_id": ctx.engine_vmid,
        "engine_mgmt_ip": ctx.engine_mgmt_ip or "",
        "endpoint": endpoint,
        "node": node,
        "datastore": datastore,
    }
    document.section("identity").update(identity)
    # persist_state() absorbs ctx.state with replace=True: a key absent here is DELETED
    # from the identity on the next phase save. The minted ctx.state predates this
    # function, so without this sync the engine address (and host) vanished from the
    # document mid-deploy and a later resume fell back to the default address.
    ctx.state.update(identity)
    document.section("roster")["boxes"] = ctx.boxes
    document.save()
