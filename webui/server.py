#!/usr/bin/env python3
"""tezcatlipoca web UI — backend.

Edits the ONE canonical home of a competition, `competitions/<id>/` (Compfile, boxes.json,
box_services.json, box_vulns.json, injects/, packet.md), reads nakon's catalog for the
"add config" pickers, and runs the existing CLI drivers (create-competition.py,
verify-competition.py) as background jobs. It never reimplements deploy logic: every deploy
goes through the same entry point an operator would type, so the UI cannot drift from it.

Run from the repo root (or a practice worktree — see AGENTS.md):

    pip install -r webui/requirements.txt
    python3 webui/server.py            # http://127.0.0.1:8765

`packets/<id>/packet.yaml` is NOT edited here: it is an optional upstream input that
compile-packet.py turns into a comp dir. Once the comp dir exists it is the source of truth.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO = Path(__file__).resolve().parent.parent
COMPS = REPO / "competitions"
NAKON_DIR = REPO / "vendor" / "nakon"
DIST = Path(__file__).resolve().parent / "frontend" / "dist"
JOB_LOG_DIR = REPO / "logs" / "webui"  # logs/ is gitignored

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(NAKON_DIR))

try:  # same .env files the CLI drivers read; never overrides an exported value
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    load_dotenv(NAKON_DIR / ".env")
except ImportError:
    pass

app = FastAPI(title="tezcatlipoca")

COMP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BOX_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


# ── helpers ─────────────────────────────────────────────────────────────────────────────

def comp_dir(comp_id: str) -> Path:
    if not COMP_ID_RE.match(comp_id):
        raise HTTPException(400, f"invalid competition id {comp_id!r}")
    d = COMPS / comp_id
    if not d.is_dir():
        raise HTTPException(404, f"no competition {comp_id!r}")
    return d


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def read_compfile(path: Path) -> list:
    """Compfile is `key value` lines (utils.load_compfile). Keep order + unknown keys."""
    items = []
    try:
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s:
                continue
            key, _, value = s.partition(" ")
            items.append([key, value.strip()])
    except FileNotFoundError:
        pass
    return items


def write_compfile(path: Path, items: list):
    lines = []
    for key, value in items:
        key = str(key).strip()
        value = " ".join(str(value).split())  # one line per key, no embedded newlines
        if key and " " not in key:
            lines.append(f"{key} {value}".rstrip())
    path.write_text("\n".join(lines) + "\n")


def compfile_dict(items: list) -> dict:
    return {k: v for k, v in items}


def pin_name(pin) -> str:
    return pin if isinstance(pin, str) else pin.get("name", "")


def os_to_platform(template: str) -> str:
    # Same rule nakon_ops.os_to_platform uses; duplicated so the UI imports nothing heavy.
    return "windows" if "win" in (template or "").lower() else "linux"


def list_injects(d: Path) -> list:
    out = []
    root = d / "injects"
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        meta = read_json(p / "inject.json", None) if p.is_dir() else None
        if meta is None:
            continue
        out.append({"slug": p.name, **{k: meta.get(k) for k in (
            "title", "open_offset_min", "due_offset_min", "close_offset_min")}})
    return out


def comp_summary(d: Path) -> dict:
    cf = compfile_dict(read_compfile(d / "Compfile"))
    boxes = read_json(d / "boxes.json", [])
    services = read_json(d / "box_services.json", {})
    vulns = read_json(d / "box_vulns.json", {})
    return {
        "id": d.name,
        "name": cf.get("name") or d.name,
        "scenario": cf.get("scenario", ""),
        "difficulty": cf.get("difficulty", ""),
        "boxes": len(boxes),
        "injects": len(list_injects(d)),
        "services": sum(len(v) for v in services.values()),
        "misconfigs": sum(len(v) for v in vulns.values()),
        "modified": max((p.stat().st_mtime for p in d.iterdir()), default=0),
    }


# ── competitions ────────────────────────────────────────────────────────────────────────

@app.get("/api/comps")
def list_comps():
    out = []
    for d in sorted(COMPS.iterdir()):
        if d.is_dir() and (d / "Compfile").exists():
            out.append(comp_summary(d))
    return out


class NewComp(BaseModel):
    id: str
    name: str
    scenario: str = ""
    difficulty: int = 5


@app.post("/api/comps")
def create_comp(body: NewComp):
    if not COMP_ID_RE.match(body.id):
        raise HTTPException(400, "id may use letters, digits, '.', '_' and '-'")
    d = COMPS / body.id
    if d.exists():
        raise HTTPException(409, f"competition {body.id!r} already exists")
    d.mkdir(parents=True)
    write_compfile(d / "Compfile", [["name", body.name], ["scenario", body.scenario],
                                    ["difficulty", str(body.difficulty)]])
    write_json(d / "boxes.json", [])
    write_json(d / "box_services.json", {})
    write_json(d / "box_vulns.json", {})
    (d / "injects").mkdir()
    (d / "packet.md").write_text(f"# {body.name} — Competitor Packet\n")
    return comp_summary(d)


@app.get("/api/comps/{comp_id}")
def get_comp(comp_id: str):
    d = comp_dir(comp_id)
    return {
        **comp_summary(d),
        "compfile": read_compfile(d / "Compfile"),
        "boxes": read_json(d / "boxes.json", []),
        "box_services": read_json(d / "box_services.json", {}),
        "box_vulns": read_json(d / "box_vulns.json", {}),
        "injectList": list_injects(d),
        "deployed": (d / ".deploy_state.json").exists(),
    }


class CompfileBody(BaseModel):
    items: list


@app.put("/api/comps/{comp_id}/compfile")
def put_compfile(comp_id: str, body: CompfileBody):
    d = comp_dir(comp_id)
    write_compfile(d / "Compfile", body.items)
    return read_compfile(d / "Compfile")


# ── boxes ───────────────────────────────────────────────────────────────────────────────

BOX_FIELDS = ("name", "last_octet", "cpu", "memory_mb", "disk_gb", "disk_iface", "template",
              "unmanaged")


def clean_box(raw: dict) -> dict:
    box = {k: raw[k] for k in BOX_FIELDS if k in raw and raw[k] not in ("", None)}
    if "disk_gb" in raw and raw["disk_gb"] is None:
        box["disk_gb"] = None  # null = template disk size; keep it explicit
    if not BOX_RE.match(str(box.get("name", ""))):
        raise HTTPException(400, "box name must be lowercase letters, digits and '-'")
    try:
        octet = int(box.get("last_octet", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "last_octet must be a number")
    if not 2 <= octet <= 254:
        raise HTTPException(400, "last_octet must be 2–254 (.1 is the team gateway)")
    box["last_octet"] = octet
    if not box.get("template"):
        raise HTTPException(400, "template (operating system) is required")
    for k in ("cpu", "memory_mb", "disk_gb"):
        if box.get(k) is not None:
            box[k] = int(box[k])
    box.setdefault("cpu", 1)
    box.setdefault("memory_mb", 2048)
    return box


def check_unique(boxes: list, box: dict, skip: Optional[str]):
    for b in boxes:
        if b["name"] == skip:
            continue
        if b["name"] == box["name"]:
            raise HTTPException(409, f"a box named {box['name']!r} already exists")
        if b["last_octet"] == box["last_octet"]:
            raise HTTPException(409, f".{box['last_octet']} is already used by {b['name']}")


@app.post("/api/comps/{comp_id}/boxes")
def add_box(comp_id: str, raw: dict = Body(...)):
    d = comp_dir(comp_id)
    boxes = read_json(d / "boxes.json", [])
    box = clean_box(raw)
    check_unique(boxes, box, None)
    boxes.append(box)
    write_json(d / "boxes.json", boxes)
    for f in ("box_services.json", "box_vulns.json"):
        m = read_json(d / f, {})
        m.setdefault(box["name"], [])
        write_json(d / f, m)
    return box


@app.put("/api/comps/{comp_id}/boxes/{name}")
def update_box(comp_id: str, name: str, raw: dict = Body(...)):
    d = comp_dir(comp_id)
    boxes = read_json(d / "boxes.json", [])
    idx = next((i for i, b in enumerate(boxes) if b["name"] == name), None)
    if idx is None:
        raise HTTPException(404, f"no box {name!r}")
    box = clean_box({**boxes[idx], **raw})
    if "disk_iface" in raw and not raw["disk_iface"]:
        box.pop("disk_iface", None)
    if "unmanaged" in raw and not raw["unmanaged"]:
        box.pop("unmanaged", None)
    check_unique(boxes, box, name)
    boxes[idx] = box
    write_json(d / "boxes.json", boxes)
    if box["name"] != name:  # carry this box's pins across a rename
        for f in ("box_services.json", "box_vulns.json"):
            m = read_json(d / f, {})
            m = {(box["name"] if k == name else k): v for k, v in m.items()}
            write_json(d / f, m)
    return box


@app.delete("/api/comps/{comp_id}/boxes/{name}")
def delete_box(comp_id: str, name: str):
    d = comp_dir(comp_id)
    boxes = read_json(d / "boxes.json", [])
    if not any(b["name"] == name for b in boxes):
        raise HTTPException(404, f"no box {name!r}")
    write_json(d / "boxes.json", [b for b in boxes if b["name"] != name])
    for f in ("box_services.json", "box_vulns.json"):
        m = read_json(d / f, {})
        m.pop(name, None)
        write_json(d / f, m)
    return {"ok": True}


def normalize_pin(pin):
    """Write back in the file's own idiom: a bare name when there is nothing but a name."""
    if isinstance(pin, str):
        return pin
    if not isinstance(pin, dict) or not pin.get("name"):
        raise HTTPException(400, "each entry needs a name")
    out = {k: v for k, v in pin.items() if v not in (None, "", {}, False)}
    if "port" in out:
        out["port"] = int(out["port"])
    if isinstance(out.get("vars"), dict):
        out["vars"] = {str(k): str(v) for k, v in out["vars"].items() if str(k).strip()}
        if not out["vars"]:
            out.pop("vars")
    return out["name"] if list(out) == ["name"] else out


