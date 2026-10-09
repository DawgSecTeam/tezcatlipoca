"""Remote access: the scoring engine becomes a headscale subnet router; participants
reach only their own team's network.

Model (docs/remote-access.md): every deploy enrolls the engine as a tailscale node
(tag:range-router) advertising each team's 192.168.<identifier>.0/24, pushes a
per-deploy firewall unit that SNATs tailnet-sourced traffic to the team gateway IP
(boxes trust SSH only from 192.168.<id>.1 — the same treatment the jump VMs give
routed-red traffic), and hands out per-person one-time preauth keys tagged
tag:comp-<id>-team-<tid>. The headscale ACL policy grants each tag its own subnet
plus the scoreboard; the pre-existing users keep the full access they had when the
tailnet ran with no policy at all (group:full-access -> *:*).

This module owns the ENTIRE headscale ACL policy file: the policy is regenerated
from a small state file that lives beside it on the headscale host, so deploys and
teardowns never text-surgery HuJSON. The VPS-side state (full-access users + the
active competitions) seeds itself on first contact from the foundation installed
2026-10-08; edit the state file to change full-access membership until the
limiting-by-groups work replaces it.

Everything here degrades loudly: a missing TEZ_HEADSCALE_* env var or an
unreachable headscale host is a deploy-stage failure, not a skipped step —
"deployed for every comp" means the tailnet wiring is part of the range.
"""

import base64
import json
import os
import re
import subprocess
from pathlib import Path

TAILSCALE_INSTALL = "curl -fsSL https://tailscale.com/install.sh | sh"
TAILNET_SRC = "100.64.0.0/10"
INFRA_USER_DEFAULT = "cyberrange-infra"
POLICY_STATE_FILE = "/home/sysadmin/headscale/config/tezcatlipoca-remote-access.json"
POLICY_FILE = "/etc/headscale/acl.hujson"

PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


# ── pure renderers (golden-filed in tests/test_remote_access.py) ─────────────────


def team_subnet(identifier):
    return f"192.168.{identifier}.0/24"


def team_tag(comp_id, identifier):
    return f"tag:comp-{comp_id}-team-{identifier}"


def router_node_name(comp_id):
    return f"eng-{comp_id}"


def load_roster(comp_dir):
    """people.json: {"team1": ["alice", ...], ...} — optional; {} when absent.

    Person names become headscale usernames (`comp-<id>-<person>`) and argv
    fragments on the headscale host, so they are validated hard: lowercase
    [a-z0-9._-], must be unique across the whole roster, and must name a team this
    competition actually has."""
    path = Path(comp_dir) / "people.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except ValueError as e:
        raise SystemExit(f"  ERROR: people.json is not valid JSON: {e}")
    if not isinstance(raw, dict):
        raise SystemExit("  ERROR: people.json must be an object of {team: [people]}")
    seen = set()
    roster = {}
    for team, people in raw.items():
        if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
            raise SystemExit(f"  ERROR: people.json[{team!r}] must be a list of names")
        cleaned = []
        for person in people:
            name = person.strip().lower().replace(" ", "-")
            if not PERSON_RE.match(name):
                raise SystemExit(
                    f"  ERROR: people.json[{team!r}] name {person!r} is not usable as a "
                    f"headscale username (lowercase letters/digits/._- only)")
            if name in seen:
                raise SystemExit(f"  ERROR: person {name!r} appears twice in people.json")
            seen.add(name)
            cleaned.append(name)
        roster[team] = cleaned
    return roster


