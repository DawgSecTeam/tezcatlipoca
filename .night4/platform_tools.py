#!/usr/bin/env python3
"""c3rberus platform probes from the dashboard host: census and pre-bell ledger export.

The remote half is generated locally and shipped base64'd (no nested-quote corruption, and the
admin password never sits in a remote command line). Run: --census, or --export LABEL --dest DIR.
"""
import argparse
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

KEY = "/home/hna/tezcatlipoca-dashboard/lab/engine/proxmox"
# red01's IP is recycled every deploy, so its host key changes under a static known_hosts
# entry — match the engine's own ssh posture (red01_ops.ssh_argv), or a changed key can
# trip ssh's MITM guard mid-export.
SSH_OPTS = ["-F", "/dev/null", "-i", KEY, "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]


def ssh_run(ip, script, *, capture=True):
    blob = base64.b64encode(script.encode()).decode()
    argv = ["ssh"] + SSH_OPTS + [f"sysadmin@{ip}", f"echo {blob} | base64 -d | python3 -"]
    return subprocess.run(argv, capture_output=capture, text=True, timeout=300)


REMOTE = """
import collections, json, ssl, sys, urllib.request
ctx = ssl._create_unverified_context()
API = 'https://localhost:3000'


def call(path, method='GET', body=None, tok=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {'Content-Type': 'application/json'}
    if tok:
        hdr['Authorization'] = 'Bearer ' + tok
    req = urllib.request.Request(API + path, data=data, method=method, headers=hdr)
    try:
        raw = urllib.request.urlopen(req, context=ctx, timeout=timeout).read()
    except Exception as e:
        return {'_error': type(e).__name__ + ': ' + str(e)[:200]}
    try:
        return json.loads(raw)
    except Exception:
        return {'_text': raw.decode('utf-8', 'ignore')[:4000]}


TOK = call('/login', 'POST', {'username': 'admin', 'password': @PW@}, timeout=30).get('token', '')
MODE = @MODE@
if not TOK:
    print('LOGIN FAILED')
    sys.exit(3)
print('login ok')

if MODE == 'census':
    tasks = call('/c2/tasks?limit=400', tok=TOK)
    rows = call('/c2/iocs/deployed', tok=TOK)
    tl = tasks.get('tasks', []) if isinstance(tasks, dict) else tasks
    rl = rows.get('deployed', []) if isinstance(rows, dict) else rows
    print('tasks total:', len(tl))
    print('task status:', dict(collections.Counter(t.get('status') for t in tl)))
    print('task types:', dict(collections.Counter(t.get('task_type') or t.get('type')
                                                  for t in tl)))
    print('deployed rows:', len(rl))
    print('row status:', dict(collections.Counter(r.get('status') for r in rl)))
    print('active per box:', dict(collections.Counter(r.get('box_ip') for r in rl
                                                      if r.get('status') == 'active')))
else:
    import datetime, pathlib
    out = {'exported_at': datetime.datetime.utcnow().isoformat() + 'Z', 'sections': {}}
    for name, path in (('deployed', '/c2/iocs/deployed'),
                       ('tasks', '/c2/tasks?limit=400'),
                       ('scoreboard', '/scoreboard'),
                       ('history', '/scoreboard/history'),
                       ('health', '/nxc-health')):
        doc = call(path, tok=TOK)
        out['sections'][name] = doc
        inner = doc.get('deployed') or doc.get('tasks') or doc
        n = len(inner) if isinstance(inner, (list, dict)) else 0
        err = doc.get('_error') if isinstance(doc, dict) else None
        print(f"{name:<11} bytes={len(json.dumps(doc)):<8} entries={n}"
              + (f"  ERROR {err}" if err else ""))
    dest = pathlib.Path(@DEST@)
    dest.write_text(json.dumps(out))
    print('WROTE', dest, dest.stat().st_size, 'bytes')
"""


def remote_script(pw, mode, label):
    path = repr(f"/home/sysadmin/evidence-{label}.json") if mode == "export" else "''"
    body = (REMOTE.replace("@PW@", repr(pw))
                  .replace("@MODE@", repr(mode))
                  .replace("@DEST@", path))
    compile(body, "<remote>", "exec")          # never ship a broken payload
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("comp_dir")
    ap.add_argument("--census", action="store_true")
    ap.add_argument("--export", metavar="LABEL")
    ap.add_argument("--dest", metavar="DIR")
    ap.add_argument("--red-ip")
    a = ap.parse_args()
    seed = json.loads((Path(a.comp_dir) / "red" / "c3rberus-seed.json").read_text())
    ip = a.red_ip or seed.get("red_ip") or "10.0.0.247"
    pw = seed["admin_password"]
    if a.census:
        r = ssh_run(ip, remote_script(pw, "census", ""))
        sys.stdout.write(r.stdout)
        if r.returncode:
            sys.stderr.write(r.stderr[-500:])
        return r.returncode
    if a.export:
        r = ssh_run(ip, remote_script(pw, "export", a.export))
        sys.stdout.write(r.stdout)
        if r.returncode:
            sys.stderr.write(r.stderr[-500:])
            print("EXPORT SCRIPT FAILED", file=sys.stderr)
            return r.returncode
        Path(a.dest).mkdir(parents=True, exist_ok=True)
        out = Path(a.dest) / "platform-ledger.json"
        pull = subprocess.run(["ssh"] + SSH_OPTS + [f"sysadmin@{ip}",
                               f"cat /home/sysadmin/evidence-{a.export}.json"],
                              capture_output=True, text=True, timeout=180)
        out.write_text(pull.stdout)
        try:
            doc = json.loads(out.read_text())
        except Exception as e:
            print(f"PULLED FILE UNPARSEABLE ({e}) — {out.stat().st_size} bytes")
            return 1
        print(f"pulled {out} = {out.stat().st_size} bytes")
        bad = 0
        for name, inner in doc["sections"].items():
            n = len(inner.get('deployed') or inner.get('tasks') or inner or [])
            err = inner.get('_error') if isinstance(inner, dict) else None
            if err or n == 0:
                bad += 1
            print(f"  {name:<11} entries={n} {'ERROR ' + str(err) if err else ''}")
        return 1 if bad else 0
    ap.error("choose --census or --export LABEL --dest DIR")


if __name__ == "__main__":
    sys.exit(main())