@app.put("/api/comps/{comp_id}/boxes/{name}/{kind}")
def put_pins(comp_id: str, name: str, kind: str, pins: list = Body(...)):
    files = {"services": "box_services.json", "misconfigs": "box_vulns.json"}
    if kind not in files:
        raise HTTPException(404)
    d = comp_dir(comp_id)
    if not any(b["name"] == name for b in read_json(d / "boxes.json", [])):
        raise HTTPException(404, f"no box {name!r}")
    m = read_json(d / files[kind], {})
    m[name] = [normalize_pin(p) for p in pins]
    write_json(d / files[kind], m)
    return m[name]


# ── injects + packet (markdown) ─────────────────────────────────────────────────────────

def inject_path(d: Path, slug: str) -> Path:
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"invalid inject slug {slug!r}")
    return d / "injects" / slug


@app.get("/api/comps/{comp_id}/injects/{slug}")
def get_inject(comp_id: str, slug: str):
    p = inject_path(comp_dir(comp_id), slug)
    meta = read_json(p / "inject.json", None)
    if meta is None:
        raise HTTPException(404, f"no inject {slug!r}")
    body_file = meta.get("description_file")
    body = (p / body_file).read_text() if body_file and (p / body_file).exists() \
        else meta.get("description", "")
    return {"slug": slug, "meta": meta, "body": body}


