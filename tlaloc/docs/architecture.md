# Architecture

```
tezcatlipoca (engine + dashboard + competitions repo)
│  plan ── deploy ── run ── verdict ── destroy
│  │       │         │      │          │
│  │       │         │      │          └─ restore table + evidence
│  │       │         │      └─ tlaloc report/verdict on red01
│  │       │         └─ tlaloc loop (red01)
│  │       └─ red01_ops: clone red01, red segment, engine rules + C2 DNAT (30s re-assert)
│  │       └─ clone_ops/terraform + nakon_ops (existing)
│  └─ Compfile: red.{assume_breach, depth, duration_min, ladder, caps, windows, budget, clef_url, c2_*}
│
├── nakon (facts + mechanisms + catalog CI gate)
├── Mythic C2 (installed/configured by Gaia) — callbacks DNAT'd by the engine
│     agents: Apollo (win), Poseidon (linux) · profile: http + webhook
│     payloads: built at comp-prep, frozen into competitions/<id>/red/payloads/
└── clef-flash (shared club infra, POST /v1/systemone) — choice/score/noul
```

Red is planted during deploy (phase 7): every competition stages tlaloc, with no
engine selector and no fallback.

## Runtime on red01

One systemd unit running `python3 -m tlaloc run`, state in `/var/lib/tlaloc/`,
config at `/etc/tlaloc/config.yaml` (pushed by the deploy pipeline; the pushed
file must carry every field in `cli.CONFIG_CONTRACT` or the feature is dead on
the VM — `tlaloc verify-config` diffs it). tlaloc talks to: Quotient (read-only),
Mythic (HTTPS), clef (HTTPS), SSH (team boxes), and nothing else. It never talks
to Proxmox, the engine's Docker, or the dashboard: infrastructure belongs to the
engine.

## The pieces and what each owns

| module | owns | must not |
|---|---|---|
| `policy.py` | the event clock, the ladder, the caps, the windows; the candidate list and its deterministic score; the question bundle | execute anything, or call a model |
| `decide.py` | clef client + circuit breaker, the confidence gate, the arbiter, per-decision logging, replay | invent a candidate; open a gate |
| `llm.py` | command authoring, arbitration, narrative | decide; return `reasoning_content` as prose |
| `sensors.py` | the scoreboard mirror, DOWN→UP restore detection | write to the scoreboard |
| `seed.py` | the assume-breach pipeline stage (full/light/off), its per-box record, `red/manifest.json` | bypass the gate or the registry |
| `run.py` | the loop, cadence, circuit breakers, host guard, report invocation | hold doctrine (it lives in the registry + config) |
| `effects/` | one capability per module, its preconditions, its undo recipe | call gate primitives; return `ok=True` for a give-up |
| `exec/` | the single transport, the single gate, the Windows idioms | decide anything; hold state |
| `intel/` | facts (nakon) and the blind fallback | hand-maintain what nakon already knows |
| `state/` | World v2 + the compact digest | merge concurrent writers (flock, reload-then-mutate) |
| `c2/` | Mythic tasking + callback evidence + payload planting | build payloads mid-event |
| `report.py` | report, organizer secrets file, restore table | re-derive the undo recipes (effects recorded them) |

## One cycle, end to end

```
gate.check_kill_switch()                     (per cycle; the template re-checks per command)
world.reload()                               disk is truth
sensors.observe()                            scoreboard mirror + blue restores -> world
c2.callbacks() -> world.note_callback()      freshness evidence, never liveness by process
verify_artifacts()                           each artifact's check_cmd -> dead set (re-plant trigger)
candidates = policy.candidates(ctx)          bounded, scored, reasoned
digest, questions = decider.compose(ctx, candidates)
decision = decider.next_effect(...)          clef -> gate(theta) -> arbiter? -> deterministic
executed += run(decision.chosen)             policy.validate -> effects.execute -> ledger/audit
escalate_now / pressure_count                the same answers, applied to already-legal candidates
decider.record(cycle, executed=...)          one decision_result per cycle
sleep(cadence)                               decision_window_min (+jitter) | endgame_cycle_min
```

At the end of the event (or on halt): `report.write_artifacts()` writes
`report.md` (secrets-free), `report-secrets.md` (0600, organizer), and
`restore-table.json` (machine-readable; the engine's `red-verdict` and destroy
consume it).

## Deleted from v1 (and why nothing replaces it)

- the C2 beacon stacks and their vendored C toolkits → Mythic (the only C2; the
  beacon trees are gone);
- `pivot_exec` → `task_channel` (the C2 mesh covers the pivot cases it existed
  for; re-add only if a scrim proves otherwise);
- the coverage/endgame/burndown sweepers → one "final window" policy block plus
  the engine's `red-verdict` (same loop, same gate, faster cadence);
- `db_attack` / `harvest_creds` as standalone tactics → enrichment inside
  `escalate` / `spray_access`;
- six Windows exec idioms → `exec/winexec.py`;
- three unrecoverable-mode lists → one `unrecoverable` tag;
- the vendored `C3rberus/` tree → out of the tree (mining source, not runtime).
