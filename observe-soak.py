#!/usr/bin/env python3
"""observe-soak.py — read-only telemetry for a two-node competition soak.

Samples both Proxmox nodes (ssh to the hdd-150 satellite, API to the zfs-193
engine node), the Quotient engine scoreboard, and a per-node VM census into
JSONL files under the run dir. Every probe is best-effort: a failed sample is
recorded as an error line, never a crash — availability gaps are themselves
signal for the report.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

import requests
import urllib3

urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
SSH_KEY = os.path.join(HERE, "proxmox")
NODE150_SSH = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o",
               "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
               "root@10.0.0.150"]

SSH_CMD = (
    "echo LOAD $(cut -d' ' -f1-3 /proc/loadavg); "
    "echo MEMAVAIL $(awk '/MemAvailable/{print $2}' /proc/meminfo); "
    "echo MEMTOTAL $(awk '/MemTotal/{print $2}' /proc/meminfo); "
    "echo SWAPFREE $(awk '/SwapFree/{print $2}' /proc/meminfo); "
    "echo PSI_CPU $(awk '/^some/{print $3}' /proc/pressure/cpu 2>/dev/null); "
    "echo PSI_MEM $(awk '/^some/{print $3}' /proc/pressure/memory 2>/dev/null); "
    "echo PSI_IO $(awk '/^some/{print $3}' /proc/pressure/io 2>/dev/null); "
    "echo PSIFULL_MEM $(awk '/^full/{print $3}' /proc/pressure/memory 2>/dev/null); "
    "echo HDDAVAIL $(zfs list -o avail -H hdd 2>/dev/null); "
    "echo VMRUN $(qm list 2>/dev/null | awk '$3==\"running\"' | wc -l); "
    "echo ARC $(awk '$1==\"size\"{print $3}' /proc/spl/kstat/zfs/arcstats 2>/dev/null); "
    "echo ROOTFS $(df -P / | tail -1 | awk '{print $5}')"
)


def load_env():
    env = dict(os.environ)
    try:
        for line in open(ENV_PATH):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass
    return env


ENV = load_env()
OUT = {}


def emit(stream, record):
    record["wallclock"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(OUT[stream], "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def parse_ssh_block(text):
    rec = {}
    for ln in text.splitlines():
        m = re.match(r"([A-Z_]+)\s*(.*)", ln)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "LOAD":
            parts = val.split()
            if len(parts) == 3:
                rec["load1"], rec["load5"], rec["load15"] = (float(x) for x in parts)
        elif key == "MEMAVAIL":
            rec["mem_avail_gb"] = float(val) / 2 ** 20 if val else None
        elif key == "MEMTOTAL":
            rec["mem_total_gb"] = float(val) / 2 ** 20 if val else None
        elif key == "SWAPFREE":
            rec["swap_free_gb"] = float(val) / 2 ** 20 if val else None
        elif key in ("PSI_CPU", "PSI_MEM", "PSI_IO", "PSIFULL_MEM"):
            if val:
                rec.setdefault("psi", {})[key.lower()] = float(val.replace("avg60=", "") or 0)
        elif key == "HDDAVAIL":
            rec["hdd_avail_gb"] = float(val.rstrip("G")) if val else None
        elif key == "VMRUN":
            rec["vms_running"] = int(val) if val else None
        elif key == "ARC":
            rec["arc_gb"] = round(int(val) / 2 ** 30, 1) if val else None
        elif key == "ROOTFS":
            rec["rootfs_pct"] = int(val.rstrip("%")) if val else None
    return rec


def sample_node150(stop, interval):
    while not stop.is_set():
        t0 = time.time()
        rec = {"latency_ms": None, "error": None}
        try:
            r = subprocess.run(NODE150_SSH + [SSH_CMD], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                rec["error"] = f"ssh rc={r.returncode}: {(r.stderr or '').strip()[-100:]}"
            else:
                rec["latency_ms"] = int((time.time() - t0) * 1000)
                rec.update(parse_ssh_block(r.stdout))
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:100]}"
        emit("node150", rec)
        stop.wait(max(0, interval - (time.time() - t0)))


def api(path, token, params=None):
    h = {"Authorization": "PVEAPIToken=" + token}
    r = requests.get(f"https://10.0.0.193:8006/api2/json{path}", headers=h,
                     params=params, verify=False, timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def sample_node193(stop, interval):
    token = ENV.get("TF_VAR_proxmox_api_token_193", "")
    while not stop.is_set():
        t0 = time.time()
        rec = {"latency_ms": None, "error": None}
        try:
            t1 = time.time()
            st = api("/nodes/pve/status", token)
            rec["latency_ms"] = int((time.time() - t1) * 1000)
            rec["cpu_pct"] = round(st.get("cpu", 0) * 100, 1)
            rec["load1"] = round(float(st["loadavg"][0]), 2)
            rec["mem_used_gb"] = round(st["memory"]["used"] / 2 ** 30, 1)
            rec["mem_total_gb"] = round(st["memory"]["total"] / 2 ** 30, 1)
            rec["mem_free_gb"] = round(st["memory"]["free"] / 2 ** 30, 1)
            sw = st.get("swap") or {}
            rec["swap_used_gb"] = round(sw.get("used", 0) / 2 ** 30, 2)
            root = st.get("rootfs") or {}
            if root.get("total"):
                rec["rootfs_pct"] = round(100 * root["used"] / root["total"], 1)
            stg = api("/nodes/pve/storage/hdrives-zfs/status", token)
            rec["pool_avail_gb"] = round(stg.get("avail", 0) / 2 ** 30)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:100]}"
        emit("node193", rec)
        stop.wait(max(0, interval - (time.time() - t0)))


def census(stop, interval):
    tok150 = ENV.get("TF_VAR_proxmox_api_token_150", "")
    tok193 = ENV.get("TF_VAR_proxmox_api_token_193", "")
    while not stop.is_set():
        rec = {"nodes": {}}
        for name, ep, tok in (("hdd-150", "https://10.0.0.150:8006", tok150),
                              ("zfs-193", "https://10.0.0.193:8006", tok193)):
            try:
                h = {"Authorization": "PVEAPIToken=" + tok}
                d = requests.get(f"{ep}/api2/json/cluster/resources",
                                 headers=h, params={"type": "vm"}, verify=False,
                                 timeout=10).json()["data"]
                rec["nodes"][name] = [
                    {"vmid": v["vmid"], "name": v.get("name"), "status": v["status"],
                     "template": bool(v.get("template"))}
                    for v in d if v.get("node") in (rec_name(name),) or True
                ]
            except Exception as e:
                rec["nodes"][name] = f"ERROR {type(e).__name__}: {str(e)[:80]}"
        emit("census", rec)
        stop.wait(interval)


def rec_name(name):
    return {"hdd-150": "proxmox", "zfs-193": "pve"}.get(name, name)


class Engine:
    def __init__(self, ip):
        self.ip = ip
        self.s = requests.Session()
        self.team_ids = None
        self.admin_pw = None

    def _admin_pw(self):
        if self.admin_pw:
            return self.admin_pw
        try:
            st = json.load(open(os.path.join(
                HERE, "competitions", "scale8-scrim-2026-10-01", ".deploy_state.json")))
            self.admin_pw = st["admin_password"]
        except Exception:
            pass
        return self.admin_pw

    def login(self):
        pw = self._admin_pw()
        if not pw:
            return False
        r = self.s.post(f"http://{self.ip}/api/login",
                        json={"user": "admin", "password": pw}, timeout=8)
        r.raise_for_status()
        return True

    def sample(self):
        rec = {"error": None, "latency_ms": None, "scores": {}, "injects": None}
        try:
            t0 = time.time()
            if not self.s.cookies:
                self.login()
            teams = self.s.get(f"http://{self.ip}/api/teams", timeout=8).json()
            rec["latency_ms"] = int((time.time() - t0) * 1000)
            tmap = {}
            for t in (teams if isinstance(teams, list) else teams.get("teams", teams.get("data", []))):
                tmap[str(t.get("ID") or t.get("id"))] = t.get("Name") or t.get("name")
            for tid, tname in tmap.items():
                try:
                    t1 = time.time()
                    svcs = self.s.get(f"http://{self.ip}/api/services/{tid}", timeout=8).json()
                    dt = int((time.time() - t1) * 1000)
                    ups = [s for s in svcs if (s.get("Up") if "Up" in s else s.get("up"))]
                    rec["scores"][tname or tid] = {
                        "up": len(ups), "total": len(svcs), "svc_latency_ms": dt}
                except Exception as e:
                    rec["scores"][tname or tid] = f"ERR {type(e).__name__}"
            try:
                inj = self.s.get(f"http://{self.ip}/api/injects", timeout=8).json()
                inj = inj if isinstance(inj, list) else inj.get("injects", inj.get("data", []))
                rec["injects"] = {"count": len(inj),
                                  "submissions": sum(len(i.get("Submissions") or i.get("submissions") or []) for i in inj)}
            except Exception:
                pass
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            try:
                if e.response is not None and e.response.status_code in (401, 403):
                    self.s = requests.Session()
                    self.login()
                    rec.pop("error", None)
                    return self.sample()
            except Exception:
                pass
            rec["error"] = f"HTTP {status}"
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e)[:80]}"
            self.s = requests.Session()
        return rec


def sample_engine(stop, interval, engine_ip):
    eng = Engine(engine_ip)
    while not stop.is_set():
        t0 = time.time()
        rec = eng.sample()
        rec["latency_ms_total"] = int((time.time() - t0) * 1000)
        emit("engine", rec)
        stop.wait(max(0, interval - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--engine-ip", default="10.0.0.252")
    ap.add_argument("--node-interval", type=int, default=60)
    ap.add_argument("--engine-interval", type=int, default=30)
    ap.add_argument("--census-interval", type=int, default=300)
    a = ap.parse_args()
    os.makedirs(a.run_dir, exist_ok=True)
    OUT["node150"] = os.path.join(a.run_dir, "telemetry-node150.jsonl")
    OUT["node193"] = os.path.join(a.run_dir, "telemetry-node193.jsonl")
    OUT["census"] = os.path.join(a.run_dir, "telemetry-census.jsonl")
    OUT["engine"] = os.path.join(a.run_dir, "telemetry-engine.jsonl")
    stop = threading.Event()
    threads = [
        threading.Thread(target=sample_node150, args=(stop, a.node_interval), daemon=True),
        threading.Thread(target=sample_node193, args=(stop, a.node_interval), daemon=True),
        threading.Thread(target=census, args=(stop, a.census_interval), daemon=True),
        threading.Thread(target=sample_engine, args=(stop, a.engine_interval, a.engine_ip), daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"observe-soak: sampling -> {a.run_dir} (ctrl-c to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        sys.exit(0)


if __name__ == "__main__":
    main()