class InjectBody(BaseModel):
    meta: dict
    body: str


@app.put("/api/comps/{comp_id}/injects/{slug}")
def put_inject(comp_id: str, slug: str, data: InjectBody):
    p = inject_path(comp_dir(comp_id), slug)
    p.mkdir(parents=True, exist_ok=True)
    meta = dict(data.meta)
    for k in ("open_offset_min", "due_offset_min", "close_offset_min"):
        if meta.get(k) not in (None, ""):
            meta[k] = int(meta[k])
    o, du, c = (meta.get(k) for k in ("open_offset_min", "due_offset_min", "close_offset_min"))
    if None not in (o, du, c) and not o <= du <= c:
        raise HTTPException(400, "offsets must satisfy open ≤ due ≤ close")
    meta.pop("description", None)
    meta["description_file"] = meta.get("description_file") or "briefing.md"
    (p / meta["description_file"]).write_text(data.body)
    write_json(p / "inject.json", meta)
    return get_inject(comp_id, slug)


@app.delete("/api/comps/{comp_id}/injects/{slug}")
def delete_inject(comp_id: str, slug: str):
    p = inject_path(comp_dir(comp_id), slug)
    if not (p / "inject.json").exists():
        raise HTTPException(404, f"no inject {slug!r}")
    shutil.rmtree(p)
    return {"ok": True}


