#!/usr/bin/env python
"""Probe Quotient admin login + engine state; unpause if paused (scrim-two prep)."""
import json, subprocess, sys
state = json.load(open('/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one/.deploy_state.json'))
E = '10.0.0.252'
jar = '/tmp/ba-scrim2-admin.jar'
pw = state['admin_password']
for line in open('/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one/credentials.txt'):
    parts = line.split()
    if len(parts) >= 2 and parts[0] == 'admin':
        pw = parts[1]
        break

def curl(*args):
    return subprocess.run(['curl', '-s', '--max-time', '15', *args],
                          capture_output=True, text=True).stdout

logged = False
for key in ('username', 'userName'):
    out = curl('-c', jar, '-X', 'POST', f'http://{E}/api/login',
               '-H', 'Content-Type: application/json',
               '-d', json.dumps({key: 'admin', 'password': pw}))
    ok = bool(out) and 'error' not in out.lower()
    print('login', key, '->', 'OK' if ok else out[:120])
    if ok:
        logged = True
        break
if not logged:
    sys.exit(1)

print('engine:', curl('-b', jar, f'http://{E}/api/engine')[:300])
if '--unpause' in sys.argv:
    print('competition/start:', curl('-b', jar, '-X', 'POST', f'http://{E}/api/competition/start',
                                     '-H', 'Content-Type: application/json',
                                     '-d', json.dumps({'started': True}))[:200])
    print('engine/pause:', curl('-b', jar, '-X', 'POST', f'http://{E}/api/engine/pause',
                                '-H', 'Content-Type: application/json',
                                '-d', json.dumps({'pause': False}))[:200])
    print('engine after:', curl('-b', jar, f'http://{E}/api/engine')[:300])
