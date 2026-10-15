"""Per-team teardown: remove ONE team from a live range (the mid-event drop-out).

Everything here is scoped to the deleted team — the other teams' VMs, the engine and
the templates are never addressed:

  1. load_ownership + require_terraform_state (the same refusals the full teardown
     runs; a state without its run id names no ownership).
  2. hard-stop the team's clones (ownership-gated, exactly the terminal plan's
     pre-stop, narrowed to the one team).
  3. targeted `terraform destroy -target=...` of exactly the team_box / team_box_satN
     + bridge instances this comp dir's STATE carries, so only instances that exist
     are ever named. team_box depends only on the per-team bridges — the dependency
     graph cannot cascade to the engine or to another team.
  4. deactivate the team in Quotient (active:false drops it from scoring rounds;
     Quotient has no delete API, so the row stays listed as inactive).
  5. regenerate + push event.conf WITHOUT the team (revokes its scoreboard + portal
     login) and rewrite the portal's portal.json without it (the portal reads its
     config at startup, so a compose restart applies it). The push restarts Quotient.
  6. record the deletion: teams.json + .deploy_state.json (teams/deleted_teams) and
     terraform.tfvars.json lose the team, so a later full `terraform destroy` and any
     apply see a consistent config (team_nics' triggers change with it — the next
     plan re-runs its netplan without the dead subnet).

Resumable by re-running: every step is idempotent against its own result.
"""

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import requests
import urllib3

from common.ssh_ops import read_terraform_ctx
from common.terraform_workdir import terraform_plugin_cache_dir
from common.utils import run_terraform
from infra.ownership import Refusal
from multinode.placement_record import activate_placement, read_placement, record_of
from teardown.machinery import (clear_stale_state_lock, load_ownership,
                                pre_stop_windows_boxes, require_terraform_state,
                                repair_ssh_key_path, terraform_init)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DESTROY_ATTEMPT_TIMEOUT_S = 1800


def _team_prefix(team_key, team):
    # team 1 is named `team1-<box>` (terraform main.tf / targets.enumerate_targets);
    # every other team names its clones `<identifier>-<box>`.
    return team_key if team_key == "team1" else str(team["identifier"])


def team_targets_from_state(tf_state, team_key, team):
    # The -target list, read from THIS comp dir's terraform state: only instances that
    # exist are ever named, so the destroy cannot drift into another team.
    prefix = _team_prefix(team_key, team) + "-"
    vm_res, vm_keys, bridges = None, set(), []
    for res in tf_state.get("resources") or []:
        keys = [i.get("index_key") for i in res.get("instances") or []]
        if res.get("type") == "proxmox_virtual_environment_vm" and str(res.get("name", "")).startswith("team_box"):
            for k in keys:
                if isinstance(k, str) and k.startswith(prefix):
                    vm_res = f"{res['type']}.{res['name']}"
                    vm_keys.add(k)
        elif (res.get("type") == "proxmox_network_linux_bridge"
              and (res.get("name") == "transit_bridge"
                   or str(res.get("name", "")).startswith("team_bridge"))
              and team_key in keys):
            bridges.append(f"{res['type']}.{res['name']}[\"{team_key}\"]")
    return [f'{vm_res}["{k}"]' for k in sorted(vm_keys)] if vm_res else [], bridges


def _deactivate_team(base_url, scoring_password, team_key, identifier):
    # Quotient has no team delete: active:false is the lever (the round loop skips
    # inactive teams). Idempotent — the row keeps existing.
    session = requests.Session()
    r = session.post(f"{base_url}/api/login",
                     json={"username": "scoring", "password": scoring_password}, timeout=10)
    r.raise_for_status()
    r = session.get(f"{base_url}/api/teams", timeout=10)
    r.raise_for_status()
    tid = next((t["ID"] for t in r.json() if t.get("Name") == team_key), None)
    if tid is None:
        return "team not present in Quotient (already removed)"
    r = session.post(f"{base_url}/api/admin/teams", timeout=10,
                     json={"teams": [{"id": tid, "identifier": str(identifier), "active": False}]})
    r.raise_for_status()
    return f"deactivated (Quotient id {tid})"


