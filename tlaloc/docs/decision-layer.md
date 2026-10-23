# The decision layer

clef-flash (`ggml-org/Clef-Flash-GGUF`, 9B, Jev/SystemOne-compatible) answers
**typed questions with probabilities** in one forward pass; those probabilities are
not guaranteed calibrated. It is the
only non-deterministic input to a cycle, and it can only choose between what
`policy.candidates()` produced.

## The chain

```
nakon facts + world state + ROE bounds
        │
        ▼
candidate generation + deterministic scoring      (policy.py — pure, testable)
        │   {id, effect, params, score, reason, cost, high_stakes}
        ▼
typed question bundle ──► clef (/v1/systemone)
        │                  {answers: {...}, usage: {...}}
        ▼
confidence gate (top_prob >= theta[question])
   act                     → deterministic validation still runs (policy.validate)
   below theta, high stakes → LLM arbiter, bounded to the candidate list
   below theta, low stakes  → deterministic candidate #1
        │
        ▼
hard gate (ROE allowlist, kill switch, per-OS lint, per-effect preconditions)
        │
        ▼
execute → record → log (bundle, answer, probabilities, theta, source, chosen)
```

## The questions (the whole catalogue)

| id | type | when | replaces (the earlier agent) |
|---|---|---|---|
| `next_effect` | noul per action (`A`…`H`, `HOLD`) | every cycle | 3 override gates + the fallback ladder's picks |
| `pressure_count` | score 0–3 | every cycle | `press_impacts_per_cycle` + cap heuristics |
| `escalate_now` | noul | every cycle | restore-streak escalation rules |
| `command_intent` | choice: takedown/recon/persistence/benign | per LLM-authored command | `_IMPACTY_WORDS` substring sniff |
| `step_verified` | noul | per improvised one-shot | `ok = any(rc == 0)` |
| `blue_hunting` | noul | every `sensors.blue_hunting_every_n_cycles` | ad-hoc tempo/restore heuristics |

## Reading an answer

- **`choice`**: the winning option is the argmax of `probabilities` (never the
  echoed label); `top_prob = max(probabilities)`. A choice that is not on the
  candidate list is a hallucinated id — the candidate list is the authority and
  the chain falls through to the deterministic pick.
- **`score`**: the expected value over the levels is the count; the levels stay in
  the log so a recalibration can see the distribution, not just the rounded
  number. `top_prob = max(probabilities)`.
- **`noul`**: the answer carries a single yes-probability and no probability map.
  For an action question, **use `p_yes` directly** (0.3 is a confident no, not
  0.7); `top_probability()` keeps the `max(p, 1-p)` meaning only for the
  `escalate_now` / `blue_hunting` questions.
- **`HOLD`** is a separate noul, not a choice option. Hold wins only when
  `p_hold >= theta` and `p_hold >=` every action's `p_yes`; otherwise the highest
  action at or above theta acts, and below theta the old degradation ladder runs.

## The `next_effect` state and questions

Before the action questions, `Policy.clef_state()` renders the readable state:
`minute`, `total_minutes`, `minutes_left`, `phase`, `objective`, the priority
team and why, each team's restore count/trend, each box's OS/access/channel/dead
artifacts, each service's status, the recent events, and the ROE window/modes.
It contains no credentials.

Each of the top actions is one `noul` with a short key (`A`, `B`, …) and a
natural description derived only from `effect`, `params`, `reason`, `score`,
`high_stakes`: effect and target, team/OS, standing status, and whether it adds
downtime now or only protects access. `HOLD` is its own noul. The short-key →
candidate-id map is kept in `meta.option_keys`; the candidate list remains the
authority.

Measured on 40 red event logs (453 `next_effect` decisions, Oct 15–23): the old
8-way `choice` cleared theta 15/453 (3.8%); 0/298 passed with 8 candidates plus
hold, median top probability 0.286. On 11 known high-value cycles the expanded
state + per-action noul chose a high-value action **11/11**, median `p_yes`
**0.811**, **8/11** at or above 0.6. The live probe in this change returned
`nginx p_yes=0.865` with `input_tokens=1814`. The model's probabilities are not
guaranteed calibrated; tune theta on range data, not on one probe.

## Thresholds

`clef.theta` (default **0.60**), overridable per question via
`clef.theta_by_question`. Every call logs `top_prob` even when it passes, because
recalibration reads the log: after each scrim, raise theta on a question whose
confident answers were wrong, lower it on one whose answers were right but
timid. Never tune against the rounded label.

## Degradation ladder