@app.get("/api/comps/{comp_id}/packet")
def get_packet(comp_id: str):
    p = comp_dir(comp_id) / "packet.md"
    return {"body": p.read_text() if p.exists() else ""}


class TextBody(BaseModel):
    body: str


@app.put("/api/comps/{comp_id}/packet")
def put_packet(comp_id: str, data: TextBody):
    (comp_dir(comp_id) / "packet.md").write_text(data.body)
    return {"ok": True}


# ── catalog (nakon / vulndb) ────────────────────────────────────────────────────────────

_catalog_cache: dict = {"rows": None, "at": 0.0, "error": None}
_catalog_lock = threading.Lock()
CATALOG_TTL = 300


def _open_source():
    """TEZ_WEBUI_CATALOG_FILE (a JSON array of rows) is an offline stand-in for development;
    otherwise nakon's own `auto` source: vulndb-ui over HTTP if VULNDB_UI_URL, else MySQL."""
    fixture = os.getenv("TEZ_WEBUI_CATALOG_FILE")
    if fixture:
        from nakon.catalog.source import DictCatalog
        return DictCatalog(json.loads(Path(fixture).read_text()))
    from nakon.catalog.query import open_source
    return open_source("auto")


def catalog_rows(refresh=False) -> list:
    with _catalog_lock:
        fresh = time.time() - _catalog_cache["at"] < CATALOG_TTL
        if _catalog_cache["rows"] is not None and fresh and not refresh:
            return _catalog_cache["rows"]
        try:
            from nakon.catalog.query import list_configurations
            source = _open_source()
            try:
                rows = list_configurations(source)
            finally:
                getattr(source, "close", lambda: None)()
        except Exception as e:  # unreachable vulndb is an expected state, not a crash
            raise HTTPException(503, f"catalog unavailable: {e}. Set VULNDB_UI_URL (or the "
                                     "MySQL creds) in vendor/nakon/.env.")
        _catalog_cache.update(rows=rows, at=time.time())
        return rows


@app.get("/api/catalog")
def get_catalog(platform: Optional[str] = None, kind: Optional[str] = None,
                search: str = "", refresh: bool = False):
    """kind=services → category 'service'; kind=misconfigs → everything else."""
    from constants import KNOWN_BROKEN_CONFIGS, REQUIRED_VARS
    needle = search.lower().strip()
    out = []
    for row in catalog_rows(refresh):
        if platform and row["platform"] not in (platform, "other"):
            continue
        if kind == "services" and row["category"] != "service":
            continue
        if kind == "misconfigs" and row["category"] == "service":
            continue
        if needle and needle not in f"{row['name']} {row.get('description') or ''}".lower():
            continue
        out.append({
            **{k: row[k] for k in ("id", "name", "description", "platform", "category",
                                    "type", "run_as", "depends_on")},
            "required_vars": sorted(set(row.get("required_vars") or [])
                                    | set(REQUIRED_VARS.get(row["name"], {}))),
            "identity_vars": [v for v, src in REQUIRED_VARS.get(row["name"], {}).items()
                              if src != "literal"],
            "broken": KNOWN_BROKEN_CONFIGS.get(row["name"]),
        })
    return out


@app.get("/api/catalog/{name}")
def get_catalog_entry(name: str):
    row = next((r for r in catalog_rows() if r["name"] == name), None)
    if row is None:
        raise HTTPException(404, f"{name!r} is not in the catalog")
    from constants import KNOWN_BROKEN_CONFIGS, REQUIRED_VARS
    return {**row,
            "required_vars": sorted(set(row.get("required_vars") or [])
                                    | set(REQUIRED_VARS.get(name, {}))),
            "identity_vars": [v for v, src in REQUIRED_VARS.get(name, {}).items()
                              if src != "literal"],
            "broken": KNOWN_BROKEN_CONFIGS.get(name)}


# ── templates (operating systems) + nodes ───────────────────────────────────────────────