def _refresh_engine_conf(comp_dir, teams, boxes, state, name, log):
    from engine.engine_event_ops import push_event_conf
    push_event_conf(comp_dir, teams, boxes, read_terraform_ctx(comp_dir), name,
                    inject_password=state.get("inject_password"),
                    admin_password=state.get("admin_password"),
                    scoring_password=state.get("scoring_password"),
                    postgres_password=state.get("postgres_password"),
                    redis_password=state.get("redis_password"),
                    box_creds=state.get("box_creds") or {},
                    extra_credlists=({"domain": state["domain_creds"]}
                                     if state.get("domain_creds") else None))
    log("  event.conf re-pushed without the team (Quotient restarts — scoreboard blips ~1 min)")


def _refresh_portal_config(comp_dir, team_key, log):
    # The portal reads portal.json once at startup: drop the team from the config the
    # comp dir already carries, push it, restart. Console users minted for the dead
    # team's vmids stay until the full teardown revokes them — harmless, the portal
    # can no longer resolve their boxes.
    from engine.engine_cmd_ops import _run_engine_cmd
    config_path = Path(comp_dir) / "portal.json"
    if not config_path.exists():
        return "portal not deployed"
    config = json.loads(config_path.read_text())
    if team_key not in (config.get("teams") or {}):
        return "team not in portal.json"
    config["teams"].pop(team_key)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    b64 = base64.b64encode(json.dumps(config, indent=2).encode()).decode()
    _run_engine_cmd(read_terraform_ctx(comp_dir),
                    f"echo '{b64}' | base64 -d | sudo tee /opt/tez-portal/portal.json >/dev/null && "
                    f"sudo chmod 600 /opt/tez-portal/portal.json && "
                    f"cd /opt/tez-portal && sudo docker compose restart portal",
                    timeout=180, step="portal config refresh")
    return "portal.json re-pushed without the team"


