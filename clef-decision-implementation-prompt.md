# Task: make clef's `next_effect` decisions confident and useful

You are implementing a change in `/home/hna/dev/tezcatlipoca` (tlaloc, the red agent). Read these before editing:

- `AGENTS.md`
- `tlaloc/AGENTS.md`
- `docs/architecture.md`
- `docs/internals.md`
- `tlaloc/docs/decision-layer.md`
- `docs/known-issues.md`

Do not change the safety rails. clef only chooses among `policy.candidates()`; `policy.validate`, the hard gate, the kill switch, and the ROE allowlist still run. The model never invents a candidate.

The endpoint is `http://10.0.0.143:8080/v1/systemone`: llama.cpp, `clef-flash` 9B Q4_K_M, 16k batch, typed questions only (`choice` / `score` / `noul`), deterministic, no text generation. Its system prompt is fixed: "Read the complete state and schema. Decide every field jointly. Each answer must be exactly one of that field's allowed options." It cannot reason in prose; the state and instructions must carry all context.

The investigation's probe scripts are under `.cache/clef-probe/` in the main tree (untracked, gitignored). Reuse or re-derive them; do not commit them.

## The problem

The `next_effect` question is one `choice` over up to 8 candidates plus `hold`, with long machine IDs (`takedown:192.168.131.4:nginx`) and a compressed JSON state (`v`, `t`, `cap`, `down`, `ch`, `resp`, `unrec`). The confidence gate almost never fires.

Measured from 40 unique red-side `events.jsonl` logs (54 collected files deduped by sha256; 68 run starts; 453 `next_effect` decisions, Oct 15-23):

- clef cleared `theta=0.60` **15 times (3.8%)**.
- With the normal **8 candidates + `hold`**: **0/298 passes**. Median top probability **0.286**.
- **13/15 passes were `hold`**; only **two confident non-hold picks in 453 decisions**.
- Actual chooser: **deterministic 372 (82%)**, **LLM arbiter 66 (15%)**, **clef 15 (3%)**.
- The model is confident on the smaller questions: `escalate_now` 359/393 (91.3%), `blue_hunting` 33/38 (86.8%), `pressure_count` 189/395 (47.8%). The choice question is the outlier.

The model is not broken. It is deterministic: replaying a logged bundle reproduces the logged distribution; the same request twice returns identical probabilities to four decimals. The input shape is the problem.

## What the probe found

The winning input shape is **a readable state + short option keys + natural-language descriptions + one `noul` question per action**, not one 8-way `choice`.

Tested against the live endpoint on real logged cycles:

- **11 unique cycles with at least one high-value action** (takedown/lockout/block_port against a standing service) and at least two standing services:
  - **11/11 chose a high-value action**
  - **median p(yes) = 0.811**
  - **8/11 at or above 0.6**
- **5 unique cycles with no high-value action available** (only persistence/channel/lockout/rotate/hold):
  - 0/5 picked a high-value action (none existed)
  - median p(yes) = 0.501
  - 3 chose hold, 2 chose channel repair
- Improved `choice` with the same readable state/descriptions:
  - median top probability **0.389**; top-3 candidates + hold **0.548**, high-value 10/11.
  - Better than baseline, but not gate-ready.
- `score` per action ("how much scored-service downtime would this create?" with `none` / `a little` / `some` / `a lot`):
  - median expected **2.29** on high-value cycles vs **1.16** on no-value cycles
  - separated high-value from support in 10/11.
- One joint request with 9 action nouls + pressure + escalate + blue:
  - **2,063 input tokens**, all answers returned
  - action p 0.842, pressure expected 1.40, escalate 0.447, blue 0.716
  - fits the 16k batch in one pass.

Representative examples:

- `run-81a24e13` cycle 5: logged top `mysql 0.200`, `persist:timestomp 0.160`, `hold 0.154`. Expanded state + noul: `mysql 0.841`.
- `run-8f91036c` cycle 19: logged top `hold 0.222`, while the deterministic policy picked `persist:scheduled_task` — despite nine standing services. Expanded state + noul: `nginx 0.887`, `mysql 0.825`, `persist:scheduled_task 0.037`.
- `run-0a751922` endgame: no high-value actions available; logged `hold 0.59` is defensible. Expanded state + noul stays uncertain (0.35-0.52) and holds.

## What to implement

### 1. Add a clef-facing state renderer

Use an **expanded JSON object** (or a short prose brief; both tested equally well). Expanded JSON is easier to generate from the existing state. Required fields:

- `minute`, `total_minutes`, `minutes_left`, `phase`, `objective`
- `priority_team` (the team with the most blue restores; explain the priority)
- `teams[]`: name, `blue_restores_last_30m`, `blue_trend`
- `teams[].boxes[]`: id, OS, access, channel, dead artifacts
- `teams[].boxes[].services[]`: name, status
- `recent_events` (the last handful)
- `rules_of_engagement`: window, allowed mechanisms, unrecoverable allowed, teardown active

