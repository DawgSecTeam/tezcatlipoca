"""Per-competition console-only Proxmox tokens for the student portal (docs/portal.md).

The portal on the engine opens team consoles through PVE's vncproxy/vncwebsocket, which needs a
Proxmox credential ON the engine — a host every team NIC touches. So that credential is minted
per competition and scoped to the bone:

  user   tezcon-<comp>-<run_id>@pve       one per node the competition's teams live on
  token  <user>!portal                    privsep=0: the token IS the user's permissions
  role   TezConsole = VM.Console only     (shared across competitions; never deleted)
  ACLs   /vms/<vmid> -> user, one per team VM of THIS competition on that node

What the token can do is exactly what the students already get: a console on this competition's
own team VMs. It cannot start/stop, read configs, or touch any other VM.

Teardown deletes exactly `tezcon-<comp>-<run_id>@pve` on every node (the user's token and ACLs
go with it). Exact name only — never a prefix sweep: AGENTS.md records an over-broad sweep
destroying two other competitions' infrastructure. A user that is already gone is success.

This module never decides WHETHER the portal runs; portal_ops calls it and degrades (logins
work, consoles off) when minting fails.
"""

import os

import requests

from pve_api import proxmox_api_for

ROLE_ID = "TezConsole"
ROLE_PRIVS = "VM.Console"
TOKEN_NAME = "portal"
# Privileges the deploy token needs to mint (preflight probe). Sys.Modify only matters the
# first time, when the TezConsole role does not exist yet on a node.
REQUIRED_PRIVS = ("User.Modify", "Permissions.Modify", "Realm.AllocateUser", "Sys.Modify")


def console_user(comp_name, run_id):
    """The one PVE userid this run's portal owns (deterministic: teardown recomputes it)."""
    if not comp_name or not run_id:
        raise ValueError("console user needs both a competition name and a run id")
    return f"tezcon-{comp_name}-{run_id}@pve"


def _http_detail(e):
    resp = getattr(e, "response", None)
    if resp is None:
        return str(e)
    return f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"


def _is_absent(e):
    """PVE answers 500 for "no such user/role/token" — tell that apart from real failures."""
    resp = getattr(e, "response", None)
    text = (resp.text or "").lower() if resp is not None else ""
    return resp is not None and resp.status_code in (404, 500) and (
        "does not exist" in text or "no such" in text)


def _exists_already(e):
    resp = getattr(e, "response", None)
    text = (resp.text or "").lower() if resp is not None else ""
    return resp is not None and "already exists" in text


def node_targets(comp_dir_targets, placement, default_node):
    """Group a competition's team VMs by the node record that hosts them.

    Returns {node_key: {"pve_node", "endpoint", "token_env", "tls_fingerprint", "vmids"}}.
    node_key is the nodes.json record NAME under a multi-node placement (PVE hostnames can
    repeat across independent hosts) and the env node otherwise; every portal box record
    carries the same key, so the portal can find its node without re-deriving placement.
    """
    out = {}
    if placement:
        for t in comp_dir_targets:
            key = placement["team_nodes"][t["team_key"]]
            rec = placement["nodes"][key]
            entry = out.setdefault(key, {
                "pve_node": rec["node"], "endpoint": rec["endpoint"],
                "token_env": rec["token_env"],
                "tls_fingerprint": rec.get("tls_fingerprint") or "", "vmids": []})
            entry["vmids"].append(int(t["vmid"]))
    else:
        entry = {"pve_node": default_node,
                 "endpoint": os.environ.get("TF_VAR_proxmox_endpoint", ""),
                 "token_env": "TF_VAR_proxmox_api_token",
                 "tls_fingerprint": os.environ.get("PROXMOX_TLS_FINGERPRINT", ""),
                 "vmids": [int(t["vmid"]) for t in comp_dir_targets]}
        out[default_node] = entry
    for entry in out.values():
        entry["vmids"] = sorted(set(entry["vmids"]))
    return out


def team_node_key(target, placement, default_node):
    """The node_key (see node_targets) of one target."""
    return placement["team_nodes"][target["team_key"]] if placement else default_node


def probe_permissions(endpoint, token, api=proxmox_api_for):
    """Missing REQUIRED_PRIVS of the deploy token on `/`, as a list (empty = can mint)."""
    perms = api(endpoint, token, "GET", "/access/permissions", params={"path": "/"})["data"]
    have = perms.get("/") or {}
    return [p for p in REQUIRED_PRIVS if not have.get(p)]