def render_policy(full_access_users, comps):
    """The WHOLE headscale ACL policy, regenerated every time.

    `full_access_users`: headscale usernames WITH the trailing @ the v2 policy
    syntax requires. `comps`: [{"comp_id", "engine_ip", "teams": [{"identifier",
    "participants"}]}]. Grants are keyed on the participant's USERNAME, not a node
    tag: live-proven 2026-10-08, headscale v0.29 builds routers' packet filters for
    user/group sources but NOT for tag sources (tag-sourced dials black-hole at the
    router while netmap route visibility still works), so participant nodes enroll
    untagged and each team's grant names its people directly. Two grant lines per
    team: the whole /24 on every port, the scoreboard on 80 only. Teams without
    participants emit nothing."""
    out = []
    out.append("{")
    out.append("  // Managed by tezcatlipoca (remote_access_ops.render_policy) — do not")
    out.append("  // hand-edit: regenerated from tezcatlipoca-remote-access.json on every")
    out.append("  // deploy/teardown that touches remote access. Foundation behavior:")
    out.append("  // the tailnet ran with NO policy (allow-all) until 2026-10-08; the")
    out.append("  // full-access grant preserves that for the pre-existing users.")
    users = ", ".join(f'"{u}"' for u in full_access_users)
    out.append(f'  "groups": {{"group:full-access": [{users},]}},')
    out.append("  \"tagOwners\": {")
    out.append('    "tag:range-router": ["cyberrange-infra@"],')
    out.append("  },")
    out.append('  "autoApprovers": {"routes": {"192.168.0.0/16": ["tag:range-router"]}},')
    out.append('  "grants": [')
    out.append('    {"src": ["group:full-access"], "dst": ["*"], "ip": ["*"]},')
    for comp in sorted(comps, key=lambda c: c["comp_id"]):
        ip = comp["engine_ip"]
        scoreboard = f'"{ip if ip.startswith("10.0.0.") else "10.0.0." + ip}"'
        for team in sorted(comp["teams"], key=lambda t: str(t["identifier"])):
            if not team["participants"]:
                continue
            srcs = ", ".join(f'"comp-{comp["comp_id"]}-{p}@"'
                             for p in sorted(team["participants"]))
            out.append(f'    {{"src": [{srcs}], '
                       f'"dst": ["{team_subnet(team["identifier"])}"], "ip": ["*"]}},')
            out.append(f'    {{"src": [{srcs}], "dst": [{scoreboard}], "ip": ["80"]}},')
    out.append("  ],")
    out.append("}")
    return "\n".join(out) + "\n"


def remote_access_firewall_script(local_ids, satellite_ids, engine_mgmt_ip):
    """iptables-restore-free re-assert script for the ENGINE (idempotent -C || -A lines).

    Local teams: SNAT to 192.168.<id>.1 — the engine owns the gateway IP on its own
    team bridges, so tailnet-sourced traffic arrives at boxes indistinguishable from
    engine traffic (gateway-IP-only SSH trust preserved). Satellite teams: SNAT to
    the ENGINE'S MGMT IP instead — the jump VM owns .1 on satellite bridges and its
    existing `-s engine -> SNAT to .1` rule finishes the job, exactly the two-hop
    treatment routed-red traffic gets (return traffic rides conntrack back through
    the engine, whose tailscale0 delivers it to the participant)."""
    lines = ["#!/bin/bash",
             "# Re-asserted every 30s (range-remote-access.timer): tailnet -> team "
             "forwarding + gateway SNAT for headscale remote access.",
             f"iptables -C FORWARD -s {TAILNET_SRC} -d 192.168.0.0/16 -j ACCEPT 2>/dev/null || "
             f"iptables -A FORWARD -s {TAILNET_SRC} -d 192.168.0.0/16 -j ACCEPT"]
    for identifier in local_ids:
        gw = f"192.168.{identifier}.1"
        lines.append(
            f"iptables -t nat -C POSTROUTING -s {TAILNET_SRC} -d {team_subnet(identifier)} "
            f"-j SNAT --to-source {gw} 2>/dev/null || "
            f"iptables -t nat -A POSTROUTING -s {TAILNET_SRC} -d {team_subnet(identifier)} "
            f"-j SNAT --to-source {gw}")
    for identifier in satellite_ids:
        lines.append(
            f"iptables -t nat -C POSTROUTING -s {TAILNET_SRC} -d {team_subnet(identifier)} "
            f"-j SNAT --to-source {engine_mgmt_ip} 2>/dev/null || "
            f"iptables -t nat -A POSTROUTING -s {TAILNET_SRC} -d {team_subnet(identifier)} "
            f"-j SNAT --to-source {engine_mgmt_ip}")
    return "\n".join(lines) + "\n"


def remote_access_systemd_units():
    """range-remote-access .service/.timer — the same re-assert pattern as
    range-firewall (Docker wipes iptables on restart; the timer re-asserts)."""
    service = ("[Unit]\n"
               "Description=Re-assert tailnet->team SNAT/forwarding (headscale remote access)\n"
               "After=docker.service\n"
               "\n"
               "[Service]\n"
               "Type=oneshot\n"
               "ExecStart=/usr/local/sbin/range-remote-access.sh\n")
    timer = ("[Unit]\n"
             "Description=Periodically re-assert tailnet->team SNAT/forwarding\n"
             "\n"
             "[Timer]\n"
             "OnBootSec=30\n"
             "OnUnitActiveSec=30\n"
             "\n"
             "[Install]\n"
             "WantedBy=timers.target\n")
    return service, timer


