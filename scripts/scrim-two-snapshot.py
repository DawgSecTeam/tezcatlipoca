#!/usr/bin/env python
"""Scrim-two pre-flight state snapshot -> logs/scrim-two-snapshot-<ts>/ (read-only)."""
import json, os, sys, time, urllib.request
REPO = os.path.expanduser("~/dev/dawgsec/tezcatlipoca")
sys.path.insert(0, REPO)
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO, ".env"))
import range_ops

OUT = os.path.join(REPO, "logs", "scrim-two-snapshot-" + time.strftime("%Y%m%dT%H%M%S"))
os.makedirs(OUT, exist_ok=True)
node = os.environ.get("TF_VAR_proxmox_node", "proxmox")

resp = range_ops.proxmox_api("GET", f"/nodes/{node}/qemu"); vms = resp["data"] if isinstance(resp, dict) else resp
with open(os.path.join(OUT, "vms.json"), "w") as f:
    json.dump(vms, f, indent=2)
interesting = {1000, 1140, *range(1150, 1156), *range(1400, 1406), 1410}
for v in sorted(vms, key=lambda x: x["vmid"]):
    if v["vmid"] in interesting:
        print(f'{v["vmid"]:>5} {v.get("name","?"):<30} {v.get("status","?")}')

state = json.load(open(os.path.join(REPO, "competitions", "scrim-one", ".deploy_state.json")))
engine_ip = "10.0.0.252"

def req(path, token=None, body=None, method=None):
    r = urllib.request.Request(f"http://{engine_ip}{path}",
                               data=json.dumps(body).encode() if body is not None else None,
                               method=method or ("POST" if body is not None else "GET"))
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(r, timeout=10) as resp:
        return json.loads(resp.read())

tok = None
try:
    tok = req("/api/login", body={"userName": "admin",
                                  "password": state["admin_password"]}).get("token")
except Exception as e:
    print("login failed:", e)

out = {}
for name, path in (("engine", "/api/engine"), ("competition", "/api/competition"),
                   ("scoreboard", "/api/scoreboard"), ("flag", "/api/flag")):
    try:
        out[name] = req(path, tok)
    except Exception as e:
        out[name] = {"_error": str(e)}
with open(os.path.join(OUT, "engine_api.json"), "w") as f:
    json.dump(out, f, indent=2)

eng = out["engine"]
print("engine:", {k: eng.get(k) for k in ("running", "paused") if isinstance(eng, dict)})
sb = out["scoreboard"]
if isinstance(sb, dict):
    print("scoreboard services:", json.dumps(sb)[:400])
print("snapshot dir:", OUT)