No secrets. IPs and service names only. Do not serialize credentials from `world`.

Measure the token cost with `tokens_needed`; keep the 16k budget and the existing compact fallback. The observed range was ~1.5-2.1k input tokens. Do not silently truncate.

### 2. Replace the `next_effect` choice with per-action `noul` questions

- Question keys must be short labels (`A`, `B`, `C`..., `HOLD`), never the machine candidate id. Keep a map from key to candidate id in the decision metadata.
- Each instruction:
  `Should red do this action this cycle to maximize scored-service downtime? <natural description>`
- The description must say:
  - effect and target,
  - team and OS,
  - whether the service is currently standing,
  - whether it adds downtime now or only protects access (persistence/channel repair/recon).
- Derive only from the candidate fields (`effect`, `params`, `reason`, `score`, `high_stakes`); do not invent facts.
- Keep `hold` as a separate question, not a choice option. Do not remove it; the model needs a legal "do nothing."

Example descriptions:

- `Take down mysql on team2 database 192.168.131.6. It is a scored service currently standing; taking it down adds downtime now.`
- `Re-plant timestomp persistence on 192.168.131.3. This protects access after blue removed artifacts but adds no downtime.`
- `Do nothing this cycle; consolidate and keep depth. No downtime is added.`

### 3. Selection and gate

- Compute `p_yes` for each action.
- Choose the action with the highest `p_yes`.
- Hold if `p_hold` is the highest and `>= theta`; otherwise act on the max action if `p_yes >= theta`.
- If the max `p_yes < theta`, preserve the existing degradation ladder:
  - arbiter for a high-stakes candidate, if the arbiter is available;
  - otherwise deterministic candidate #1.
- Use **`p_yes` directly**, not `max(p, 1-p)`. A p_yes of 0.3 is a confident no, but the existing `top_probability()` would read it as 0.7 confidence. Add a dedicated helper/path for action nouls; do not change the meaning of `top_probability()` for the existing `escalate_now` / `blue_hunting` uses.
- Keep the `Decision.source` labels `clef` / `llm_arbiter` / `deterministic` and the chosen candidate id.
- Still run `policy.validate` and the hard gate.

### 4. Logging

The event log currently omits `top_prob` on `decision` rows. Add, for every `next_effect` decision:

- `top_prob`, the per-action `p_yes` map, and `hold` p
- chosen key and mapped candidate id
- theta, source, candidate count, high-stakes flag
- `usage.input_tokens` and state-renderer/question-shape version
- whether the bundle was compact or over budget

Make old and new runs distinguishable. This is required for the monitoring below.

### 5. Adjacent fixes — keep separate commits

- **Pressure:** `pressure_count` currently falls back to `extra_pressure_max` on low confidence. Across 206 low-confidence cycles with an expected value, the mean expected count was **1.37** but the mean fired was **2.72**; 191/260 low-confidence cycles fired the max 3. Fix the fallback to `0` or `round(expected)`, and add a test. This is the clearest case where clef's uncertainty is converted into maximum pressure.
- **Arbiter:** 66 usable replies vs 73 terminal failures; 72 failures were `reply was not JSON`, mostly truncated at `max_tokens=256`; 204 reasoning-only intermediate retries. Retry on truncated JSON as well as empty content, and/or raise the budget. This is adjacent, not the clef-input change; do not bundle it unless the user approves.
- **Do not** lower theta to 0.2; the probe shows that mostly buys holds. **Do not** merely cap to four options; the choice path still hovers around 0.55.

### 6. Docs

- Update `tlaloc/docs/decision-layer.md` with:
  - the new question shape and `p_yes` rule;
  - the expanded state renderer;
  - the measured numbers above;
  - the fact that clef's probabilities are not guaranteed calibrated and theta must be tuned on range data.
- Keep one canonical statement; link the code.
- Put anything unproven in `docs/known-issues.md`, not in the doc as a claim.

## Tests

### Unit (`tlaloc/tests/test_decide.py`)

- `test_next_effect_noul_selects_max_yes`
- `test_next_effect_noul_uses_p_yes_not_max` — p_yes 0.3 must not pass as 0.7 confidence
- `test_next_effect_noul_hold_wins`
- `test_next_effect_noul_low_confidence_falls_back` — deterministic/arbiter path
- `test_next_effect_noul_never_invents` — unknown key ignored; candidate list is the authority
- `test_noul_option_keys_are_short_and_unique` — `A..`, `HOLD`, mapping to candidate ids
- `test_expanded_state_has_no_secrets`
- `test_expanded_state_fits_budget` and compact fallback
- `test_pressure_low_confidence_is_not_max`

### Replay fixtures

The current `tlaloc/tests/fixtures/decisions-recorded.jsonl` has only five 1-2-option records with top probabilities 0.36-0.88. It never exercises the real 8-option regime, which is why the suite stayed green while the live gate never fired.

