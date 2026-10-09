"""Student range portal: Quotient team login -> Proxmox web console on the team's own boxes.

Runs per competition on the scoring engine (portal_ops.deploy_portal), published on 127.0.0.1
only and reached through the Cloudflare tunnel (or `ssh -L` for operators). docs/portal.md has
the full design; the load-bearing rules are:

  - Isolation is enforced HERE. The console token can open any team VM of the competition, so
    every mint resolves the box from the SESSION's team (a team session can never name another
    team), and relay ids are single-use and bound to the minting session.
  - The gate: team consoles stay closed (423) until an admin opens access; admins bypass it.
  - Never log into Quotient (one session per account) — portal/auth.py reads event.conf.

Environment (set by portal_ops in /opt/tez-portal/.env and compose.yaml):
  PORTAL_CONFIG      portal.json (boxes, nodes, console tokens)       default /config/portal.json
  PORTAL_EVENT_CONF  Quotient's event.conf, mounted read-only         default /config/event.conf
  PORTAL_STATE_DIR   access.json (gate) + access.log                  default /state
  PORTAL_SECRET_KEY  session-cookie signing key (required)
  PORTAL_OPEN        1 = gate starts open when no access.json exists yet
  PORTAL_COOKIE_SECURE  0 to allow the cookie over plain http (default 1)
  PORTAL_DIST        the built frontend (portal/frontend, Vite)       default ./frontend/dist
"""

import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

try:  # package import (tests) or flat import (the container runs `uvicorn app:app`)
    from portal.auth import EventConfAuth
    from portal.console import ConsoleBroker, ConsoleError
except ImportError:  # pragma: no cover - container layout
    from auth import EventConfAuth
    from console import ConsoleBroker, ConsoleError

DIST_DIR = Path(__file__).resolve().parent / "frontend" / "dist"
SESSION_MAX_AGE = 12 * 3600
LOGIN_WINDOW = 300       # seconds
LOGIN_MAX_FAILS = 10     # per client IP per window


def _client_ip(request):
    # The portal listens on 127.0.0.1 only: every request arrives via cloudflared (which sets
    # CF-Connecting-IP) or an operator's ssh -L. Nothing else can reach it to forge the header.
    return (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "?"))


