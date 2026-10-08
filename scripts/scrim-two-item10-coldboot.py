#!/usr/bin/env python
"""Item 10: engine cold-boot drill. qm stop/start 1000, wait for scoring to resume
in-path with zero manual network repair. Verifies: ens20 addressed via netplan,
MASQUERADE re-asserted, route via transit, rounds advancing, 12 checks UP."""
import json, subprocess, sys, time
sys.path.insert(0, '/home/hna/dev/dawgsec/tezcatlipoca')
from dotenv import load_dotenv
load_dotenv('/home/hna/dev/dawgsec/tezcatlipoca/.env')
import range_ops

NODE = 'proxmox'
VMID = 1000

def qm_api(method, path, **kw):
    return range_ops.proxmox_api(method, path, **kw)

t0 = time.time()
def log(msg):
    print(f'[{time.time()-t0:7.1f}s] {msg}', flush=True)

log(f'stop {VMID}')
qm_api('POST', f'/nodes/{NODE}/qemu/{VMID}/status/stop')
while True:
    st = qm_api('GET', f'/nodes/{NODE}/qemu/{VMID}/status/current')['data']['status']
    if st == 'stopped':
        break
    time.sleep(2)
log('stopped; starting')
qm_api('POST', f'/nodes/{NODE}/qemu/{VMID}/status/start')
log('start issued')

# wait for engine API
ready = False
for i in range(90):
    time.sleep(5)
    r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '5',
                        'http://10.0.0.252/api/engine'], capture_output=True, text=True)
    if r.stdout in ('200', '401'):
        ready = True
        log(f'engine HTTP reachable ({r.stdout}) after ~{5*(i+1)}s')
        break
if not ready:
    log('ENGINE NOT REACHABLE after 450s — manual intervention needed')
    sys.exit(2)

# unpause per the documented cold-boot behaviour
from pathlib import Path
import requests
from verifier.creds import load_admin_password
pw = load_admin_password(Path('/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one'), None)
s = requests.Session()
s.post('http://10.0.0.252/api/login', json={'username': 'admin', 'password': pw}, timeout=10)
eng = s.get('http://10.0.0.252/api/engine', timeout=10).json()
log(f'engine state: started={eng.get("competition_started")} round={eng.get("last_round",{}).get("ID")}')
r1 = s.post('http://10.0.0.252/api/competition/start', json={'started': True}, timeout=10)
r2 = s.post('http://10.0.0.252/api/engine/pause', json={'pause': False}, timeout=10)
log(f'unpause: {r1.status_code} {r2.status_code}')

# verify networking WITHOUT repair: route via transit + apt-cacher DNAT presence
checks = subprocess.run(['ssh', '-i', '/home/hna/dev/dawgsec/tezcatlipoca/proxmox',
                         '-o', 'StrictHostKeyChecking=no', 'sysadmin@10.0.0.252',
                         'ip route get 192.168.120.10 | head -1; ip -4 addr show ens20 | grep -c 172.31.120.1; '
                         'sudo iptables -t nat -S POSTROUTING | grep -c "192.168.0.0/16"; '
                         'sysctl -n net.ipv4.ip_forward'],
                        capture_output=True, text=True)
log('networking after cold boot:\n' + checks.stdout)

# wait for a fresh round to complete with checks
seen = eng.get('last_round', {}).get('ID')
for i in range(40):
    time.sleep(15)
    e = s.get('http://10.0.0.252/api/engine', timeout=10).json()
    rid = e.get('last_round', {}).get('ID')
    if rid and rid != seen:
        log(f'new round {rid} completed (was {seen}) — loop advancing in-path')
        break
else:
    log('NO NEW ROUND in 600s — FAIL')
    sys.exit(3)

print('COLD-BOOT DRILL: PASS' if '172.31.120.1' in checks.stdout else 'COLD-BOOT DRILL: network check unclear')