def enrollment_markdown(comp_id, server_url, entries):
    """The per-person enrollment handout. entries: [{"person","team","key"}]."""
    lines = [f"# {comp_id} — remote access enrollment",
             "",
             "Each command enrolls ONE device as that person. A key is single-use:",
             "a second device needs a fresh key (re-run the deploy step or mint one",
             "with `headscale preauthkeys create`).",
             ""]
    for e in entries:
        lines.append(f"## {e['person']} (team {e['team']})")
        lines.append("")
        lines.append("```")
        lines.append(f"tailscale up --login-server {server_url} --authkey {e['key']}")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


# ── headscale host I/O (one ssh shape, one place) ────────────────────────────────


def _headscale_env():
    """The TEZ_HEADSCALE_* env contract; a missing var is a deploy-stage failure."""
    missing = [v for v in ("TEZ_HEADSCALE_URL", "TEZ_HEADSCALE_SSH_HOST",
                           "TEZ_HEADSCALE_SSH_USER", "TEZ_HEADSCALE_SSH_PASSWORD")
               if not os.environ.get(v)]
    if missing:
        raise SystemExit(
            f"  ERROR: remote access is enabled but {', '.join(missing)} not set. "
            f"Add the TEZ_HEADSCALE_* block to .env (see .env.example and "
            f"docs/remote-access.md).")
    return {v: os.environ[v] for v in ("TEZ_HEADSCALE_URL", "TEZ_HEADSCALE_SSH_HOST",
                                       "TEZ_HEADSCALE_SSH_USER", "TEZ_HEADSCALE_SSH_PASSWORD")}


def _hs_ssh(cmd, password, input_text="", timeout=90, check=True, capture=True):
    """One sshpass ssh to the headscale host. `cmd` runs under `sudo -S` with the
    password on stdin (sysadmin has passworded sudo; docker is root-only there)."""
    env = _headscale_env()
    argv = ["sshpass", "-p", password, "ssh",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
            f"{env['TEZ_HEADSCALE_SSH_USER']}@{env['TEZ_HEADSCALE_SSH_HOST']}", cmd]
    return subprocess.run(argv, input=input_text, text=capture, capture_output=capture,
                          timeout=timeout, check=check)


def hs_cli(args, timeout=90, check=True):
    """`headscale <args>` inside the container on the headscale host. `args` is a
    pre-validated single string — only module-generated comp ids/person names ever
    reach it (PERSON_RE), never free user input."""
    env = _headscale_env()
    pw = env["TEZ_HEADSCALE_SSH_PASSWORD"]
    return _hs_ssh(f"sudo -S -p '' docker exec headscale headscale {args}", pw,
                   input_text=pw + "\n", timeout=timeout, check=check)


def hs_json(args, timeout=90, check=True):
    r = hs_cli(f"{args} -o json", timeout=timeout, check=check)
    return json.loads(r.stdout)


