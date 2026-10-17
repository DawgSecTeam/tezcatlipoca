# tlaloc v2 — independent test prompt

Paste the block below into an agent session with shell access on **bossagent**. It
verifies the v2 rebuild and returns a verdict. The offline half takes ~15 minutes;
the live half needs red01, clef and (optionally) a scrim window.

Everything in the prompt is a check someone else can re-run — nothing asks the
agent to trust this repo's own documentation.

---

You are the independent tester for **tlaloc v2**, the rebuilt CCDC red-team agent.

Context you need, and should verify rather than assume:
- v2 lives IN the range engine: it is the `tlaloc/` folder of the tezcatlipoca
  checkout. tlaloc is the only red engine: every competition stages it in phase 7,
  with no engine selector and no fallback.
- The design it must satisfy is this folder's `docs/` (architecture,
  decision-layer, roe, effects-authoring); the v1 audit that motivated the
  rebuild was an external review, not part of this tree.
- The engine's integration is `red/tlaloc_ops.py`: phase 7 of every deploy
  provisions red01, installs the C2, pushes the config and seeds the agent. The
  Compfile's `red.*` block is the ROE, not an engine selector.

## Rules of the test

1. **Report actual output.** Never claim a step passed without the command you ran
   and its real result. A failure reported honestly is a good result; a plausible
   summary of a failure is a failed test.
2. **Read before you believe.** The repo's docs describe intent. Where a doc and
   the code disagree, the code wins and the disagreement is a finding.
3. **Do not touch the range.** No SSH to a team box, no red01, no deploy, no
   scrim. The only live network traffic allowed is the clef probe and the dry-run
   smoke in step 5 (both read-only, both against the competition's clef endpoint —
   the address is estate ground truth: `infrastructure-status.md`).
4. **You may fix what you find**, but every fix keeps the suite green and comes
   with before/after evidence in your report. Do not commit or push; leave that
   to the human.
5. Timestamps in EST/EDT.

## Step 1 — orient (5 min)

Read, in this order: `README.md`, `AGENTS.md`, `docs/architecture.md`,
`docs/decision-layer.md`, `docs/roe.md`, `docs/effects-authoring.md`,
`docs/testing.md`, then `tlaloc/run.py`, `tlaloc/effects/base.py`,
`tlaloc/effects/__init__.py`, `tlaloc/decide.py`, `tlaloc/policy.py`.

Write down, in your own words: what the loop does per cycle; where the single
action path is; what decides "can I do this?"; and what happens when each model is
down. If any of those is not answerable from the code, that is finding #1.

## Step 2 — the suite, and the suite from a clean clone (5 min)

```bash
cd tlaloc                                     # from the tezcatlipoca checkout root
python3 -m pytest tests -q                   # expect: green (count drifts)
C=$(mktemp -d)
git clone "$(git rev-parse --show-toplevel)" "$C/tezcatlipoca"
cd "$C/tezcatlipoca" && python3 -m pytest tlaloc/tests -q
```

Both must be green, and the clone must collect the same number of tests — a repo
that only works in its working copy is a failed repo. (From the repo root the
same suite is `python3 -m pytest tlaloc/tests -q`.)

## Step 3 — the invariant audit (10 min)

These are the design promises. Check them in the code and say where each holds and
where it does not. Grep-level evidence is fine, but the claim must be specific.

1. **One action path.** Every effect runs through `effects.execute()` — nothing
   calls an effect's `run()` directly, and no caller can skip the gate. (Two
   deliberate exceptions exist; find them and check they are justified in place.)
2. **The gate is per command.** Kill switch checked per command (not per cycle),
   `ensure_target` before anything that touches a target, POSIX **and** Windows
   lint on free-form text, dry-run read one way everywhere.
3. **clef never opens a gate.** Its `command_intent` classification informs
   attribution; it cannot authorise anything.
4. **Nothing is deleted.** Effects move or copy. Every `rm`/`Remove-Item` in the
   tree must be inside a restore recipe or a documented temp-file cleanup.
5. **Every mutation is recoverable-by-design**: `Result.artifacts` and
   `Result.restore` set for anything that changed a box; secrets only in the
   organizer-facing 0600 artifact, never in the blue-visible report.
6. **The catalogue cannot be faked.** `effects.MODULES` names 12 effects, the
   tests refuse to call it complete while any module is missing, and the dry-run
   sweep covers every registered effect with nothing touched.
