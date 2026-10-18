# Testing and verification

## Running

```bash
cd tlaloc                            # from the tezcatlipoca checkout root
python3 -m pytest tests -q           # the ONLY runner
python3 -m pytest tests -q -m "not net"     # what CI runs

# from the repo root, the same suite:
python3 -m pytest tlaloc/tests -q
```

`unittest discover` is NOT a runner here. It silently skips pytest-only files
(fixtures, `monkeypatch`, `tmp_path`), which is how the earlier agent's suite
reported green while 50 tests — the whole deploy config-contract, engine-NAT and
keepalive surface — never ran. If you see an `unittest` invocation in CI, that is
a bug.

`pyproject.toml` pins `testpaths`, `pythonpath = ["."]` and registers the
`net`/`live` markers.

## What the suite actually proves

| file | proves |
|---|---|
| `test_core.py` | the gate (kill switch, ROE allowlist, POSIX **and** Windows lint, command caps, retries), config purity + Compfile mapping + `at_min` conversion, World schema/comp refusal, artifact keys, the digest's truncation cascade and its never-drop set, intel (nakon facts, unmanaged boxes excluded, blind fallback) |
| `test_policy.py` | ladder tiers over event progress, the mercy rule, grace/caps/floor, unrecoverable unlocking, the final window suspending the gates, candidate determinism and ladder obedience, the question bundle's shape and batch budget, focus rotation, restore detection |
| `test_decide.py` | the gate (act / arbiter / deterministic), the arbiter bounded to the candidate list, hold semantics, clef-down degradation, per-question thresholds, the expected-value read of a `score` answer, `noul` probability handling, command-intent's conservative floor, **the batch budget** (at 16k the whole state goes out untrimmed and un-compacted — asserted byte-identical to an unlimited-budget frame; at 1k the compact fallback still fits; below the floor it is *flagged*, never silently truncated; an endpoint "too large" rejection is learned from and retried exactly once), the cooldown, **and the replay of recorded bundles** |
| `test_effects.py` | the `execute()` template (kill switch, `ensure_target`, dry-run neutrality, `PolicyError` → failed result + remembered rejection, `GuardrailError` propagates, ledger/artifact/restore bookkeeping, free-form linting), the registry contract and catalogue completeness, the **dry-run sweep over every registered effect**, `recon` and `takedown` behaviour incl. proof-of-damage, artifact naming |
| `test_config_contract.py` | the deploy contract: every `config.DEFAULTS` leaf is in `cli.CONFIG_CONTRACT` (a key the agent can read but the deploy would drop is a feature that is dead on red01 — the earlier agent's defect this guards), every contract entry resolves to a real path, every config path an effect module reads is declared, and the effect-read table is complete against a source scan |
| `test_c2.py` | the Mythic REST mapping (auth, callbacks, attribution by description, freshness, task polling incl. a queued `submitted` not being terminal), the planter (frozen payload, install record without a callback, manifest discipline), `implant_channel`/`task_channel` preconditions and attribution |
| `test_integration.py` | the loop end to end on fakes (decision → gate → effect → ledger → reports), the kill switch halting a run, the consecutive-error breaker, escalation choosing a legal stronger mode, the host guard, the seed stages (full/light/resumable/manifest), the report artifacts and their file modes |

`docs/TEST-PROMPT.md` is the ordered, copy-pasteable version of this table for
someone outside the repo: the suite + clone drill, the invariant audit, the live
clef probe and the engine check, with a verdict format.

## Hermeticity

`tests/conftest.py` makes real I/O fail:

```python
monkeypatch.setattr(subprocess, "Popen", boom)
monkeypatch.setattr(socket, "create_connection", boom)
monkeypatch.setattr(socket.socket, "connect", boom)
```

A test that reaches for a box or a URL raises `AssertionError` with an
instruction to use the fakes (or to opt in with `@pytest.mark.net`). This is not
pedantry: the earlier agent's destroy tests were green because they really did
dial the club LAN and swallowed the failures, and every one of them stalled for
seconds.

## The fakes

`tests/helpers.py` provides exactly four boundaries, all scripted:

- `FakeSSH` — mirrors `exec/ssh.py`'s surface; `on(pattern, rc=, out=)` registers
  responses first-match-wins, every call is recorded (`ssh.sent("mask")`,
  `ssh.targets()`), `rc="raise"` simulates a transport blowup.
- `FakeC2` — `callbacks()`, `plant()`, `task()`.
- `FakeSensors` — the scoreboard mirror (`service_map`).
- `make_ctx(tmp_path)` — a real `World`, `Gate`, `EventLog` and intel on a temp
  state dir, plus `base_cfg(**over)` (validated config with no file layer) and
  `make_intel(comp_dir, blind=)`.
- `tests/fixtures/comp-min/` — a small competition: 2 teams, web01/db01/dc01 and
  an `unmanaged` in-path firewall, nakon facts incl. a pam_permit box and a
  scored port-only pin, plus a Compfile.

## Verified offline, still owed on a range

The suite proves the agent's logic. It cannot prove the club's live
infrastructure, and this repo does not pretend otherwise:

- a live Mythic (install via Gaia, comp-prep payload freeze, DNAT);
- a real scrim through the engine (deploy including the seed → run →
  report/verdict → destroy);
- the restore table's recipes against real boxes.

`python3 -m tlaloc --dry-run run --cycles 1` is the closest local end-to-end:
a whole cycle, no box touched. Dry-run short-circuits `run()` but not `check()`,
so it also shows which candidates the ROE rejects.