def run_team_teardown(comp_dir, team_key, *, name=None, env=None, log=print):
# Remove one team from a live range. Returns {"clean": bool, "problems": [..]}.
    comp_dir = Path(comp_dir)
    env = dict(env if env is not None else os.environ)
    deployed_state, ownership = load_ownership(comp_dir)
    require_terraform_state(comp_dir, deployed_state)

    teams = json.loads((comp_dir / "teams.json").read_text())
    if team_key not in teams:
        raise Refusal(saw=f"teams.json without '{team_key}'",
                      expected="the team key of a deployed team (one of "
                               f"{', '.join(sorted(teams))})",
                      remedy="delete with the exact team key shown on the run page")
    if team_key not in (deployed_state.get("teams") or {}):
        raise Refusal(saw=f"'{team_key}' missing from .deploy_state.json teams",
                      expected="a team this deploy owns",
                      remedy="nothing to do — the team was already deleted")
    team = teams[team_key]
    identifier = team["identifier"]
    boxes = json.loads((comp_dir / "boxes.json").read_text())
    if name is None:
        from common.utils import load_compfile
        name = load_compfile(comp_dir / "Compfile")[0]

    placement = read_placement(comp_dir)
    default_node = env.get("TF_VAR_proxmox_node", "pve")
    nodes = [default_node]
    team_nodes = None
    if placement:
        nodes = [record_of(placement, s["name"]).node for s in placement["satellites"]] + [default_node]
        activate_placement(placement)
        # Callers snapshot os.environ BEFORE activate_placement — see run_terminal_teardown.
        for key in ("TF_VAR_proxmox_endpoint", "TF_VAR_proxmox_api_token", "TF_VAR_proxmox_node",
                    "TF_VAR_datastore", "TF_VAR_template_vm_id"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        if team_key in (placement.get("team_nodes") or {}):
            team_nodes = {team_key: record_of(placement, placement["team_nodes"][team_key]).node}

    tf_state = json.loads((comp_dir / "terraform" / "terraform.tfstate").read_text())
    targets, bridges = team_targets_from_state(tf_state, team_key, team)
    if not targets:
        raise Refusal(saw="no team_box instances for this team in terraform state",
                      expected="the linked clones the deploy built",
                      remedy="the team's VMs are not terraform-managed here — check the "
                             "node by hand before proceeding")

    log(f"  Team '{team_key}' (identifier {identifier}): {len(targets)} clone(s), "
        f"{len(bridges)} bridge(s) — ownership run id '{ownership.run_id}'.")

    log("  Hard-stopping the team's clones...")
    pre_stop_windows_boxes({team_key: team}, boxes, default_node,
                           team_nodes=team_nodes, ownership=ownership)

    tf_cwd = str(comp_dir / "terraform")
    tf_env = {**env, "TF_PLUGIN_CACHE_DIR": str(terraform_plugin_cache_dir())}
    repair_ssh_key_path(Path(tf_cwd) / "terraform.tfvars.json", tf_env)
    terraform_init(tf_env, tf_cwd)
    problems = []
    for attempt in (1, 2, 3, 4):
        if attempt > 1:
            clear_stale_state_lock(tf_cwd)
        try:
            proc = run_terraform(
                ["destroy", "-parallelism=4", "-auto-approve",
                 *(f"-target={t}" for t in targets + bridges)],
                cwd=tf_cwd, env=tf_env, timeout=DESTROY_ATTEMPT_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            log(f"  terraform destroy timed out (attempt {attempt}/4, partial progress kept)...")
            continue
        if proc.returncode == 0:
            break
        log(f"  terraform destroy failed (attempt {attempt}/4, partial progress kept)...")
    else:
        problems.append("terraform destroy did not complete — the team's VMs may still "
                        "exist; re-run (it is safe: every step is idempotent)")

    # Verify the team's clones are actually gone before touching anything else.
    prefix = _team_prefix(team_key, team) + "-"
    for node in ([team_nodes[team_key]] if team_nodes else nodes):
        try:
            from infra.client import proxmox_api
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not verify on {node}: {e}")
            continue
        alive = [v for v in vms if (v.get("name") or "").startswith(prefix)]
        if alive:
            problems.append(f"still on {node}: " + ", ".join(
                f"{v.get('name')} (vmid {v['vmid']})" for v in alive))

    if problems:
        return {"clean": False, "problems": problems}

    log(f"  Deactivating '{team_key}' in Quotient...")
    scoring_ip = read_terraform_ctx(comp_dir)["scoring_engine_ip"]
    try:
        log(f"    {_deactivate_team(f'http://{scoring_ip}', deployed_state.get('scoring_password'), team_key, identifier)}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"Quotient deactivation failed ({type(e).__name__}: {e}) — the dead "
                        "team's checks keep scoring SLA penalties; re-run to retry")
        return {"clean": False, "problems": problems}

    remaining = {k: v for k, v in teams.items() if k != team_key}
    state = deployed_state
    try:
        _refresh_engine_conf(comp_dir, remaining, boxes, state, name, log)
        log(f"    {_refresh_portal_config(comp_dir, team_key, log)}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"engine/portal refresh failed ({type(e).__name__}: {e}) — the team's "
                        "VMs are destroyed and it is deactivated, but its logins live on; "
                        "re-run to retry")
        return {"clean": False, "problems": problems}

    # Record the deletion everywhere the roster lives, so apply/destroy and the
    # dashboard all see a consistent competition.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for roster in (teams, state.get("teams") or {}):
        roster.pop(team_key, None)
    state.setdefault("deleted_teams", []).append(
        {"team": team_key, "identifier": str(identifier), "at": stamp})
    from config.config_ops import write_state, write_text_atomic
    write_state(comp_dir / ".deploy_state.json", state)
    write_text_atomic(comp_dir / "teams.json", json.dumps(remaining, indent=2))
    tfvars_path = comp_dir / "terraform" / "terraform.tfvars.json"
    if tfvars_path.exists():
        tfvars = json.loads(tfvars_path.read_text())
        if isinstance(tfvars.get("teams"), dict) and team_key in tfvars["teams"]:
            tfvars["teams"].pop(team_key)
            tfvars_path.write_text(json.dumps(tfvars, indent=2) + "\n")
            log("  terraform.tfvars.json updated (team_nics re-applies its netplan on the "
                "next plan — the dead subnet leaves the engine)")
    log(f"  Team '{team_key}' removed: clones destroyed, deactivated in Quotient, "
        f"logins revoked. {len(remaining)} team(s) remain.")
    return {"clean": True, "problems": []}
