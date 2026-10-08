#!/usr/bin/env python
"""Item 12: fw01 failure drill.
stop 1410 -> poll scoreboard until ALL 12 team checks report DOWN (record time, partial/stale state)
start 1410 -> poll until 12/12 UP again WITHOUT touching engine (record convergence time)."""
import json, sys, time
from pathlib import Path
import requests
sys.path.insert(0, '/home/hna/dev/dawgsec/tezcatlipoca')
from dotenv import load_dotenv
load_dotenv('/home/hna/dev/dawgsec/tezcatlipoca/.env')
from verifier.creds import load_admin_password
import range_ops

NODE = 'proxmox'
E = 'http://10.0.0.252'
pw = load_admin_password(Path('/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one'), None)
s = requests.Session()
r = s.post(E + '/api/login', json={'username': 'admin', 'password': pw}, timeout=10)
assert r.status_code == 200

def round_state():
    e = s.get(E + '/api/engine', timeout=15).json()
    lr = e.get('last_round') or {}
    checks = lr.get('Checks') or []
    ups = sum(1 for c in checks if c.get('Result'))
    downs = sum(1 for c in checks if not c.get('Result'))
    # dump one raw check to learn the real schema once
    return lr.get('ID'), len(checks), ups, downs, (checks[0] if checks else {})

t0 = time.time()
def log(m): print(f'[{time.time()-t0:6.1f}s] {m}', flush=True)

rid, n, up, down, sample = round_state()
print('check schema sample keys:', sorted(sample.keys()))
print(f'baseline round {rid}: {n} checks up={up} down={down}')

def is_up(c):
    return bool(c.get('Result'))

def counts():
    rid, n, _, _, _ = round_state()
    e = s.get(E + '/api/engine', timeout=15).json()
    checks = (e.get('last_round') or {}).get('Checks') or []
    ups = sum(1 for c in checks if is_up(c))
    return e['last_round']['ID'], len(checks), ups

print('STOPPING fw01 (1410)')
range_ops.proxmox_api('POST', f'/nodes/{NODE}/qemu/1410/status/stop')
t_stop = time.time()
all_down_at = None
timeline = []
while time.time() - t_stop < 600:
    time.sleep(10)
    rid, n, ups = counts()
    timeline.append((round(time.time()-t_stop), rid, ups))
    log(f'round {rid}: {ups}/{n} up')
    if n and ups == 0:
        all_down_at = time.time()
        break
print('ALL DOWN at', f'{all_down_at-t_stop:.0f}s' if all_down_at else 'NEVER within 600s')

print('STARTING fw01 (1410)')
range_ops.proxmox_api('POST', f'/nodes/{NODE}/qemu/1410/status/start')
t_start = time.time()
converged_at = None
while time.time() - t_start < 900:
    time.sleep(10)
    rid, n, ups = counts()
    timeline.append((round(time.time()-t_start), rid, ups))
    log(f'round {rid}: {ups}/{n} up')
    if n and ups == n:
        converged_at = time.time()
        break
print('CONVERGED 12/12 UP at', f'{converged_at-t_start:.0f}s' if converged_at else 'NEVER within 900s')
print('engine untouched during drill: True (only proxmox API power ops were issued)')
json.dump({'all_down_after_s': None if not all_down_at else all_down_at-t_stop,
           'converged_after_s': None if not converged_at else converged_at-t_start,
           'timeline': timeline},
          open('/home/hna/dev/dawgsec/tezcatlipoca/logs/scrim-two-item12-fw-drill.json', 'w'), indent=2)