def headscale_reachable():
    """Cheap liveness probe for preflight: `headscale health` via the container."""
    try:
        r = hs_cli("health", timeout=30, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _infra_user_id():
    """The router nodes' owning service user (TEZ_HEADSCALE_INFRA_USER, default
    cyberrange-infra — the account that already owns the tailnet's infra nodes)."""
    name = os.environ.get("TEZ_HEADSCALE_INFRA_USER", INFRA_USER_DEFAULT)
    for u in hs_json("users list"):
        if u.get("name") == name:
            return u["id"]
    raise SystemExit(f"  ERROR: headscale user '{name}' not found — create it once "
                     f"(`headscale users create {name}`) or point "
                     f"TEZ_HEADSCALE_INFRA_USER at the router-owner user.")


def _ensure_user(name):
    for u in hs_json("users list"):
        if u.get("name") == name:
            return u["id"]
    hs_cli(f"users create {name}")
    for u in hs_json("users list"):
        if u.get("name") == name:
            return u["id"]
    raise SystemExit(f"  ERROR: headscale users create {name} did not yield a user")


def _mint_preauth_key(user_id, tags, expiration):
    """`tags` may be None for an untagged key (node keeps its user identity) or a
    comma-separated tag list."""
    args = f"preauthkeys create -u {user_id} --expiration {expiration}"
    if tags:
        args += f" --tags {tags}"
    out = hs_json(args)
    key = out.get("key")
    if not key:
        raise SystemExit(f"  ERROR: preauthkeys create returned no key material: "
                         f"{sorted(out)}")
    return key


# ── policy state (the one writer of acl.hujson) ──────────────────────────────────


def _read_policy_state():
    env = _headscale_env()
    pw = env["TEZ_HEADSCALE_SSH_PASSWORD"]
    r = _hs_ssh(f"sudo -S -p '' cat {POLICY_STATE_FILE}", pw, input_text=pw + "\n",
                check=False)
    if r.returncode == 0 and r.stdout.strip():
        return json.loads(r.stdout)
    # Seed: the foundation installed 2026-10-08 granted these users the tailnet's
    # previous (policy-less, allow-all) behavior. Edit the state file on the
    # headscale host to change full-access membership.
    return {"full_access": [f"{u}@" for u in (INFRA_USER_DEFAULT, "hnasher1", "dipam1",
                                              "sdavis24", "ckegly")],
            "comps": {}}


def _write_policy_state(state):
    payload = json.dumps(state, indent=2, sort_keys=True)
    env = _headscale_env()
    pw = env["TEZ_HEADSCALE_SSH_PASSWORD"]
    remote = (f"cat > /tmp/tez-ra-state.json <<'TEZEOF'\n{payload}\nTEZEOF\n"
              f"sudo -S -p '' cp /tmp/tez-ra-state.json {POLICY_STATE_FILE}")
    _hs_ssh(remote, pw, input_text=pw + "\n", timeout=60)


def _apply_policy(full_access, comps):
    """Render, stage, validate, restart. The restart is brief and expected: the
    WireGuard data plane keeps flowing through a control-plane restart."""
    env = _headscale_env()
    pw = env["TEZ_HEADSCALE_SSH_PASSWORD"]
    policy = render_policy(full_access, comps)
    remote = (f"cat > /tmp/acl.next.hujson <<'TEZEOF'\n{policy}TEZEOF\n"
              f"sudo -S -p '' cp /tmp/acl.next.hujson /home/sysadmin/headscale/config/acl.hujson")
    _hs_ssh(remote, pw, input_text=pw + "\n", timeout=60)
    r = hs_cli(f"policy check --file {POLICY_FILE}", check=False)
    if r.returncode != 0:
        raise SystemExit(f"  ERROR: generated headscale policy failed validation:\n"
                         f"{r.stdout}{r.stderr}")
    hs_cli("policy get", timeout=30, check=False)  # smoke: CLI can read it
    _hs_ssh("sudo -S -p '' docker restart headscale", pw, input_text=pw + "\n", timeout=120)
    r = hs_cli("health", timeout=60, check=False)
    if r.returncode != 0:
        raise SystemExit("  ERROR: headscale did not come back healthy after policy "
                         "apply — check acl.hujson on the headscale host (rollback: "
                         "config.yaml.bak-tezcatlipoca-20261008 there).")


def policy_upsert_comp(comp_id, engine_ip, teams):
    """teams: [{"identifier", "participants"}] — insert/replace this comp's entry
    and re-apply. Idempotent on resume by construction."""
    state = _read_policy_state()
    state.setdefault("comps", {})[comp_id] = {"engine_ip": engine_ip, "teams": teams}
    _write_policy_state(state)
    _apply_policy(state["full_access"], [dict(v, comp_id=k)
                                         for k, v in sorted(state["comps"].items())])


def policy_remove_comp(comp_id):
    state = _read_policy_state()
    if comp_id in state.get("comps", {}):
        del state["comps"][comp_id]
        _write_policy_state(state)
        _apply_policy(state["full_access"], [dict(v, comp_id=k)
                                             for k, v in sorted(state["comps"].items())])


# ── route-conflict preflight ─────────────────────────────────────────────────────


def route_conflicts(comp_id, planned_identifiers):
    """Advertised subnet routes from OTHER nodes overlapping this deploy's team
    subnets. Concurrent ranges sharing an identifier would cross-route each other's
    participants (two routers advertising 192.168.101.0/24 are one HA route group),
    so this is a refusal, not a warning. Our own previous router is excluded: an
    engine rebuild leaves its node registered until teardown."""
    ours = router_node_name(comp_id)
    conflicts = []
    entries = hs_json("nodes list-routes")
    for entry in entries:
        name = entry.get("name", "")
        if name == ours:
            continue
        live = set(entry.get("approved_routes") or []) | set(entry.get("subnet_routes") or [])
        for identifier in planned_identifiers:
            subnet = team_subnet(identifier)
            if subnet in live:
                conflicts.append(f"192.168.{identifier}.0/24 advertised by '{name}'")
    return conflicts


# ── engine side (phase 3) ────────────────────────────────────────────────────────


def _engine_cmd(ctx, cmd, **kw):
    from engine_cmd_ops import _run_engine_cmd
    return _run_engine_cmd(ctx, cmd, **kw)


def engine_tailscale_enrolled(ctx, server_url):
    """True when the engine's tailscaled is Running against OUR control server.

    `status --json` reports the control server as CurrentTailnet.Name (bare host,
    no scheme) on current tailscale versions; older shapes exposed ControlURL —
    accept either, and fail the check on any JSON the field hunt can't satisfy."""
    r = _engine_cmd(ctx, "sudo tailscale status --json 2>/dev/null || true",
                    check=False, timeout=30, capture=True, step="tailscale status")
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return False
    if data.get("BackendState") != "Running":
        return False
    tailnet = data.get("CurrentTailnet") or {}
    host = server_url.rstrip("/").removeprefix("https://").removeprefix("http://")
    control_url = (tailnet.get("ControlURL") or "").rstrip("/")
    return host in (control_url, (tailnet.get("Name") or "").rstrip("/"))


def enroll_engine_router(ctx, comp_id, all_identifiers):
    """Install (once) + enroll/update the engine as tag:range-router advertising
    every team subnet. Minting a fresh key on each run is deliberate: the key is
    single-use, short-lived, and an unused one costs nothing."""
    server_url = os.environ["TEZ_HEADSCALE_URL"].rstrip("/")
    routes = ",".join(team_subnet(i) for i in all_identifiers)
    hostname = router_node_name(comp_id)
    if not engine_tailscale_enrolled(ctx, server_url):
        print("  Installing tailscale on the engine (first run)...")
        # Retry loop, not `|| true`: the engine's apt machinery races phase 3
        # (documented class — masked install failures resurface as a 127 on the
        # version check), and a silently-missing binary is worse than a loud one.
        _engine_cmd(ctx,
                    "for i in 1 2 3; do command -v tailscale >/dev/null && break; "
                    f"sudo {TAILSCALE_INSTALL} > /tmp/ts-install.log 2>&1 || true; "
                    "sleep 5; done; "
                    "command -v tailscale >/dev/null || { echo 'tailscale install "
                    "failed:'; cat /tmp/ts-install.log; exit 127; }; tailscale version",
                    timeout=900, step="install tailscale")
        user_id = _infra_user_id()
        key = _mint_preauth_key(user_id, "tag:range-router", "12h")
        print(f"  Enrolling engine as headscale node '{hostname}' "
              f"({len(all_identifiers)} team subnets)...")
        _engine_cmd(ctx,
                    f"sudo tailscale up --login-server={server_url} --authkey={key} "
                    f"--hostname={hostname} --advertise-routes={routes} --accept-dns=false",
                    timeout=180, step="tailscale up")
    else:
        print(f"  Engine already enrolled — updating advertised routes "
              f"({len(all_identifiers)} team subnets)...")
        _engine_cmd(ctx, f"sudo tailscale set --advertise-routes={routes}",
                    timeout=60, step="tailscale set routes")
    _engine_cmd(ctx, "sudo tailscale status | head -2", timeout=30,
                step="tailscale status confirm")


def push_remote_access_firewall(tf_ctx, engine_mgmt_ip, local_ids, satellite_ids):
    """The per-deploy SNAT/forward unit — separate from the template's
    range-firewall.sh so the engine template hash never moves. `tf_ctx` is the
    terraform connection dict every engine SSH hop takes."""
    script = remote_access_firewall_script(local_ids, satellite_ids, engine_mgmt_ip)
    service, timer = remote_access_systemd_units()
    s_b64 = base64.b64encode(script.encode()).decode()
    svc_b64 = base64.b64encode(service.encode()).decode()
    tmr_b64 = base64.b64encode(timer.encode()).decode()
    _engine_cmd(tf_ctx, (
        f"echo '{s_b64}' | base64 -d | sudo tee /usr/local/sbin/range-remote-access.sh "
        f"> /dev/null && sudo chmod +x /usr/local/sbin/range-remote-access.sh && "
        f"echo '{svc_b64}' | base64 -d | sudo tee /etc/systemd/system/range-remote-access.service "
        f"> /dev/null && "
        f"echo '{tmr_b64}' | base64 -d | sudo tee /etc/systemd/system/range-remote-access.timer "
        f"> /dev/null && "
        "sudo systemctl daemon-reload && sudo systemctl enable --now range-remote-access.timer"
    ), timeout=30, step="install range-remote-access unit+timer")
    _engine_cmd(tf_ctx, "sudo /usr/local/sbin/range-remote-access.sh && "
                        "sudo iptables -t nat -S POSTROUTING | grep -c SNAT || true",
                timeout=30, step="assert remote-access SNAT rules")


# ── orchestration ────────────────────────────────────────────────────────────────


def _comp_teams(ctx):
    """[{"identifier", "participants"}] for every team, participants from
    people.json. Refuses a roster naming a team this competition doesn't have."""
    roster = load_roster(ctx.comp_dir)
    teams = []
    for team_key in sorted(ctx.teams):
        identifier = str(ctx.teams[team_key]["identifier"])
        participants = roster.get(team_key, [])
        teams.append({"identifier": identifier, "participants": participants})
    unknown = set(roster) - set(ctx.teams)
    if unknown:
        raise SystemExit(f"  ERROR: people.json names teams this competition does not "
                         f"have: {', '.join(sorted(unknown))} "
                         f"(known: {', '.join(sorted(ctx.teams))})")
    return teams


def setup_remote_access(ctx):
    """Phase-3 step: engine enrollment + SNAT unit + headscale users/keys/policy +
    enrollment artifacts. Every comp, unless `remote_access 0` in the Compfile."""
    env = _headscale_env()
    comp_id = ctx.comp_name
    teams = _comp_teams(ctx)
    all_ids = [t["identifier"] for t in teams]
    engine_ip = ctx.tf_ctx.get("scoring_engine_ip") or ctx.engine_mgmt_ip
    placement = ctx.placement or {}
    team_slots = placement.get("team_slots", {})
    team_keys_by_id = {str(ctx.teams[k]["identifier"]): k for k in ctx.teams}
    satellite_ids = [i for i in all_ids if team_slots.get(team_keys_by_id.get(i), 0) != 0]
    local_ids = [i for i in all_ids if i not in satellite_ids]

    print(f"  Remote access: enrolling engine router '{router_node_name(comp_id)}' at "
          f"{env['TEZ_HEADSCALE_URL']}")
    conflicts = route_conflicts(comp_id, all_ids)
    if conflicts:
        raise SystemExit(
            "  ERROR: headscale already has live subnet route(s) overlapping this "
            "competition's team subnets — two routers advertising the same /24 would "
            "cross-route each other's participants:\n    "
            + "\n    ".join(conflicts)
            + "\n  Tear down the other range first, or move this competition to "
              "disjoint team identifiers (TF_VAR_team_identifiers).")

    enroll_engine_router(ctx.tf_ctx, comp_id, all_ids)
    print("  Pushing range-remote-access unit (tailnet->team SNAT, re-asserted 30s)...")
    push_remote_access_firewall(ctx.tf_ctx, ctx.engine_mgmt_ip, local_ids, satellite_ids)

    entries = []
    for team in teams:
        for person in team["participants"]:
            user = f"comp-{comp_id}-{person}"
            uid = _ensure_user(user)
            ttl = os.environ.get("TEZ_REMOTE_KEY_TTL", "72h")
            # Deliberately UNTAGGED: a tagged node's identity is its tag, and
            # headscale v0.29 does not expand tag sources into routers' packet
            # filters (live-proven 2026-10-08) — the username is the grant key.
            key = _mint_preauth_key(uid, None, ttl)
            entries.append({"person": person, "team": team["identifier"], "key": key,
                            "user": user})
    policy_upsert_comp(comp_id, engine_ip,
                       [{"identifier": t["identifier"], "participants": t["participants"]}
                        for t in teams])

    out_dir = ctx.comp_dir / "remote-access"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "enrollment.md").write_text(
        enrollment_markdown(comp_id, env["TEZ_HEADSCALE_URL"], entries))
    keys_path = out_dir / "keys.json"
    keys_path.write_text(json.dumps(entries, indent=2))
    os.chmod(keys_path, 0o600)

    ctx.state["remote_access"] = {
        "enabled": True,
        "comp_id": comp_id,
        "engine_ip": engine_ip,
        "router_node": router_node_name(comp_id),
        "teams": {team_keys_by_id[t["identifier"]]: t["identifier"] for t in teams},
        "participants": [e["user"] for e in entries],
    }
    print(f"  Remote access ready: {len(entries)} participant key(s) in "
          f"competitions/{comp_id}/remote-access/ (full access for pre-existing "
          f"users is untouched).")


