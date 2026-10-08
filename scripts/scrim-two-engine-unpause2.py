#!/usr/bin/env python
"""Unpause the scrim-one engine (competition/start + engine/pause) and confirm loop state."""
import sys, time
from pathlib import Path
import requests
sys.path.insert(0, '/home/hna/dev/dawgsec/tezcatlipoca')
from verifier.creds import load_admin_password

comp = Path('/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one')
pw = load_admin_password(comp, None)
base = 'http://10.0.0.252'
s = requests.Session()
r = s.post(base + '/api/login', json={'username': 'admin', 'password': pw}, timeout=10)
assert r.status_code == 200, r.status_code
print('login OK')
print('engine before:', s.get(base + '/api/engine', timeout=10).text[:200])
r1 = s.post(base + '/api/competition/start', json={'started': True}, timeout=10)
print('competition/start:', r1.status_code, r1.text[:120])
r2 = s.post(base + '/api/engine/pause', json={'pause': False}, timeout=10)
print('engine/pause:', r2.status_code, r2.text[:120])
time.sleep(3)
print('engine after:', s.get(base + '/api/engine', timeout=10).text[:200])