| clef | LLM | behaviour |
|---|---|---|
| up | up | full chain (arbiter available for unconfident high-stakes choices) |
| up | **off in scrims** | clef decides; below theta falls straight to deterministic |
| up | down | everything except command authoring (improvise degrades to a `Result(False)`) |
| down | any | deterministic candidate #1, source=`deterministic`, reason logged |
| down | down | the deterministic ladder (candidate order is the policy's score order) |

The scrim harness disables the free-form LLM arbiter (`llm.enabled: false`): red's
decisions are clef's or the deterministic fallback, never a second model. A deploy may
enable the arbiter explicitly, but the decision log always names the source.
clef failures are counted; `clef.failure_threshold` consecutive failures put the
client in a `clef.cooldown_sec` cooldown so a dead endpoint costs one failed call
per window rather than a timeout every question.

## Batch budget

The whole request (state + questions) must fit the model's physical batch.
**That batch is 16384 tokens** (was 512 at `--ubatch-size 512`
when tlaloc was designed), and the endpoint states it in its own words when it
refuses: `input (17783 tokens) is too large to process. increase the physical
batch size (current batch size: 16384)`. Verified from red01's side of the wire:
a realistic 24-box digest (5370 chars) reports 1826 input tokens and answers
200; a 120-box one (21487 chars) reports **6951** and answers 200; 51848 chars
reports 17783 and 500s.

What that means in practice: the full digest goes out untrimmed, the full option
list (8 candidates + `hold`) goes out, and the compaction machinery below is a
fallback rather than the normal path. Three properties keep it safe, and all
three were paid for on the old 512-token endpoint:

1. **Measured sizing, not a guess.** Token density varies by content: **~0.13-0.34
   tokens/char** for a wide, repetitive digest, **~0.55-0.93** for a compact
   bundle of short ids and punctuation. `chars/4` is optimistic for the latter,
   and an "≤512-token" bundle was once rejected as `input (960 tokens) is too
   large` — silently making every cycle deterministic while every test stayed
   green. `state/digest.py`'s `TOKENS_PER_CHAR` (0.7) is deliberately
   conservative: over-estimating costs a compacted bundle, under-estimating
   costs a 500.
2. **The bundle is built first, then the digest gets the remainder.**
   `Decider.compose()` measures the question bundle, subtracts a 24-token
   envelope margin, and sizes the digest to what is left (capped at
   `DIGEST_BUDGET_CEILING` = 8192 — a digest is a decision input, not a dump).
   When that does not fit it retries with the *compact* bundle (4 options,
   reason-only text, short instructions) and a smaller digest, and the digest's
   own cascade can fall back to identity + access only (`digest_minimal`).
3. **The endpoint is the authority on its own tokenizer.** A rejection of the
   form `input (N tokens) is too large` is parsed, the implied ratio is recorded
   on the client (`learned_ratio`, which then wins over the configured one), and
   the request is recomposed **once** and retried. Never a loop.

If nothing fits, the client raises `ClefError` and the chain degrades
deterministically — logged as `clef_bundle_over_budget` (plus `_over_budget` on
the digest), not silently truncated. Measured floor for a 12-box board with fat
candidate reasons: ~577 tokens, so only a deliberately tiny endpoint hits it.

`pressure_count`, `escalate_now` and `blue_hunting` are each asked as their own
tiny call rather than bundled into the decision request — bundling them spent the
batch twice for the same information. An uncertain `pressure_count` falls back
to **0**, not the configured maximum: the old fallback fired the cap in 191/260
low-confidence cycles even though the mean expected count was 1.37.

## Replay

Every decision record carries its bundle, so the decision layer is testable
offline: `tests/fixtures/decisions-recorded.jsonl` holds recorded bundles +
answers + probabilities, and `tests/test_decide.py` replays each one through
`decide.py` with a stubbed clef, asserting the chain's outcome for that bundle
(`clef` above threshold, `llm_arbiter` for an unconfident high-stakes record,
`deterministic` otherwise). Replay reproduces the environment that produced the
record — a chain that recorded an arbiter decision gets an arbiter; one that
recorded a deterministic pick does not.

## The LLM's only jobs

1. author one command (and its verification command) for `improvise`;
2. arbitrate an unconfident high-stakes choice, bounded to the candidate list;
3. write the after-action narrative.

`llm.chat()` returns `content` only — a reasoning model's `reasoning_content` is
kept on the side (`self.last_reasoning`) and never becomes prose (the earlier
agent put raw chain-of-thought into an engagement report's executive summary that
way). A read timeout is terminal; the identical request is never retried.
