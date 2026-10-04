# Offline harness + tooling validation (release 0.2.0 split) — 2026-10-04

Branch `offline-harness-2026-10-04` off `release-0.2.0-legacy-removal` (b2a680f). No Proxmox, no
credentials, no LLM spend. Baseline suite: 1049 passed.

## Results

| Area | Result | What ran |
|---|---|---|
| scrim-report.py vs pre-split | PASS | Both versions (`git archive b2a680f^`) run on 22 run dirs (`~/.tezcatlipoca/automated-tests/*/*`, sibling worktrees' `competitions/*/.automated-tests/*`, recent pytest temp run dirs): all exit 0. 18 identical apart from one trimmed sentence in "Data limitations" (the dropped "Future runs get the full set." — wording). 1 real diff, `scrim-one/run-b6b96a7e`: `windows_footholds` 2 -> 0, gates failed 3 -> 4. This is the intentional removal of the legacy octet table: that comp's `boxes.json` no longer exists, so no Windows octets resolve and the gate counts none. |
| run-agent-scrim.py offline stages | PASS | `--help`; missing `--competition`, bad name, unknown comp (fresh and `--resume-event`, no comp dir minted), bad `--red-mode`; run-dir/test-folder resolution stable across resume and `--run-dir` override; manifest + `resume_intent`/`resume_refusal`; `stage_blues` rendered against a temp copy of `agent-scrim` and every generated helper checked (`bash -n` on mybox/scorch/qlogin/myscore/submit-inject, `py_compile` on score.py, `scrim.env` 0600, no unsubstituted placeholders in opencode.jsonc); `watchdog_script` shell-checked; cycle prompt render; `scoreboard_delta`; `monitor_loop` one tick and `stage_capture` against canned Quotient bodies (empty in-flight round skipped); blue/engine evidence globs; inject re-anchor plan; red tunnel argv. New test: `tests/test_scrim_offline_stages.py` (11 tests). |
| webui | PASS | `webui/server.py` imports; every route driven through TestClient on a temp competitions dir with Popen stubbed (comps, boxes, pins, injects, packet, catalog, templates incl. the live `config_ops.list_proxmox_templates` path, nodes, deploy/plan/verify jobs, job log, 404/400 paths). Every flag the deploy job builds was checked against `create-competition.py --help`; verify target script exists. The webui only reaches `constants` and `config_ops.list_proxmox_templates` (re-exported by `config_ops`): both resolve. New test: `tests/test_webui_routes.py` (5 tests). npm not run. |
| Remaining tooling | PASS | `--help` for create/destroy/redeploy/verify/compile-packet/generate-packet/test-artifacts/sync-template/observe-soak/run-schedule/round_loop_guard; import of template_sync_ops, beacon_ops, round_loop, firewall_ops; `generate-packet.py` on a scratch copy of all 14 competitions (ok); `run-schedule.py` and `compile-packet.py --dry-run` on both packets (maccdc-q-2026 schedule needs a compiled comp dir: expected error); `sync-template.py --dry-run` refuses without nodes.json (expected). `quotient/setup.py` is a library, not a script (`engine_event_ops` imports it). |
| Name resolution, whole repo | PASS | AST scan of every .py (incl. hyphenated scripts, webui, tools, tests): every `module.attr` and `from module import name` against local modules resolves (verified the scanner flags a planted bad name). 443 `mock.patch("mod.attr")` / `patch.object(mod, "name")` targets in tests all exist. |
| Stale references | FIXED (docs/comments) | No remaining `deploy_phases` module, `destroy_cloned_vms`, `--legacy-tags`, `--allow-untagged`, `TEZ_ALLOW_UNTAGGED_RECLAIM` outside `docs/reports/` (historical) and the `tests/test_deploy_phases.py` filename. No `deploy._x` / `verify._x` / `range_ops._x` private access. Every documented `TEZ_*` env var exists in code. Stale line/module refs fixed: `docs/known-issues.md` (destroy-competition.py:126-141/162 -> `destroy_sweep_ops.pre_stop_windows_boxes`; range_ops.py:668 -> `targets.enumerate_targets`), `verifier/state_gates.py` and `tests/test_verify_gates.py` (`deploy.py:105` -> `deploy_lib/coverage.py`), `artifacts_lib/transport.py` (`run-agent-scrim.py:1906` -> `scrim.red_link.pull_red_snapshot`), `artifacts_lib/lifecycle.py` and `tests/test_teardown_artifacts_hook.py` (`:487-491`), `windows_ops.py` comment. |

## Refactor regressions found

None in code. All the scanning above found no broken import, moved-name access or patch target.

## Pre-existing defects noticed (not refactor regressions, not fixed)

- `scrim/staging.py` `DEFAULT_TEMPLATE` is `competitions/agent-scrim-2026-09-17b`, which does not
  exist (the tree has `competitions/agent-scrim`); `--new` without `--from-template` fails. Already
  listed in `docs/reports/2026-10-04-hardening-plan.md`.
- `scrim-report.py --self-test` still needs a positional `run_dir` and only then asserts the pinned
  17c numbers; no 17c run dir is on this machine.
- `tools/vnc_shot.py` has no `--help` (positional argv, IndexError without arguments).

## Not testable offline

Real Quotient/engine HTTP behavior, ssh/scp to red01 and boxes, the opencode cycle runner, red
tunnel liveness, `stage_capture`'s engine pause, terraform/Proxmox phases, `sync-template.py`
transfers, `observe-soak.py` sampling, `run-schedule.py --execute`, the webui deploy/verify jobs
against a live range, the built frontend, and `scrim-report.py --self-test` (needs the 17c run dir).
