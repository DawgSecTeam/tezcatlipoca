# tlaloc

The red-team component of the tezcatlipoca scoring range: an **assume-breach**
CCDC red teamer that holds pre-planted access, applies escalating downtime
pressure on scored services, and adapts to blue's response — with the club
owning the whole lifecycle.

This is a rebuild of an earlier private agent, not a refactor; the findings of
the rebuild's audit are what every module here is shaped by. Where this README
states behaviour, the code in `tlaloc/` is the authority — this file is the map.

This folder lives INSIDE the range engine: it is part of the
[tezcatlipoca](../README.md) tree, whose phase 7 provisions red01, stages this
package to `/opt/tlaloc`, installs the C2 when the competition enables one, and
runs the seed ([docs/deploy.md](docs/deploy.md)). Every competition stages
tlaloc; there is no engine selector and no fallback.

## What it does

Scored services going down is the whole score. tlaloc is already inside at T0
(day-0 seeding), then each cycle it:

1. reads the scoreboard mirror (Quotient) + detects blue's restores,
2. checks whether blue removed any planted artifact (the re-plant trigger),
3. builds the **bounded candidate list** from world state + ROE (`policy.py`),
4. asks the decision model (**clef**) to choose between those candidates
   (`decide.py`), gating its answer on calibrated confidence,
5. executes the chosen effect **through the one gate** (`effects/`),
6. logs the whole chain — bundle, answer, probabilities, threshold, source.

The run never depends on a model being up: clef down → deterministic candidate
#1; LLM down → clef still runs everything but command authoring; both down → the
deterministic ladder. Every degradation is logged with its reason.

## Quickstart

```bash
cd tlaloc                                   # from the tezcatlipoca checkout root
python3 -m pytest tests -q                  # the ONLY test runner (see docs/testing.md)

# inspect what the agent believes, from a competition dir
python3 -m tlaloc --comp /path/to/competition status
python3 -m tlaloc --comp /path/to/competition candidates

# one decision without touching a box
python3 -m tlaloc --comp /path/to/competition decide
python3 -m tlaloc --comp /path/to/competition decide --execute   # takes the action

# the event loop (on red01; refuses to run anywhere else unless --dry-run)
python3 -m tlaloc --config /etc/tlaloc/config.yaml --comp /path/to/competition run
```

On red01 the loop runs from `tlaloc.service` (see `systemd/`). Everything an
operator needs is a subcommand: `run`, `seed`, `status`, `candidates`, `decide`,
`report`, `restore-table`, `verify-config`, `c2-prep`.

## Layout

```
tlaloc/
  run.py             the loop: cycle, cadence, kill switch, circuit breakers, host guard
  policy.py          ROE as data + candidate generation/scoring + the question bundle
  decide.py          clef client, confidence gate, LLM arbiter, decision log, replay
  llm.py             the LLM's only jobs: author a command, arbitrate, write the narrative
  sensors.py         Quotient (read-only) + blue-restore detection
  seed.py            assume-breach seeding as a pipeline stage (full|light|off)
  report.py          after-action report, organizer 0600 secrets file, restore table
  cli.py / __main__.py
  ctx.py             the typed context every effect receives
  errors.py          GuardrailError (halt) vs PolicyError (reject one candidate)
  events.py          the append-only JSONL audit log (masked)
  config.py          DEFAULTS < config.yaml < env allow-map; Compfile red.* block
  state/             World (schema+comp stamped), digest (sized for the model batch)
  exec/              ssh.py (one transport), gate.py (the choke point), winexec.py
  effects/           one module per effect + the registry + the shared execute() template
  intel/             nakon facts (the sanctioned cheat) with a blind fallback
  c2/                Mythic client + the planter (frozen payloads, callback evidence)
config.example.yaml  annotated config
docs/                architecture, decision layer, ROE, deploy, testing, effect authoring
                     + TEST-PROMPT.md (the independent verification prompt)
systemd/tlaloc.service
tests/               pytest only; fixtures/comp-min + fixtures/decisions-recorded.jsonl
```

## The design rules the code enforces

1. **Deterministic by default, model-assisted by exception.** Facts and
   arithmetic come from nakon/Quotient/state; bounds from the Compfile (ROE);
   *judgment between bounded options* from clef; *generation* from the LLM.
2. **One code path per concern.** One exec, one gate, one registry, one loop, one
   state file. If two components can disagree about "can I do this?", that is a
   design bug (the earlier agent shipped six Windows exec idioms, three takedown
   routers, three unrecoverable-mode lists and four copies of its doctrine).
3. **Infrastructure belongs to tezcatlipoca.** red01 provisioning, the red
   segment, the engine's red rules and C2 DNAT, teardown/verdicts are range
   components; tlaloc owns decisions + effects.
4. **Facts belong to nakon.** Boxes, creds, planted vulns: read, never guessed.
5. **Assume-breach is the default** and is a pipeline stage, not loop logic.
6. **Every effect is recoverable-by-design** except where the ROE says otherwise;
   the restore knowledge is part of the effect and flows to the verdict artifact
   automatically.
7. **Everything is logged with its reason.**

## Status

Implemented and tested offline (see `docs/testing.md` for how each is verified):

- the state document (schema + comp stamped, refuse cross-event state), events,
  digest with the fixed truncation cascade, one SSH transport with bounded
  waits/keepalive/close-on-replace, the gate (POSIX **and** Windows pattern sets),
  the effect registry + `execute()` template, the loop with circuit breakers and
  host guard, the decision layer (clef client, question catalog, confidence gate,
  LLM arbiter, replay tests), the C2 layer (Mythic REST client, payload planter,
  callback-freshness liveness), assume-breach seeding, the report + restore-table
  artifacts, the CLI.

The live half is a scrim: the Mythic install through Gaia, the comp-prep payload
freeze against a live Mythic, the engine's red rules and C2 DNAT, and the
report/verdict pull are wired in `red/tlaloc_ops.py` (phase 7), but the offline
suite does not exercise a live Mythic or a real range.