class AccessGate:
    """state/access.json: {open, opened_at, opened_by}. Read per request (it is tiny and the
    CLI toggle in portal_ops writes it out-of-band)."""

    def __init__(self, state_dir, default_open=False):
        self.path = Path(state_dir) / "access.json"
        self.default_open = default_open
        self._lock = threading.Lock()

    def get(self):
        try:
            data = json.loads(self.path.read_text())
            return {"open": bool(data.get("open")), "opened_at": data.get("opened_at"),
                    "opened_by": data.get("opened_by")}
        except (OSError, ValueError):
            return {"open": self.default_open, "opened_at": None, "opened_by": None}

    def set(self, is_open, who):
        data = {"open": bool(is_open), "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "opened_by": who}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        return data


class AccessLog:
    """JSON-lines audit trail: logins, console opens/closes, gate flips. Collected into
    .automated-tests/<run-id>/ at teardown."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / "access.log"
        self._lock = threading.Lock()

    def write(self, event, **fields):
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields})
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(line + "\n")
        except OSError:
            pass  # an unwritable log must never take the console down


class LoginLimiter:
    def __init__(self, window=LOGIN_WINDOW, max_fails=LOGIN_MAX_FAILS, clock=time.monotonic):
        self.window, self.max_fails, self.clock = window, max_fails, clock
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, key):
        with self._lock:
            q = self._fails[key]
            cutoff = self.clock() - self.window
            while q and q[0] < cutoff:
                q.popleft()
            return len(q) >= self.max_fails

    def fail(self, key):
        with self._lock:
            self._fails[key].append(self.clock())


def _public_box(box):
    return {"name": box["name"], "os": box.get("os", "linux"), "ip": box.get("ip"),
            "firewall": bool(box.get("firewall"))}


def create_app(config_path, event_conf_path, state_dir, secret_key, *, broker=None,
               default_open=False, cookie_secure=True, dist_dir=DIST_DIR, limiter=None):
    config = json.loads(Path(config_path).read_text())
    teams = config.get("teams") or {}
    auth = EventConfAuth(event_conf_path)
    gate = AccessGate(state_dir, default_open=default_open)
    log = AccessLog(state_dir)
    limiter = limiter or LoginLimiter()
    broker = broker if broker is not None else ConsoleBroker(config.get("nodes") or {})

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secret_key, session_cookie="tez_portal",
                       max_age=SESSION_MAX_AGE, same_site="lax", https_only=cookie_secure)

    def session(request):
        s = request.session
        if not s.get("role") or time.time() - s.get("iat", 0) > SESSION_MAX_AGE:
            return None
        if s["role"] == "team" and s.get("team") not in teams:
            return None
        return s

    def err(status, msg):
        # `detail`, like FastAPI's own errors and webui/server.py — the frontend's api.js
        # (shared shape with webui/frontend/src/api.js) reads exactly that key.
        return JSONResponse({"detail": msg}, status_code=status)

    def same_origin(request):
        # SameSite=Lax already stops cross-site POSTs carrying the cookie; this is the belt to
        # that pair of braces for browsers that treat top-level POSTs leniently.
        origin = request.headers.get("origin")
        if not origin:
            return True
        origin_host = origin.split("://", 1)[-1]
        # portal.json's public_host (the tunnel hostname) is accepted explicitly, so a proxy
        # that rewrites Host on the way in cannot turn every student login into a 403.
        allowed = {request.headers.get("x-forwarded-host"), request.headers.get("host"),
                   config.get("public_host")}
        return origin_host in {h for h in allowed if h}

    def resolve_box(s, team_req, box_name):
        """(team_key, box) the session may open, or an error response."""
        if s["role"] == "team":
            if team_req and team_req != s["team"]:
                return None, err(403, "not your team")
            team_key = s["team"]
        else:
            team_key = team_req
            if team_key not in teams:
                return None, err(404, "unknown team")
        box = next((b for b in teams[team_key]["boxes"] if b["name"] == box_name), None)
        if box is None:
            # Same answer whether the box exists on another team or nowhere: a team session
            # learns nothing about other teams' lineups from this endpoint.
            return None, err(403 if s["role"] == "team" else 404, "no such box on this team")
        return (team_key, box), None

    @app.get("/healthz")
    def healthz():
        return {"comp": config.get("comp"), "run_id": config.get("run_id"),
                "event_name": config.get("event_name"),
                "open": gate.get()["open"], "console": broker.available()}

    @app.post("/api/login")
    async def login(request: Request):
        if not same_origin(request):
            return err(403, "cross-origin request refused")
        ip = _client_ip(request)
        if limiter.blocked(ip):
            log.write("login_blocked", ip=ip)
            return err(429, "too many failed logins — wait a few minutes")
        try:
            body = await request.json()
        except ValueError:
            return err(400, "expected JSON")
        username = str(body.get("username") or "").strip()
        display = str(body.get("display") or "").strip()[:40]
        who = auth.check(username, str(body.get("password") or ""))
        if who is None:
            limiter.fail(ip)
            log.write("login_failed", user=username[:40], ip=ip)
            return err(401, "wrong team name or password")
        role, name = who
        if role == "team" and name not in teams:
            log.write("login_failed", user=name, ip=ip, why="team has no boxes in portal.json")
            return err(403, "this team has no boxes in this competition")
        request.session.clear()
        request.session.update({"role": role, "team": name if role == "team" else None,
                                "user": name, "display": display, "iat": int(time.time())})
        log.write("login", user=name, role=role, display=display, ip=ip)
        return {"ok": True, "role": role}

    @app.post("/api/logout")
    async def logout(request: Request):
        s = session(request)
        if s:
            log.write("logout", user=s["user"], display=s.get("display"))
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request):
        s = session(request)
        if s is None:
            return err(401, "not logged in")
        out = {"role": s["role"], "user": s["user"], "display": s.get("display"),
               "event_name": config.get("event_name"),
               "scoreboard_url": config.get("scoreboard_url"),
               "access": gate.get(), "console": broker.available()}
        if s["role"] == "team":
            out["team"] = s["team"]
            out["boxes"] = [_public_box(b) for b in teams[s["team"]]["boxes"]]
        else:
            out["teams"] = {k: [_public_box(b) for b in v["boxes"]] for k, v in teams.items()}
        return out

    @app.post("/api/console")
    async def console(request: Request):
        if not same_origin(request):
            return err(403, "cross-origin request refused")
        s = session(request)
        if s is None:
            return err(401, "not logged in")
        try:
            body = await request.json()
        except ValueError:
            return err(400, "expected JSON")
        picked, problem = resolve_box(s, body.get("team"), str(body.get("box") or ""))
        if problem:
            log.write("console_refused", user=s["user"], box=body.get("box"),
                      team=body.get("team"), why=problem.status_code)
            return problem
        team_key, box = picked
        if s["role"] == "team" and not gate.get()["open"]:
            return err(423, "box access has not opened yet")
        if not broker.available(box):
            return err(503, "console access is not configured for this competition")
        owner = f"{s['user']}:{s['iat']}"
        try:
            minted = await broker.mint(box, owner)
        except ConsoleError as e:
            log.write("console_error", user=s["user"], team=team_key, box=box["name"],
                      error=str(e)[:200])
            return err(502, f"could not open the console: {e}")
        log.write("console_open", user=s["user"], display=s.get("display"), team=team_key,
                  box=box["name"])
        return {"relay_id": minted["relay_id"], "password": minted["password"],
                "ws_path": f"/console/ws/{minted['relay_id']}", "box": _public_box(box),
                "team": team_key}

    @app.websocket("/console/ws/{relay_id}")
    async def console_ws(websocket: WebSocket, relay_id: str):
        s = websocket.session
        valid = (s.get("role") and time.time() - s.get("iat", 0) <= SESSION_MAX_AGE)
        entry = broker.take(relay_id, f"{s.get('user')}:{s.get('iat')}") if valid else None
        if entry is None:
            await websocket.close(code=4403)
            return
        wanted = websocket.scope.get("subprotocols") or []
        await websocket.accept(subprotocol="binary" if "binary" in wanted else None)
        started = time.time()
        try:
            await broker.relay(websocket, entry)
        except Exception as e:  # noqa: BLE001 - logged; the browser sees the close
            log.write("console_error", user=s.get("user"), box=entry.get("box"),
                      error=f"{type(e).__name__}: {e}"[:200])
        finally:
            log.write("console_close", user=s.get("user"), box=entry.get("box"),
                      seconds=int(time.time() - started))
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    def admin_session(request):
        s = session(request)
        if s is None:
            return None, err(401, "not logged in")
        if s["role"] != "admin":
            return None, err(403, "admin only")
        return s, None

    @app.get("/api/admin/access")
    def access_get(request: Request):
        _s, problem = admin_session(request)
        return problem or gate.get()

    @app.post("/api/admin/access")
    async def access_set(request: Request):
        if not same_origin(request):
            return err(403, "cross-origin request refused")
        s, problem = admin_session(request)
        if problem:
            return problem
        try:
            body = await request.json()
        except ValueError:
            return err(400, "expected JSON")
        data = gate.set(bool(body.get("open")), s["user"])
        log.write("gate", user=s["user"], open=data["open"])
        return data

    @app.get("/api/admin/teams")
    def admin_teams(request: Request):
        _s, problem = admin_session(request)
        if problem:
            return problem
        return {k: {"identifier": v.get("identifier"),
                    "boxes": [_public_box(b) for b in v["boxes"]]} for k, v in teams.items()}

    @app.get("/api/admin/log")
    def admin_log(request: Request):
        _s, problem = admin_session(request)
        if problem:
            return problem
        try:
            lines = log.path.read_text().splitlines()[-200:]
        except OSError:
            lines = []
        return {"entries": [json.loads(x) for x in lines if x.strip()]}

    # The SPA, served the way webui/server.py serves its build: /assets from dist, every other
    # GET that is not an API route falls through to index.html (react-router owns / and
    # /console/<team>/<box>). Registered last so it can never shadow a route above.
    dist = Path(dist_dir) if dist_dir else None
    if dist and dist.is_dir():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith(("api/", "console/ws/")) or path == "healthz":
                raise HTTPException(404)
            f = dist / path
            if path and f.is_file() and dist.resolve() in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")
    return app


def app_from_env():
    secret = os.environ.get("PORTAL_SECRET_KEY")
    if not secret:
        raise SystemExit("PORTAL_SECRET_KEY is required")
    return create_app(
        os.environ.get("PORTAL_CONFIG", "/config/portal.json"),
        os.environ.get("PORTAL_EVENT_CONF", "/config/event.conf"),
        os.environ.get("PORTAL_STATE_DIR", "/state"),
        secret,
        default_open=os.environ.get("PORTAL_OPEN") == "1",
        cookie_secure=os.environ.get("PORTAL_COOKIE_SECURE", "1") != "0",
        dist_dir=os.environ.get("PORTAL_DIST") or DIST_DIR,
    )


if os.environ.get("PORTAL_SECRET_KEY"):  # container entrypoint: `uvicorn app:app`
    app = app_from_env()
