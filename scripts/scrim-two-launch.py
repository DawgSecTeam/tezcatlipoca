#!/usr/bin/env python3
"""Launcher for the scrim-two full run: loads .env, records endpoint/GPU roster,
then hands off to run-agent-scrim (scrim.cli)."""
import json, os, sys, time
from pathlib import Path
REPO = Path('/home/hna/dev/dawgsec/tezcatlipoca')
os.chdir(REPO)
sys.path.insert(0, str(REPO))
from dotenv import load_dotenv
load_dotenv(REPO / '.env')

import requests

def probe(url, path='/v1/models'):
    try:
        r = requests.get(url + path, timeout=4)
        if r.status_code != 200:
            return {'url': url, 'status': r.status_code, 'note': r.text[:80]}
        try:
            models = [m['name'] for m in r.json().get('models', [])]
        except ValueError:
            models = ['<non-json>']
        out = {'url': url, 'status': 200, 'models': models}
        if 'localhost:8080' in url or '10.0.0.143' in url:
            try:
                slots = requests.get(url + '/slots', timeout=4).json()
                out['slots'] = len(slots)
                out['slot_n_ctx'] = [s.get('n_ctx') for s in slots]
            except Exception as e:
                out['slots_error'] = str(e)
        return out
    except Exception as e:
        return {'url': url, 'status': 'DOWN', 'error': str(e)[:100]}

roster = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'when': 'start',
          'candidates': [probe(b) for b in (
              'http://localhost:8080', 'http://localhost:8092', 'http://localhost:8180',
              'http://100.64.0.19:8080', 'http://100.64.0.19:8000')],
          'gpu_local': None, 'gpu_gb10': None}
try:
    roster['gpu_local'] = requests.get('http://localhost:8081/api/gpu', timeout=4).json()
except Exception as e:
    roster['gpu_local'] = {'error': str(e)[:100]}
try:
    roster['gpu_gb10'] = requests.get('http://100.64.0.19:8081/api/gpu', timeout=4).json()
except Exception as e:
    roster['gpu_gb10'] = {'error': str(e)[:100]}
roster['decision'] = {
    'red': {'endpoint': 'http://10.0.0.143:8080/v1', 'model': 'qwen3.8-27b',
            'note': 'llama.cpp on operator/B70 host, 2x208896 slots; red01 dials via node LAN'},
    'blue': {'endpoint': 'http://100.64.0.19:8000/v1', 'model': 'qwen3.8-flash-next',
             'note': 'vLLM on gx10, separate host; weaker model to blue per owner rule'},
    'serialization': 'red and blue on different hosts; no shared-GPU serialization'}

logdir = REPO / 'logs'
(logdir / 'scrim-two-roster-start.json').write_text(json.dumps(roster, indent=2))
print(json.dumps(roster, indent=2)[:1200])

os.environ['TEZ_RED_TEMPLATE'] = 'base-ubuntu24.04-fix'
sys.argv = ['run-agent-scrim.py',
            '--competition', 'scrim-one', '--teams', '1', '--duration-min', '180',
            '--blue-watchdog', '--keep-range', '--skip-deploy', '--reasoning-effort', '',
            '--llm-base-url', 'http://10.0.0.143:8080/v1', '--red-model', 'qwen3.8-27b',
            '--blue-base-url', 'http://100.64.0.19:8000/v1', '--blue-model', 'qwen3.8-flash-next',
            '--red-vmid', '999', '--red-storage', 'hdd']
from scrim.cli import main
main()