def teardown_remote_access(comp_dir, state=None):
    """Destroy-path cleanup: warn-and-proceed, like the rest of teardown. Reads the
    deploy state's remote_access record; absent (pre-feature comp, or the flag off)
    means nothing to do."""
    if state is None:
        state_path = Path(comp_dir) / ".deploy_state.json"
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
    ra = (state or {}).get("remote_access")
    if not ra or not ra.get("enabled"):
        return
    comp_id = ra["comp_id"]
    print(f"\nCleaning up headscale remote access for '{comp_id}' (warn-and-proceed)...")
    try:
        for entry in hs_json("nodes list"):
            if entry.get("name") == ra.get("router_node") or entry.get(
                    "hostname") == ra.get("router_node"):
                hs_cli(f"nodes delete -i {entry['id']} --force")
                print(f"  Deleted headscale node '{ra['router_node']}' "
                      "(revokes the engine's tailnet key).")
    except (subprocess.SubprocessError, ValueError, OSError, KeyError) as e:
        print(f"  WARNING: could not delete the engine's headscale node: {e}")
    # headscale refuses `users destroy` while the user still owns nodes
    # ("user not empty: node(s) found"), so participant devices go first.
    try:
        prefix = f"comp-{comp_id}-"
        for entry in hs_json("nodes list"):
            owner = (entry.get("user") or {}).get("name") or ""
            if owner.startswith(prefix):
                hs_cli(f"nodes delete -i {entry['id']} --force")
                print(f"  Deleted participant device '{entry.get('name')}' "
                      f"(user {owner}).")
    except (subprocess.SubprocessError, ValueError, OSError, KeyError) as e:
        print(f"  WARNING: could not delete participant devices: {e}")
    for user in ra.get("participants", []):
        try:
            hs_cli(f"users destroy -n {user} --force")
            print(f"  Deleted headscale user '{user}'.")
        except (subprocess.SubprocessError, OSError) as e:
            print(f"  WARNING: could not delete headscale user '{user}': {e}")
    try:
        policy_remove_comp(comp_id)
        print("  Removed this competition's ACL block (policy re-applied).")
    except (subprocess.SubprocessError, ValueError, OSError, SystemExit) as e:
        print(f"  WARNING: could not re-apply the headscale policy without this comp: {e}")