def _ensure_role(endpoint, token, api):
    try:
        api(endpoint, token, "GET", f"/access/roles/{ROLE_ID}")
        return
    except requests.HTTPError as e:
        if not _is_absent(e) and getattr(e.response, "status_code", None) != 500:
            raise
    try:
        api(endpoint, token, "POST", "/access/roles", data={"roleid": ROLE_ID,
                                                           "privs": ROLE_PRIVS})
    except requests.HTTPError as e:
        if not _exists_already(e):  # another deploy created it between our GET and POST
            raise


def mint_node_token(comp_name, run_id, node, token, previous=None, api=proxmox_api_for):
    """Mint (or re-mint) this run's console token on one node and (re)apply its ACLs.

    `previous` is the {user, token_id, secret} recorded by an earlier attempt of the SAME run:
    the secret is reused (PVE only reveals it at creation) as long as the token still exists;
    otherwise the token is recreated. ACL PUTs are idempotent, so a resume re-applies them —
    which also picks up vmids added since (a redeploy that rebuilt boxes)."""
    endpoint = node["endpoint"]
    userid = console_user(comp_name, run_id)
    _ensure_role(endpoint, token, api)
    try:
        api(endpoint, token, "POST", "/access/users",
            data={"userid": userid,
                  "comment": f"tezcatlipoca portal console ({comp_name}, {run_id})"})
    except requests.HTTPError as e:
        if not _exists_already(e):
            raise
    token_path = f"/access/users/{userid}/token/{TOKEN_NAME}"
    full_id = f"{userid}!{TOKEN_NAME}"
    secret = None
    if previous and previous.get("user") == userid and previous.get("secret"):
        try:
            api(endpoint, token, "GET", token_path)
            secret = previous["secret"]
        except requests.HTTPError as e:
            if not _is_absent(e):
                raise
    if secret is None:
        try:
            api(endpoint, token, "DELETE", token_path)
        except requests.HTTPError as e:
            if not _is_absent(e):
                raise
        made = api(endpoint, token, "POST", token_path,
                   data={"privsep": 0, "comment": "student portal console relay"})["data"]
        secret = made["value"]
        full_id = made.get("full-tokenid") or full_id
    for vmid in node["vmids"]:
        api(endpoint, token, "PUT", "/access/acl",
            data={"path": f"/vms/{vmid}", "roles": ROLE_ID, "users": userid, "propagate": 0})
    return {"user": userid, "token_id": full_id, "secret": secret}


def mint_console_tokens(comp_name, run_id, nodes, previous=None, api=proxmox_api_for,
                        env=os.environ):
    """Mint the console token on every node of `nodes` (node_targets output).

    Returns (tokens, problems): tokens maps node_key -> {user, token_id, secret} for the nodes
    that succeeded; problems lists "<node_key>: <why>" for the rest. Never raises for a
    Proxmox refusal — the caller records a degradation and the portal runs without consoles on
    those nodes."""
    tokens, problems = {}, []
    previous = previous or {}
    for key, node in nodes.items():
        deploy_token = env.get(node["token_env"])
        if not deploy_token or not node.get("endpoint"):
            problems.append(f"{key}: no endpoint/token in the environment ({node['token_env']})")
            continue
        try:
            missing = probe_permissions(node["endpoint"], deploy_token, api=api)
            if missing:
                problems.append(f"{key}: deploy token lacks {', '.join(missing)}")
                continue
            tokens[key] = mint_node_token(comp_name, run_id, node, deploy_token,
                                          previous=previous.get(key), api=api)
        except (requests.RequestException, KeyError, ValueError) as e:
            problems.append(f"{key}: {_http_detail(e)}")
    return tokens, problems


def revoke_console_tokens(comp_name, run_id, nodes, api=proxmox_api_for, env=os.environ):
    """Delete exactly this run's console user on every node. Returns a list of problems
    (empty = clean). An already-absent user counts as clean, so teardown re-runs converge."""
    userid = console_user(comp_name, run_id)
    problems = []
    for key, node in nodes.items():
        deploy_token = env.get(node["token_env"])
        if not deploy_token or not node.get("endpoint"):
            problems.append(f"{key}: no endpoint/token in the environment ({node['token_env']})")
            continue
        try:
            api(node["endpoint"], deploy_token, "DELETE", f"/access/users/{userid}")
            print(f"  Portal console user {userid} removed on {key}")
        except requests.HTTPError as e:
            if _is_absent(e):
                print(f"  Portal console user {userid} already absent on {key}")
            else:
                problems.append(f"{key}: {_http_detail(e)}")
        except requests.RequestException as e:
            problems.append(f"{key}: {e}")
    return problems