@app.get("/api/templates")
def get_templates():
    """Live Proxmox templates when the API is reachable, plus every template any comp already
    uses (so the picker works offline). Deploy preflight is the real resolver either way."""
    used = set()
    for f in COMPS.glob("*/boxes.json"):
        try:
            used.update(b.get("template") for b in json.loads(f.read_text()) if b.get("template"))
        except (ValueError, AttributeError):
            continue
    live = []
    if os.getenv("TF_VAR_proxmox_endpoint") and os.getenv("TF_VAR_proxmox_api_token"):
        try:
            from config_ops import list_proxmox_templates
            live = list_proxmox_templates()
        except Exception:
            live = []
    names = sorted(set(live) | used)
    return [{"name": n, "platform": os_to_platform(n), "live": n in live} for n in names]


@app.get("/api/nodes")
def get_nodes():
    cfg = read_json(REPO / "nodes.json", None)
    if cfg:
        return {"multi": True, "nodes": [
            {k: n.get(k) for k in ("name", "node", "datastore", "max_teams", "weight")}
            for n in cfg.get("nodes", [])]}
    endpoint = os.getenv("TF_VAR_proxmox_endpoint", "")
    return {"multi": False, "nodes": [{"name": os.getenv("TF_VAR_proxmox_node") or "default",
                                       "endpoint": endpoint}]}


# ── jobs: deploy / plan / verify run the real CLI drivers ───────────────────────────────

JOBS: dict = {}


class DeployBody(BaseModel):
    teams: int
    scoring_vmid: Optional[int] = None
    team_node: str = ""
    engine_node: str = ""
    plan_only: bool = False


def _start_job(comp_id: str, action: str, cmd: list) -> dict:
    for j in JOBS.values():
        if j["comp"] == comp_id and j["proc"].poll() is None:
            raise HTTPException(409, f"a {j['action']} job is already running for {comp_id}")
    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log_path = JOB_LOG_DIR / f"{comp_id}-{action}-{job_id}.log"
    log = open(log_path, "w")
    log.write("$ " + " ".join(cmd) + "\n\n")
    log.flush()
    proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    JOBS[job_id] = {"id": job_id, "comp": comp_id, "action": action, "cmd": cmd,
                    "log": log_path, "proc": proc, "started": time.time()}
    return job_view(JOBS[job_id])


def job_view(j: dict) -> dict:
    rc = j["proc"].poll()
    return {"id": j["id"], "comp": j["comp"], "action": j["action"], "cmd": " ".join(j["cmd"]),
            "started": j["started"], "running": rc is None, "returncode": rc}


@app.post("/api/comps/{comp_id}/deploy")
def deploy(comp_id: str, body: DeployBody):
    comp_dir(comp_id)
    if not 1 <= body.teams <= 50:
        raise HTTPException(400, "teams must be 1–50")
    cmd = [sys.executable, "create-competition.py", "--competition", comp_id,
           "--teams", str(body.teams), "--yes"]
    if body.scoring_vmid:
        cmd += ["--scoring-vmid", str(body.scoring_vmid)]
    if body.team_node.strip():
        cmd += ["--team-node", body.team_node.strip()]
    if body.engine_node.strip():
        cmd += ["--engine-node", body.engine_node.strip()]
    if body.plan_only:
        cmd.append("--plan-only")
    return _start_job(comp_id, "plan" if body.plan_only else "deploy", cmd)


@app.post("/api/comps/{comp_id}/verify")
def verify(comp_id: str):
    comp_dir(comp_id)
    return _start_job(comp_id, "verify",
                      [sys.executable, "verify-competition.py", f"competitions/{comp_id}"])


@app.get("/api/comps/{comp_id}/jobs")
def list_jobs(comp_id: str):
    return sorted((job_view(j) for j in JOBS.values() if j["comp"] == comp_id),
                  key=lambda j: -j["started"])


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, offset: int = 0):
    j = JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "no such job")
    with open(j["log"], "rb") as f:
        f.seek(offset)
        chunk = f.read(256 * 1024)
    return {**job_view(j), "offset": offset + len(chunk),
            "text": chunk.decode("utf-8", errors="replace")}


# ── frontend ────────────────────────────────────────────────────────────────────────────

if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str) -> Any:
        if path.startswith("api/"):
            raise HTTPException(404)
        f = DIST / path
        if path and f.is_file() and DIST in f.resolve().parents:
            return FileResponse(f)
        return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("TEZ_WEBUI_HOST", "127.0.0.1"),
                port=int(os.getenv("TEZ_WEBUI_PORT", "8765")))
