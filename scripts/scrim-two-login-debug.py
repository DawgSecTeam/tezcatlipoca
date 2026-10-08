#!/usr/bin/env python
"""Debug: admin login shape against the scrim-one engine (prints status codes only)."""
import json, sys, requests
sys.path.insert(0, '/home/hna/dev/dawgsec/tezcatlipoca')
from verifier.creds import load_admin_password

from pathlib import Path
comp = Path('/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one')
pw = load_admin_password(comp, None)
print('pw source len:', len(pw or ''))
base = 'http://10.0.0.252'
for body in ({'username': 'admin', 'password': pw},
             {'userName': 'admin', 'password': pw}):
    try:
        r = requests.post(base + '/api/login', json=body, timeout=10)
        print(list(body)[0], '->', r.status_code, r.text[:100].replace(pw or '@@', '**'))
    except Exception as e:
        print(list(body)[0], '-> EXC', e)
r = requests.get(base + '/api/engine', timeout=10)
print('/api/engine anon:', r.status_code, r.text[:200])