7. **Config plumbing.** Every `ctx.cfg[...]` read in an effect is in
   `config.DEFAULTS` **and** in `cli.CONFIG_CONTRACT`, and
   `tests/test_config_contract.py` proves it. A read that is not in the contract is
   dropped on the way to red01 — a feature alive locally and dead on the VM.
8. **Hermetic tests.** No test opens a socket or spawns a process
   (`tests/conftest.py` enforces it). Try to prove it: pick two tests and show the
   guard firing if you add a `subprocess.run` call, then revert.
9. **No v1 regressions were re-imported**: no `inspect.getsource` test, no
   `unittest discover` runner, no hand-maintained facts table that nakon owns, no
   literal artifact names outside `effects/artifactnames.py`.

## Step 4 — the decision chain against the live model (5 min)

```bash
curl -s -m 60 http://<clef-host>:8080/v1/systemone -H 'Content-Type: application/json' \
  --data-binary '{"state":{"v":2,"t":"10/150","phase":"opening"},
   "questions":{"next_effect":{"type":"choice","instructions":"Best next action?",
   "criteria":{"hold":"do nothing","takedown":"stop nginx on web01"}}}}'
```

Expect HTTP 200 with per-question `probabilities` (no text generation — chat
completions 500 by design). The endpoint's batch is 16384 tokens; it names that
number in its own rejection message when a request is too big.

Then the loop itself, offline, one dry run:

```bash
cd tlaloc                                     # from the tezcatlipoca checkout root
/usr/bin/python3 -m tlaloc --config /tmp/tlaloc-smoke.yaml --comp /tmp/tlaloc-smoke-comp run --cycles 3
python3 -c "import json;print([r['kind'] for r in map(json.loads,open('/tmp/tlaloc-smoke-state/events.jsonl'))])"
```

Check in `events.jsonl`: decisions with `source: clef` and the real probabilities,
`action` rows for what executed, **no** `error`, and **no** `clef_bundle_compact`
or `clef_bundle_over_budget` rows (those mean the request outgrew the endpoint).
If `/tmp/tlaloc-smoke.yaml` or the comp dir are gone, rebuild them from
`config.example.yaml` and note that you did.

## Step 5 — engine integration, without deploying (5 min)

```bash
cd ~/dev/dawgsec/tezcatlipoca
python3 -m pytest tests/test_tlaloc_ops.py -q     # expect: green (count drifts)
```

Then answer from the code: what does a competition's Compfile `red.*` block
control (and what is not selectable)? What does the deploy push to red01, and what
happens if a field the deploy meant to set is missing from the pushed file? What
does teardown do with the red VM? Two tests in the wider suite fail on this machine
(`test_local_python_spawns`, `test_scrim_defects`) — they fail identically at
upstream `accd3ba`, so they are pre-existing; confirm that and do not "fix" them.

## Step 6 — verdict

```
VERDICT: ship-to-scrim | fix-first | does-not-meet-the-plan
VERIFIED: <step → the actual result you observed>
FINDINGS: <one per line: severity · file:line · what is wrong · evidence>
PLAN GAPS: <what the rebuild's documented design (docs/) promises that the code does not do>
UNVERIFIED: <what you could not check here, and what it would take>
```

Be specific and be harsh. The point of this test is to find the thing that will
embarrass the club during a competition.

---

## The live half (run only after the human approves)

Once the offline verdict is `ship-to-scrim`, the next test is a real one and it
needs red01, Mythic and a scrim window. It is deliberately **not** in the prompt
above, because it touches team boxes:

1. **Pull the engine where it is deployed.** The dashboard cuts each run's engine
   worktree from a pinned origin ref (host and path: `infrastructure-status.md`),
   so the pushed `tlaloc/` folder has to be in that ref before the deploy stages
   it.
2. **Mythic, once.** Install/configure the club's Mythic via Gaia (on red01 unless
   `red.c2_ip` names another box), then run `tlaloc c2-prep` for the competition
   and confirm `red/payloads/manifest.json` maps frozen payloads to boxes.
3. **Smoke comp, `assume_breach: light`, LLM-free**: full
   `plan → deploy (red planted in phase 7) → run → destroy` on the 1-box smoke comp
   with clef stubbed, then again live; teardown captures the report + verdict
   before it removes the VM.
4. **First scrim, `assume_breach: full`.** Exit gates: pressure + adaptivity (the
   decision log's `source` mix and the impact ledger), zero guardrail incidents,
   verdict and restore table complete, and the C2 DNAT re-assert survives a Docker
   restart on the engine.
