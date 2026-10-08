import json, collections
RD = '/home/hna/dev/dawgsec/tezcatlipoca/competitions/scrim-one/.automated-tests/run-b6c2db3e'
ev = [json.loads(l) for l in open(RD + '/evidence/red/events.jsonl')]
ev = [e for e in ev if e['ts'] > '2026-10-07T17:38']
llm = [e for e in ev if e['kind'] == 'llm']
fails = [e for e in llm if not e.get('ok')]
print('llm calls:', len(llm), 'failures:', len(fails))
print(collections.Counter(str(f.get('detail'))[:70] for f in fails))
maxd = 0
for e in ev:
    if e['kind'] == 'score':
        sd = e.get('services_down') or {}
        maxd = max(maxd, len([k for k, v in sd.items() if v]))
print('max simultaneous down (events):', maxd)
print('--- windows-targeted actions (.2/.3):')
for e in ev:
    s = json.dumps(e)
    if ('.120.2' in s or '.120.3' in s) and e['kind'] == 'action':
        print(e['ts'][11:19], e.get('tactic'), e.get('target'), 'ok=', e.get('ok'), str(e.get('detail'))[:90])
print('--- all actions:')
acts = collections.Counter((e.get('tactic'), bool(e.get('ok'))) for e in ev if e['kind'] == 'action')
print(acts)
print('--- evicted lines:')
for e in ev:
    d = str(e.get('detail'))
    if 'evict' in d.lower():
        print(e['ts'][11:19], d[:120])
