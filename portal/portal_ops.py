"""Student portal, deploy side: build portal.json, ship portal/ to the engine, start it, check it.

The portal itself (portal/) is a FastAPI app the engine runs as a second docker compose project
at /opt/tez-portal, next to Quotient. Students log in with their Quotient team credentials (read
from Quotient's own event.conf, never by logging into Quotient) and get a Proxmox web
console on their team's boxes. docs/portal.md is the design doc.

Opt-in: Compfile `portal 1`. Everything here is NON-FATAL to the deploy — a portal that cannot
start records a degradation and the range is still a range.

  build_portal_config  pure: comp-dir facts -> portal.json payload
  tunnel_decision      pure: may this run start cloudflared on the stable hostname?
  deploy_portal        phase 8: mint console tokens, push, compose up, wait healthy
  check_portal         engine-side end-to-end probe (verify-competition's portal gate)
  set_access / access_state   the operator gate, without the UI
  teardown_portal      destroy path: pull access.log, revoke this run's console user
"""

import base64
import io
import json
import os
import secrets
import tarfile
import time
from pathlib import Path

import requests

from config.config_ops import write_text_atomic
from engine.engine_cmd_ops import _run_engine_cmd
from infra.console import (mint_console_tokens, node_targets, revoke_console_tokens,
                             team_node_key)
from common.timing import timed
from common.utils import compfile_flag, compfile_value, is_in_path_fw, is_unmanaged, record_degradation
from common.windows_ops import is_windows_template

PORTAL_SRC = Path(__file__).resolve().parents[1] / "portal"
REMOTE_DIR = "/opt/tez-portal"
LOCAL_PORT = 8443
# What ships to the engine: the backend files, plus the frontend SOURCE (the image builds it
# with `npm ci` against package-lock.json — node_modules/ and dist/ never ship).
_SHIP = ("Dockerfile", "compose.yaml", "requirements.txt", "auth.py", "console.py", "app.py", "ops_sync.py")
_FRONTEND_SKIP = {"node_modules", "dist"}


def portal_enabled(comp_dir):
    return bool(compfile_flag(Path(comp_dir) / "Compfile", "portal", 0))


def build_portal_config(*, comp_name, run_id, event_name, scoreboard_url, teams, boxes,
                        targets, placement, default_node, nodes, tokens,
                        firewall_console=True, public_host="", vpn=None):
# portal.json: what the portal shows and which console token reaches which box.
#
#     teams    {team_key: {identifier, ...}}            (teams.json)
#     boxes    boxes.json entries                       (template, unmanaged/in_path flags)
#     targets  enumerate/load_targets output            (team_key, box_name, vmid, ip)
#     nodes    console.node_targets output      (endpoint, pve_node, fingerprint)
#     tokens   console.mint_console_tokens      (node_key -> token); a node without a
#              token is left out of `nodes`, so its boxes list but cannot open a console
#     vpn      {server_url, teams: {team_key: [{person, key}]}}  — the remote-access
#              enrollment (phase-3 keys.json); None hides the dashboard's VPN section
#
#     Unmanaged boxes that are NOT the in-path firewall are left out (nothing a team operates);
#     the in-path firewall is listed (teams own their pfSense) unless firewall_console=False.
    by_name = {b["name"]: b for b in boxes}
    out_teams = {k: {"identifier": str(v["identifier"]), "boxes": []} for k, v in teams.items()}
    for t in targets:
        box = by_name.get(t["box_name"])
        if box is None or t["team_key"] not in out_teams:
            continue
        firewall = is_in_path_fw(box)
        if is_unmanaged(box) and not firewall:
            continue
        if firewall and not firewall_console:
            continue
        out_teams[t["team_key"]]["boxes"].append({
            "name": box["name"],
            "os": "windows" if is_windows_template(box.get("template", "")) else "linux",
            "ip": t["ip"],
            "vmid": int(t["vmid"]),
            "node": team_node_key(t, placement, default_node),
            "firewall": firewall,
        })
    out_nodes = {}
    for key, node in nodes.items():
        tok = tokens.get(key)
        if not tok:
            continue
        out_nodes[key] = {"endpoint": node["endpoint"], "pve_node": node["pve_node"],
                          "token": f"{tok['token_id']}={tok['secret']}",
                          "tls_fingerprint": node.get("tls_fingerprint") or ""}
    return {"comp": comp_name, "run_id": run_id, "event_name": event_name,
            "scoreboard_url": scoreboard_url, "public_host": public_host,
            "nodes": out_nodes, "teams": out_teams, "vpn": vpn}


