# Deploy: tezcatlipoca + tlaloc on red01

Who owns what (design principle 3):

- **tezcatlipoca** owns the range: cloning red01, the red segment, the engine
  DNAT (with its 30s re-assert timer), the Mythic VM, destroy, and the coverage
  proof behind `red-verdict`.
- **tlaloc** owns decisions and effects: what to do, to which box, with what
  mechanism, and the restore knowledge for everything it changed.

## Selecting the engine

A competition's Compfile picks the red engine:

```
red.engine tlaloc        # this competition deploys tlaloc v2
red.assume_breach full   # seed depth profile (v1 knob, still honoured)
```

`red.engine` defaults to **badauto** (v1): every existing Compfile keeps running
exactly what it ran before, which is the migration plan's phase-0 rule (v1 stays
the production red until v2 passes a scrim).

## The Compfile red block

```
red.assume_breach full      # full | light | off
red.depth 3                 # 0 access · 1 +escalate · 2 +channel · 3 +persistence/evasion
red.duration_min 150
red.caps.max_down 4
red.caps.min_standing 1
red.windows.endgame_teardown 15
red.windows.lockout 3
red.clef_url http://10.0.0.143:8080
red.c2_url https://10.0.0.210
red_yaml red.yaml           # a nested block (ladder/tiers) as YAML, if you prefer
```

Legacy v1 knobs keep working: `assume_breach 0|1|≥2` maps to `off|light|full`, and
`assume_breach_depth N` maps to `red.depth`. `red_yaml` is merged first and dotted
`red.*` keys win over it.

`red/tlaloc_ops.validate_red_block()` checks the block at deploy time and the
deploy REFUSES to plant red when it is invalid (unknown mechanism in a ladder
tier, unsorted tiers, `min_standing` below 1, a bad `assume_breach`): a
malformed ROE must fail before T0, not mid-event.

## The phase-7 sequence

`deploy_lib/phases/final.py` runs this before the `tz-ready` snapshot, so the
restore point every box carries is already compromised:

1. **render** `competitions/<id>/.tlaloc-config.yaml` (the file red01 will read),
2. **provision red01 + the engine DNAT** through the existing v1 path
   (`red_plant_ops._deploy_red`), including its per-competition identity
   reservation so a validation deploy can never recycle another session's red01,
3. **comp-prep** (`tlaloc c2-prep`): build the payload matrix against the
   competition's Mythic operation and freeze it into
   `competitions/<id>/red/payloads/` with a `uuid → box` manifest. Builds never
   run mid-event,
4. **stage red01**: push the tlaloc package to `/opt/tlaloc` (staged, never
   pip-installed — the systemd unit is what puts it on the import path), push the
   competition's nakon intel to `/var/lib/tlaloc/intel`,
5. **push the config** to `/etc/tlaloc/config.yaml` (0600) and **verify the
   contract**: the pushed file is fetched back and diffed against
   `tlaloc verify-config --print-contract` — a field the push omits is a feature
   that is dead on the VM while every local test stays green (the v1 lesson),
6. **install `tlaloc.service`** (enabled, not started — the seed runs explicitly),
7. **seed** (`tlaloc seed`): access → escalation → C2 channel → persistence +
   one evasion touch per box, at the depth the config declares, writing
   `world.meta["seed"]` and `competitions/<id>/red/manifest.json`,
8. `clean_trails` + snapshot as before.

Everything degrades: a red problem records a degradation and continues (a plant
problem must not abort a healthy range). `verify-competition` and the red report
catch it before T0.

## red01 layout

```
/opt/tlaloc/            the staged package (+ systemd/ + config.example.yaml)
/etc/tlaloc/config.yaml 0600, pushed by the deploy
/etc/tlaloc/env         optional KEY=VALUE secrets (TLALOC_MYTHIC_PASSWORD, TLALOC_LLM_API_KEY)
/var/lib/tlaloc/        state + audit + reports (per competition: fresh dir)
  world.json            belief state (0600, schema+comp stamped)
  events.jsonl          the audit log: every decision with its bundle
  KILL                  the kill switch (create it to stop red mid-cycle)
  report.md             secrets-free after-action
  report-secrets.md     organizer-facing 0600 (rotations, keys)
  restore-table.json    the undo table (verdict + organizer)
```

Manual control on red01: `systemctl stop tlaloc` (clean stop) or
`touch /var/lib/tlaloc/KILL` (the gate refuses every command from the next one
on, logged with its reason).

## Operating a run

```bash
python3 -m tlaloc --config /etc/tlaloc/config.yaml status     # beliefs, ladder tier, C2 callbacks
python3 -m tlaloc --config /etc/tlaloc/config.yaml candidates # the bounded option list
python3 -m tlaloc --config /etc/tlaloc/config.yaml decide     # one decision, no action
python3 -m tlaloc --config /etc/tlaloc/config.yaml report     # write the artifacts now
python3 -m tlaloc --config /etc/tlaloc/config.yaml restore-table --commands
python3 -m tlaloc --config /etc/tlaloc/config.yaml verify-config --pushed /etc/tlaloc/config.yaml
```

## Verdict and teardown

- **Evidence first.** Before anything is destroyed, tlaloc's
  `restore-table.json`, `report.md`, `report-secrets.md` (0600) and `world.json`
  are pulled into `evidence/red/`. `tlaloc_ops.write_verdict()` derives
  `evidence/red/verdict.json`: the red-side facts (planted, taken down, undo
  recipes) that the dashboard and the after-action read.
- **Coverage** (per scored pin, DOWN→UP proof) is the ENGINE's job: it reads the
  scoreboard, never the agent's own report. tlaloc's verdict deliberately does
  not claim coverage.
- **Teardown**: `tlaloc_ops.destroy_tlaloc_red()` stops `tlaloc.service` on
  red01 (ending the event clock and flushing the audit log) and then runs the
  VM/NAT teardown that owns red01. red01 is not estate-managed: skipping this
  step leaves the VM, its config, its key and its DNAT rules standing.

## First tlaloc scrim (cutover gate)

Run one scrim with `red.engine tlaloc` on a copy of a working competition, v1
standing by. What to grade (migration plan phase 6):

- pressure + adaptivity at or above the v1 baseline (the decision log's `source`
  mix and the impact ledger are the numbers; `restore-table.json`'s summary has
  them),
- zero guardrail incidents (`guardrail_halt` absent from `events.jsonl`),
- verdict + restore table complete: every impact row has a restore row,
- the re-plant loop actually fired (dead artifacts observed, `replant` candidates
  in the log).

Then, and only then: flip the default engine, archive v1's repo and its vendored
C trees out of the tree, update the in-repo docs, and register the cutover in the
infra-registry.