Add real bundles from:

- `run-81a24e13` cycles 2, 3, 5
- `run-f9f8712a` cycles 7, 8
- `run-8f91036c` cycles 8, 19
- `run-4ac4ccd0` cycles 2, 5
- no-value cycles: `run-f9f8712a` 15, 18; `run-0a751922` 15

Convert the logged `choice` criteria to the new noul questions, stub the p_yes values from the probe, and assert the chosen action and hold behavior. Store the actual state/inputs in the fixture so future changes cannot silently drift.

### Integration

- `run.py` step with `StubClef`: assert the hard gate still runs, the action executes, the logs contain p_yes/top_prob/shape, and the deterministic fallback still works.
- `python3 -m pytest tlaloc/tests -q` and the root test suite.
- `python3 -m tlaloc --dry-run run --cycles 3` from a new worktree.

### Live probe

POST one expanded-state + noul request to `10.0.0.143:8080/v1/systemone`. Expect:

- HTTP 200
- per-action `noul` probabilities
- `input_tokens < 16384`
- no `clef_bundle_over_budget`

Record the exact command and output in the report.

### Practice range

Follow `AGENTS.md` exactly:

- **New worktree off `main`** — never the main tree, never another run's worktree.
- `git submodule update --init`
- copy the node-proper config, `.env`, and SSH key; check the resolved endpoint/node/datastore in the banner.
- Use the dashboard for range runs; direct driver runs are the recovery path only.
- Use `scrim-one-light` or `sliver-practice-4box`.
- A/B: 2-3 scrims with the current choice and 2-3 with the noul change. Record blue presence and restores first; a red-vs-empty-room run is a failed run, not a red success.
- No manual SSH repairs, no hand-edited VMs, no `--force`. Teardown with `destroy-competition.py` (or `--teams-only` to keep templates) and archive artifacts.

### Acceptance criteria

- No guardrail/ROE/kill-switch violations; no invented candidates.
- `next_effect` clef source share on high-value cycles **>= 40%** (baseline 3.8%).
- Median p_yes on high-value cycles **>= 0.7**; hold rate on high-value cycles **<= 20%**.
- No-value cycles: hold/channel repair **>= 60%**, high-value pick **0** (none available).
- Pressure low-confidence fallback max rate **<= 20%** (baseline 73%).
- Impact/restore ratio not worse than baseline; blue restores > 0 in the scrims.
- Logs contain p_yes/top_prob/shape; old and new runs distinguishable.
- Tests green; docs updated.

## Monitor after merge

For every run, pull `comp/red/events.jsonl` and report:

- `next_effect` source mix (`clef` / `llm_arbiter` / `deterministic`), pass rate, median p_yes, p_yes distribution, hold rate, chosen action type mix.
- Clef availability: no-answer count, connection/timeout/cooldown, input_tokens, compact/over-budget events. Last analysis: 59/453 `next_effect` calls (13%) had no clef answer — 28 connection failures and 5 timeouts to `10.0.0.143:8080`, 14 cooldowns, and 12 calls to the stale documentation URL `203.0.113.30` in older runs.
- Pressure: fallback max rate, expected vs fired, low-confidence rate.
- Arbiter: usable vs terminal failures, reasoning-only count, failure reasons.
- Outcome: impacts, restores, footholds, artifacts, C2, cycle time. Lead with blue presence; a red-vs-empty-room run is not evidence of success.

Watch for these regressions:

- Clef share stays below 10% on high-value cycles -> the bundle is not reaching the model, or theta is wrong.
- Hold rate rises above 30% on high-value cycles -> the hold question is absorbing the action mass.
- p_yes is high for persistence/channel while services are up -> the descriptions are not carrying the "adds no downtime" tradeoff.
- Impact/restore ratio worsens as clef share rises -> the model's ranking is not strategically good; revisit service value/priority in the state.

Do not use the model's `confidence` field as the gate. For `choice`, it is a normalized margin; for `noul`, use `p_yes` directly. The model's probabilities are not guaranteed calibrated; tune theta on range data, not on one probe.

## Stop conditions

- Endpoint model/version differs from `clef-flash` 9B Q4_K_M -> stop and report.
- The noul probe does not reproduce the high-value confidence on the known cycles -> stop and report; do not tune theta blindly.
- The expanded state would leak credentials -> stop and report.
- The change requires altering the safety rails or the candidate authority -> stop and ask the user.

## Deliverables

- A branch with the minimal change, tests, fixtures, and docs.
- A short report with before/after metrics from 2-3 scrims each, exact commands, token counts, source mix, p_yes distributions, impacts/restores, and blue presence.
- Known-issues entries for anything unresolved (arbiter, pressure if not fixed, calibration).
- Push the branch; land as one squash commit on `main` per repo rules. Do not write "validated" until the range runs pass.