def tunnel_decision(hostname, comp_name, run_id, tunnel_token, fetch=None):
# ("start" | "skip", why) for cloudflared on the stable hostname.
#
#     The tunnel token is one stable name; two connectors on it would split students across two
#     ranges at random. So before starting ours, ask the hostname who answers: nobody (offline,
#     error page, not our JSON) or THIS run -> start; another comp or run -> skip, and the
#     portal stays reachable over ssh -L only.
    if not tunnel_token:
        return "skip", "TEZ_PORTAL_TUNNEL_TOKEN not set (LAN/ssh -L only)"
    if not hostname:
        return "skip", "TEZ_PORTAL_TUNNEL_TOKEN set but TEZ_PORTAL_HOSTNAME is not"
    fetch = fetch or (lambda url: requests.get(url, timeout=10))
    try:
        r = fetch(f"https://{hostname}/healthz")
        data = r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        data = None
    if not isinstance(data, dict) or "comp" not in data:
        return "start", f"{hostname} has no live owner"
    if data.get("comp") == comp_name and data.get("run_id") == run_id:
        return "start", f"{hostname} is already this run's"
    return "skip", (f"{hostname} is owned by live range {data.get('comp')!r} "
                    f"(run {data.get('run_id')!r}) — tear that down or unset the tunnel token")


def _env_file(session_secret, open_gate, tunnel_token, cookie_secure=True,
              ops_url="", ops_token=""):
# The portal's .env on the engine.
#
#     cookie_secure gates SessionMiddleware's `https_only`. It must follow how the browser
#     actually reaches the portal: with cloudflared the public side is HTTPS (Secure is
#     correct and required), but the tunnel-less path documented in docs/portal.md is
#     `ssh -L 8443:…` over plain HTTP — and any host that is not localhost/127.0.0.1 is not
#     a secure context, so a Secure cookie is REJECTED by the browser and login is
#     impossible (caught live: an operator forwarding the portal to a LAN address
#     got 'Cookie "tez_portal" has been rejected because a non-HTTPS cookie can't be set as
#     "secure"'). Callers pass use_tunnel.
#
#     TEZ_OPS_URL/TEZ_OPS_TOKEN wire the reset-request sync to the run-queue dashboard
#     (portal/app.py ops sync): the DASHBOARD sets both in the deploy run's env, so only
#     dashboard-submitted deploys get the channel. Without them the portal still records
#     requests locally — they just never reach the operator's bell.
    lines = [f"PORTAL_SECRET_KEY={session_secret}",
             f"PORTAL_OPEN={'1' if open_gate else '0'}",
             f"PORTAL_COOKIE_SECURE={'1' if cookie_secure else '0'}"]
    if tunnel_token:
        lines.append(f"TUNNEL_TOKEN={tunnel_token}")
    if ops_url and ops_token:
        lines.append(f"TEZ_OPS_URL={ops_url}")
        lines.append(f"TEZ_OPS_TOKEN={ops_token}")
    return "\n".join(lines) + "\n"


def bundle(config, env_text, src=PORTAL_SRC):
# The tarball shipped to REMOTE_DIR: portal sources + portal.json + .env (0600).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add_bytes(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            tar.addfile(info, io.BytesIO(data))
        for name in _SHIP:
            add_bytes(name, (src / name).read_bytes())
        front = src / "frontend"
        for path in sorted(front.rglob("*")):
            rel = path.relative_to(front)
            if path.is_file() and not (_FRONTEND_SKIP & set(rel.parts)):
                add_bytes(f"frontend/{rel.as_posix()}", path.read_bytes())
        add_bytes("portal.json", json.dumps(config, indent=2).encode(), 0o600)
        add_bytes(".env", env_text.encode(), 0o600)
    return buf.getvalue()


def deploy_portal(ctx, env=os.environ):
# Phase 8 tail: mint console tokens, write portal.json, ship and start the portal.
#
#     Idempotent on resume: tokens and the session secret are carried in the state document,
#     the gate state under /opt/tez-portal/state survives a re-push, and compose up only
#     recreates what changed. Never raises — every failure becomes a degradation.
    if not portal_enabled(ctx.comp_dir):
        return
    print("  Student portal (Compfile `portal 1`)...")
    try:
        with timed(ctx.comp_dir, 8, "portal"):
            _deploy_portal(ctx, env)
    except Exception as e:  # noqa: BLE001 - the portal must never fail the range
        print(f"  WARNING: student portal did not come up ({type(e).__name__}: {e})")
        record_degradation("student portal", f"{type(e).__name__}: {e}")
        ctx.state["portal_up"] = False
        ctx.save_state()