def verify_remote_access(state, comp_id, planned_identifiers):
    """Post-deploy check used by the verifier: returns (ok, problems[]). Config-
    presence level — a real participant-device probe is a manual step (docs)."""
    problems = []
    ra = (state or {}).get("remote_access") or {}
    if not ra.get("enabled"):
        return None, ["remote access not enabled for this deploy"]
    if ra.get("comp_id") != comp_id:
        problems.append(f"state names comp '{ra.get('comp_id')}', expected '{comp_id}'")
    try:
        found = route_conflicts(comp_id, planned_identifiers)
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return None, [f"headscale unreachable for route check: {e}"]
    if found:
        problems.append("our team subnets are ALSO advertised elsewhere: " + "; ".join(found))
    try:
        entries = hs_json("nodes list-routes")
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return None, [f"headscale unreachable for route check: {e}"]
    ours = [e for e in entries if e.get("name") == ra.get("router_node")]
    if not ours:
        problems.append(f"router node '{ra.get('router_node')}' not registered")
    else:
        serving = set(ours[0].get("subnet_routes") or [])
        for identifier in planned_identifiers:
            if team_subnet(identifier) not in serving:
                problems.append(f"team subnet 192.168.{identifier}.0/24 not SERVING on "
                                f"the router (advertised but not approved?)")
    try:
        policy_text = hs_cli("policy get", check=False).stdout
    except (subprocess.SubprocessError, OSError) as e:
        return None, [f"headscale unreachable for policy check: {e}"]
    for user in ra.get("participants", []):
        if f'"{user}@"' not in policy_text:
            problems.append(f"policy does not carry this comp's grant for {user}")
    return (not problems), problems