def vpn_config(comp_dir, teams, env):
# The dashboard's VPN enrollment data — the phase-3 remote-access artifact plus the login
#     server. keys.json is keyed by identifier (its "team" is the identifier string); None
#     (remote access off, or no roster) hides the dashboard's VPN section.
    server_url = env.get("TEZ_HEADSCALE_URL", "")
    keys_path = Path(comp_dir) / "remote-access" / "keys.json"
    if not (keys_path.exists() and server_url):
        return None
    by_id = {str(v["identifier"]): k for k, v in teams.items()}
    people = {}
    for e in json.loads(keys_path.read_text()):
        tk = by_id.get(str(e["team"]))
        if tk:
            people.setdefault(tk, []).append({"person": e["person"], "key": e["key"]})
    return {"server_url": server_url, "teams": people}


def _deploy_portal(ctx, env):
    nodes = node_targets(ctx.all_targets, ctx.placement, ctx.node)
    previous = ctx.state.get("portal_console_tokens") or {}
    minted, problems = mint_console_tokens(ctx.comp_name, ctx.run_id, nodes,
                                           previous=previous, env=env)
    # A node that failed THIS attempt keeps the token an earlier attempt of the same run
    # recorded (most failures here are transient API trouble, and that token still works).
    tokens = {k: v for k, v in {**previous, **minted}.items() if k in nodes}
    ctx.state["portal_console_tokens"] = tokens
    ctx.state.setdefault("portal_session_secret", secrets.token_hex(32))
    ctx.save_state()
    for p in problems:
        print(f"    console token: {p}")
        record_degradation("student portal console token", p)
    if not tokens:
        print("    WARNING: no console token on any node — the portal runs with logins and "
              "box lists only (docs/portal.md → Operating it)")

    hostname = env.get("TEZ_PORTAL_HOSTNAME", "")
    tunnel_token = env.get("TEZ_PORTAL_TUNNEL_TOKEN", "")
    verdict, why = tunnel_decision(hostname, ctx.comp_name, ctx.run_id, tunnel_token)
    print(f"    tunnel: {verdict} ({why})")
    if verdict == "skip" and tunnel_token:
        record_degradation("student portal tunnel", why)
    use_tunnel = verdict == "start"

    vpn = vpn_config(ctx.comp_dir, ctx.teams, env)

    config = build_portal_config(
        comp_name=ctx.comp_name, run_id=ctx.run_id, event_name=ctx.name,
        scoreboard_url=compfile_value(ctx.comp_dir / "Compfile", "portal_scoreboard_url",
                                      f"http://{ctx.scoring_ip}"), teams=ctx.teams, boxes=ctx.boxes,
        targets=ctx.all_targets, placement=ctx.placement, default_node=ctx.node,
        nodes=nodes, tokens=tokens,
        firewall_console=bool(compfile_flag(ctx.comp_dir / "Compfile",
                                            "portal_firewall_console", 1)),
        public_host=hostname if use_tunnel else "", vpn=vpn)
    write_text_atomic(ctx.comp_dir / "portal.json", json.dumps(config, indent=2))

    tar = bundle(config, _env_file(ctx.state["portal_session_secret"],
                                   env.get("TEZ_PORTAL_OPEN") == "1",
                                   tunnel_token if use_tunnel else "",
                                   cookie_secure=use_tunnel,
                                   ops_url=env.get("TEZ_OPS_URL", ""),
                                   ops_token=env.get("TEZ_OPS_TOKEN", "")))
    _run_engine_cmd(ctx.tf_ctx,
                    f"sudo mkdir -p {REMOTE_DIR}/state && sudo tar xzf - -C {REMOTE_DIR} && "
                    f"sudo chmod 600 {REMOTE_DIR}/portal.json {REMOTE_DIR}/.env",
                    input=tar, timeout=60, step="portal push")
    profile = "--profile tunnel " if use_tunnel else ""
    if not use_tunnel:
        # A re-run that lost the hostname must not leave an earlier run's connector up.
        _run_engine_cmd(ctx.tf_ctx, f"cd {REMOTE_DIR} && sudo docker compose --profile tunnel "
                        "rm -sf cloudflared >/dev/null 2>&1 || true",
                        timeout=120, step="portal tunnel stop")
    # `restart portal` after `up`: portal.json is a bind mount read once at startup, and
    # compose does not recreate a container whose image and service config are unchanged —
    # without it a re-push (resume, re-created token) would keep serving the old config.
    _run_engine_cmd(ctx.tf_ctx, f"cd {REMOTE_DIR} && sudo docker compose {profile}up -d --build "
                    f"&& sudo docker compose {profile}restart portal",
                    timeout=900, step="portal compose up")
    _run_engine_cmd(ctx.tf_ctx,
                    f"for i in $(seq 60); do curl -fsS http://127.0.0.1:{LOCAL_PORT}/healthz "
                    f">/dev/null 2>&1 && exit 0; sleep 2; done; "
                    f"sudo docker compose -f {REMOTE_DIR}/compose.yaml logs --tail 40 portal; exit 1",
                    timeout=180, step="portal healthz")

    ctx.state["portal_up"] = True
    ctx.state["portal_url"] = (f"https://{hostname}" if use_tunnel else
                               f"http://127.0.0.1:{LOCAL_PORT} via ssh -L "
                               f"{LOCAL_PORT}:127.0.0.1:{LOCAL_PORT} on the engine")
    ctx.save_state()
    consoles = sum(1 for t in config["teams"].values() for b in t["boxes"]
                   if b["node"] in config["nodes"])
    print(f"    portal up: {ctx.state['portal_url']} "
          f"({consoles} console-capable box(es), gate "
          f"{'OPEN' if env.get('TEZ_PORTAL_OPEN') == '1' else 'closed until an admin opens it'})")



def _gate_cmd(is_open, who):
    data = json.dumps({"open": bool(is_open), "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "opened_by": who})
    b64 = base64.b64encode(data.encode()).decode()
    return (f"sudo mkdir -p {REMOTE_DIR}/state && echo {b64} | base64 -d | "
            f"sudo tee {REMOTE_DIR}/state/access.json.tmp >/dev/null && "
            f"sudo mv {REMOTE_DIR}/state/access.json.tmp {REMOTE_DIR}/state/access.json")


def set_access(tf_ctx, is_open, who="operator-cli"):
# Open/close team console access without the UI (the portal re-reads it per request).
    _run_engine_cmd(tf_ctx, _gate_cmd(is_open, who), timeout=30, step="portal gate")


def access_state(tf_ctx):
    r = _run_engine_cmd(tf_ctx, f"curl -fsS http://127.0.0.1:{LOCAL_PORT}/healthz",
                        check=False, capture=True, timeout=30, step="portal healthz")
    try:
        return json.loads(r.stdout)
    except (ValueError, TypeError):
        return None



# Runs ON the engine (stdlib only — the engine's python3 has no websockets/requests), against
# the portal's 127.0.0.1 listener. Prints one JSON list of {check, ok, detail}.
_CHECK_SCRIPT = r'''
import base64, json, os, socket, ssl, sys, threading, urllib.error, urllib.request
A = json.loads(base64.b64decode(sys.argv[1]))
PORT = A["port"]
BASE = f"http://127.0.0.1:{PORT}"
PUBLIC = f"https://{A['public_host']}" if A.get("public_host") else ""
out = []
def rec(check, ok, detail=""):
    out.append({"check": check, "ok": bool(ok), "detail": str(detail)[:200]})
def req(path, body=None, cookie=None, base=None):
    data = None if body is None else json.dumps(body).encode()
    try:
        r = urllib.request.Request((base or BASE) + path, data=data,
                                   method="GET" if body is None else "POST")
        if body is not None:
            r.add_header("Content-Type", "application/json")
        if cookie:
            r.add_header("Cookie", cookie)
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.headers, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, e.headers, payload
    except (OSError, ValueError) as e:
        return 0, {}, {"detail": str(e)}
def login(user, pw, base=None):
    st, h, _ = req("/api/login", {"username": user, "password": pw, "display": "verify"},
                   base=base)
    return st, (h.get("Set-Cookie", "").split(";", 1)[0] if st == 200 else None)
def ws_greeting(path, cookie, host="127.0.0.1", port=PORT, tls=False):
    s = socket.create_connection((host, port), timeout=20)
    if tls:
        s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    host_header = host if port == 443 else f"{host}:{port}"
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: binary\r\nCookie: {cookie}\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, data = buf.partition(b"\r\n\r\n")
    parts = head.split(b" ", 2)
    status = parts[1].decode() if len(parts) > 1 else "?"
    if status != "101":
        s.close()
        return status, b""
    while len(data) < 14:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    if len(data) < 2:
        return status, b""
    n, off = data[1] & 0x7F, 2
    if n == 126:
        n, off = int.from_bytes(data[2:4], "big"), 4
    return status, data[off:off + n]

st, _, hz = req("/healthz")
rec("healthz", st == 200 and hz.get("comp") == A["comp"], f"{st} {hz}")
gate_open = bool(hz.get("open"))
for node in A.get("missing_nodes") or []:
    rec(f"console {node}", False, f"no team box sample on node {node}")
teams = A["teams"]
first = teams[0]
if PUBLIC:
    st, _, pub_hz = req("/healthz", base=PUBLIC)
    rec("tunnel healthz", st == 200 and pub_hz.get("comp") == A["comp"], f"{st} {pub_hz}")
    st, _ = login(first["team"], first["password"], base=PUBLIC)
    rec("tunnel team login", st == 200, f"{first['team']} via {A['public_host']}: HTTP {st}")
    target = (A.get("relay_targets") or [None])[0]
    if target:
        st, pub_admin = login("admin", A["admin_password"], base=PUBLIC)
        if st != 200:
            rec("tunnel console relay", False, f"admin login HTTP {st}")
        else:
            st, _, minted = req("/api/console", {"team": target["team"], "box": target["box"]},
                                cookie=pub_admin, base=PUBLIC)
            if st != 200:
                rec("tunnel console relay", False, f"mint HTTP {st} {minted.get('detail', '')}")
            else:
                try:
                    ws_status, greeting = ws_greeting(minted["ws_path"], pub_admin,
                                                      host=A["public_host"], port=443, tls=True)
                    detail = f"wss {ws_status} greeting {greeting[:12]!r}"
                except OSError as e:
                    ws_status, greeting, detail = "error", b"", f"relay: {e}"
                rec("tunnel console relay",
                    ws_status == "101" and greeting.startswith(b"RFB 003."), detail)
st, team_cookie = login(first["team"], first["password"])
rec("team login", st == 200, f"{first['team']}: HTTP {st}")
if team_cookie:
    st, _, me = req("/api/me", cookie=team_cookie)
    names = sorted(b["name"] for b in me.get("boxes", []))
    rec("team sees own boxes", st == 200 and names == sorted(first["boxes"]), names)
    if first["boxes"]:
        st, _, _ = req("/api/console", {"box": first["boxes"][0]}, cookie=team_cookie)
        want = 200 if gate_open else 423
        rec("team gate", st == want, f"gate {'open' if gate_open else 'closed'} -> HTTP {st}")
    if len(teams) > 1 and teams[1]["boxes"]:
        st, _, _ = req("/api/console", {"team": teams[1]["team"], "box": teams[1]["boxes"][0]},
                       cookie=team_cookie)
        rec("cross-team refused", st == 403, f"{first['team']} -> {teams[1]['team']}: HTTP {st}")
st, admin_cookie = login("admin", A["admin_password"])
rec("admin login", st == 200, f"HTTP {st}")
if admin_cookie:
    targets = A.get("relay_targets") or []
    probes = {}
    def probe(target):
        label = f"console {target['node']} ({target['team']}/{target['box']})"
        st, _, minted = req("/api/console", {"team": target["team"], "box": target["box"]},
                            cookie=admin_cookie)
        if st != 200:
            probes[label] = (None, False, f"mint HTTP {st} {minted.get('detail', '')}")
            return
        try:
            ws_status, greeting = ws_greeting(minted["ws_path"], admin_cookie)
        except OSError as e:
            probes[label] = (None, False, f"relay: {e}")
            return
        probes[label] = (minted["ws_path"],
                         ws_status == "101" and greeting.startswith(b"RFB 003."),
                         f"ws {ws_status} greeting {greeting[:12]!r}")
    if A.get("concurrency", 1) > 1 and len(targets) > 1:
        workers = [threading.Thread(target=probe, args=(t,)) for t in targets]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
    else:
        for t in targets:
            probe(t)
    for t in targets:
        label = f"console {t['node']} ({t['team']}/{t['box']})"
        ws_path, ok, detail = probes[label]
        rec(label, ok, detail)
        if ws_path:
            try:
                again, _ = ws_greeting(ws_path, admin_cookie)
            except OSError:
                again = "closed"
            rec(f"relay id single-use ({t['node']} {t['team']}/{t['box']})",
                again != "101", f"reuse -> {again}")
print(json.dumps(out))
'''


def check_args(comp_name, teams, admin_password, portal_config, concurrency=1):
# The probe's input: per-team box names (from portal.json, the portal's own view), the
#     team/admin credentials, the public host when the tunnel is up, and one sample box per
#     console node. Every node gets a sample (a node with none is a named FAIL); concurrency
#     adds boxes beyond the per-node samples so N relays can open at once.
    nodes = portal_config.get("nodes") or {}
    t_list, first_box, extras = [], {}, []
    for team_key, data in (portal_config.get("teams") or {}).items():
        names = [b["name"] for b in data["boxes"]]
        t_list.append({"team": team_key, "password": teams[team_key]["password"],
                       "boxes": names})
        for b in data["boxes"]:
            if b["node"] in nodes:
                sample = {"node": b["node"], "team": team_key, "box": b["name"]}
                first_box.setdefault(b["node"], sample)
                extras.append(sample)
    samples = list(first_box.values())
    targets = list(samples)
    if concurrency > len(targets):
        seen = {(t["team"], t["box"]) for t in targets}
        for t in extras:
            if len(targets) >= concurrency:
                break
            if (t["team"], t["box"]) not in seen:
                targets.append(t)
                seen.add((t["team"], t["box"]))
    return {"comp": comp_name, "port": LOCAL_PORT, "teams": t_list,
            "public_host": portal_config.get("public_host") or "",
            "admin_password": admin_password, "node_samples": samples,
            "relay_targets": targets,
            "missing_nodes": [n for n in nodes if n not in first_box],
            "concurrency": concurrency}


def check_portal(run_engine, comp_name, teams, admin_password, portal_config, concurrency=1):
# Run the probe on the engine. `run_engine(cmd) -> (rc, stdout)`. Returns the probe's
#     result list, or raises RuntimeError when the probe could not run at all.
    args = base64.b64encode(json.dumps(
        check_args(comp_name, teams, admin_password, portal_config,
                   concurrency)).encode()).decode()
    script = base64.b64encode(_CHECK_SCRIPT.encode()).decode()
    rc, out = run_engine(f"echo {script} | base64 -d > /tmp/tez-portal-check.py && "
                         f"python3 /tmp/tez-portal-check.py {args}; "
                         f"rc=$?; rm -f /tmp/tez-portal-check.py; exit $rc")
    last = (out or "").strip().splitlines()[-1:] or [""]
    try:
        results = json.loads(last[0])
    except ValueError:
        raise RuntimeError(f"portal probe produced no result (rc={rc}): {(out or '')[-200:]}")
    return results



def teardown_portal(comp_dir, comp_name, run_id, state, teams, boxes, placement, default_node,
                    tf_ctx=None, env=os.environ):
# Destroy-path step: copy access.log into the comp dir (artifact collection picks it up),
#     then delete exactly this run's console user on every node. Warns and proceeds — a dead
#     engine never blocks a teardown; the user deletion is retried by the next destroy run.
    # The Compfile flag counts too: a crash between minting a user and saving the state
    # leaves a user the state does not record — the deterministic name still finds it.
    if not (state.get("portal_console_tokens") or state.get("portal_up")
            or portal_enabled(comp_dir)):
        return []
    if tf_ctx:
        try:
            r = _run_engine_cmd(tf_ctx, f"sudo cat {REMOTE_DIR}/state/access.log",
                                check=False, capture=True, timeout=30, step="portal log")
            if r.returncode == 0 and r.stdout:
                write_text_atomic(Path(comp_dir) / "portal-access.log", r.stdout, mode=0o600)
                print(f"  Portal access log saved to competitions/{comp_name}/portal-access.log")
        except Exception as e:  # noqa: BLE001
            print(f"  WARNING: could not fetch the portal access log ({e})")
    from common.targets import load_targets

    nodes = node_targets(load_targets(comp_dir, teams, boxes), placement, default_node)
    problems = revoke_console_tokens(comp_name, run_id, nodes, env=env)
    for p in problems:
        print(f"  WARNING: portal console user not removed — {p} (re-run the destroy)")
    return problems
