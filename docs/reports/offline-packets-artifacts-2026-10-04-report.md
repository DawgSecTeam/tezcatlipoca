# Offline validation — packets, artifact lifecycle, multi-node, split modules (2026-10-04)

Branch `offline-packets-2026-10-04` (worktree `../tezcatlipoca-offline-packets`, cut from
`release-0.2.0-legacy-removal` @ b2a680f). Nothing here touched Proxmox: no `.env`, no
credentials, no deploy, no `terraform plan/apply`. Baseline suite 1056 passed; after this work
1095 passed.

## Results

| Area | Result | How |
|---|---|---|
| 1. Packet pipeline | PASS | `compile-packet.py` (`--dry-run` and real, into a scratch `--competitions-dir`) for cde-2026 and maccdc-q-2026; `generate-packet.py`; new `tests/test_packet_pipeline_offline.py` chains compile -> deploy input loaders -> `mint_competition_secrets` -> `write_credentials_file` -> `verifier.packet` gates; `verify-competition.py --packet ... --engine-ip 127.0.0.1` run end to end on a compiled bundle (packet_creds PASS, other gates SKIP/FAIL for the missing engine, no traceback). |
| 1b. `create-competition.py --plan-only` | PASS | `deploy_lib/cli.py` read first: `--plan-only` only calls `print_plan` (reads boxes.json / .deploy_state.json, prints, returns); `select_named_competition` can scaffold a missing comp dir, so it was run from a scratch cwd with copies. Ran for amongus-cde, cde-2026, a compiled cde-2026, maccdc-q-2026, scale8-scrim-2026-10-01. |
| 2. Artifact lifecycle | PASS | New `tests/test_artifacts_lifecycle_offline.py` (synthetic comp, fake scp/ssh transport): `collect_for_teardown`, `list/show/plan/collect --dry-run/verify/archive/verify --seal` through the real CLI subprocess, teardown-twice idempotence, dead-box tolerance. Also run read-only (on a copy in scratch) against `../tezcatlipoca-live-parallel/competitions/same-type-2box/.automated-tests/run-b0687393`: list/show/plan/verify/collect --dry-run/archive all behave. |
| 3. Multi-node | PASS | New `tests/test_multinode_offline_e2e.py`: 3-node nodes.json in a temp cwd, fake Proxmox behind `pve_api.proxmox_api_for`; real `resolve_placement` (probes) -> `placement.json` -> real `preflight_gates_multinode` -> `build_terraform_inputs` (satellite tfvars/routes/slots/run_tag, 0600) -> per-satellite `jump_rules`. `terraform init -backend=false && terraform validate` in `terraform/`: valid. Every tfvars key the code writes is a declared terraform variable. |
| 4. Split modules | PASS with 1 latent bug fixed | see below |

## Regression / defect found and fixed

1. **Phase 5 in-path firewall bootstrap would TypeError** (`deploy_lib/phases/firewall.py`
   calls `write_team_configs(comp_dir, teams, red_dnat_spec=...)`; the function required an
   unused positional `fw_box`). Found by a call-signature sweep of every repo-internal call
   (inspect.signature + ast), not by any test. It is **not** a refactor regression — the
   pre-split `deploy_phases.py` had the identical call and `firewall_ops` the identical
   signature — but no test or live run ever reached it (no `in_path` box deployed yet), so the
   first firewall deploy would have died at phase 5. Fix: drop the unused parameter.
   Test: `tests/test_phase5_firewall_offline.py` (fails without the fix).
2. `artifacts_lib/report.py` front-matter told authors to run `test-artifacts.py seal`, a
   subcommand that does not exist (it is `verify --seal`). Pre-existing; one-line fix, asserted
   in the lifecycle test.

## What was checked in item 4 (no further regressions found)

* `pyflakes` over all non-vendor sources: no undefined names.
* Import-everything script (279 files) and `--help` on every entrypoint script: all exit 0.
  (Four `competitions/*/pfsense/gen_pfsense_config.py` and `artifacts/.../inspect_targets.py`
  are top-level scripts that need argv/files; not importable by design.)
* Attribute sweep: every `module.attr` and `from module import name` between repo modules
  resolves (0 problems / 274 files). Signature sweep: every positional/keyword call into a repo
  function/class binds (1 problem = defect 1). Attribute sweeps of `ctx.*` and the stage
  dataclasses (`spec/secrets/place/terraform/generated/identity/prior/inputs/targets`) in
  `deploy_lib/`: 0 missing fields. No import of the deleted `deploy_phases`, no leftover
  `allow_untagged`/`legacy_name`/`legacy_clones`/`cloned_vms` references in code.
* AST function-level comparison of the 824 pre-split functions (`git show b2a680f^:`) against
  the new tree: 652 identical, 148 changed, 24 gone. Every changed/gone function in
  `deploy_lib/phases/`, `redeploy_*_ops`, `destroy_*_ops`, `vm_ownership`, `golden_ops`,
  `preflight/`, `verifier/`, `scrim/`, `scrim_report/`, `artifacts_lib/` was diffed: the
  differences are module qualification, extraction into helpers (helper bodies re-read against
  the removed inline code), and the intended 0.2.0 legacy removals (`allow_untagged`,
  `legacy_name`, `--legacy-tags`, v1/v2 branches, clone-marker adoption now in
  `ownership_verdict`). Preflight unification (single- vs multi-node) re-checked clause by clause
  against the old `preflight_gates` / `preflight_gates_multinode`: same checks, same ordering of
  fatal conditions.
* Differential run: old `scrim-report.py` (b2a680f^) vs new `scrim_report` on the three real
  run dirs in `../scrim-runs/` — byte-identical output except one deliberately dropped
  sentence ("Future runs get the full set."). `--self-test` passes on both.
* New tests executing previously-unexecuted code: `redeploy-competition.py main()` (selectors,
  gate, dry-run, reset ladder, snapshot refusal, cancel, full dispatch table, --reset-event),
  `redeploy_light_ops`, `destroy_templates_ops`, phases 1 (multi-node walk), 5, 7, `finish`.
  Coverage of `redeploy_gate_ops`/`redeploy_select_ops` went from 0%/18% to covered.

## Observations (not fixed — judgement calls or out of scope)

* `test-artifacts.py collect --dry-run` on an **already-collected** test rewrites
  `collection.json` with `skipped / "dry run"` records, discarding the sha256 provenance of the
  real pull (files stay on disk, `verify` then has less to re-hash). Identical in the pre-split
  `artifacts_ops.collect`, and the dry-run help text says it records everything as skipped, so
  left as is; a safer behaviour would be to not write `collection.json` on `--dry-run` when one
  exists.
* The committed `competitions/cde-2026/` bundle is older than what `compile-packet.py` now
  emits for `packets/cde-2026/packet.yaml` (compile adds the unmanaged `fw01` pfSense stand-in
  and `box_baseline.json`; the committed `boxes.json` has 4 boxes, compile has 5). Deploying the
  committed bundle is not deploying the packet. The packet's `fw01` is `unmanaged: true` without
  `in_path`, so it is cloned but never put in path (documented as a manual runbook step).
* `competitions/amongus-cde/packet.md` is stale relative to `generate-packet.py` output (the
  generated table adds `ftp` to skeld's services). Regenerating was reverted; the tracked file
  is unchanged.
* `scrim-report.py --self-test <run_dir>` writes `INTERACTION.md` into the run dir it is given.
  I ran it on `../scrim-runs/agent-scrim-2026-09-17c`, whose `INTERACTION.md` already existed;
  it was rewritten (last by the old-code run, so with the original text including the sentence
  that was later dropped). Contents derive deterministically from the run dir, so the
  expectation is no net change, but it is a shared directory outside this worktree.

## Not testable offline

Anything that needs a live estate: real Proxmox probes/clones/destroy semantics and tag
behaviour, `terraform plan/apply` (tfvars *types* are only checked by name against
`variables.tf`; `validate` does not read tfvars), the in-path pfSense console driving and engine
cutover (`bootstrap_firewalls`, `cut_over_engine`, `verify_in_path` are faked), guest-agent
transports (`_guest_files`), real scp/ssh to red01, nakon plant behaviour against real boxes,
Quotient round-loop/scoreboard gates, the scrim harness stages (`scrim/cli.py`, `fire_test.py`
have 0% test coverage and need an engine; only their static call/attribute resolution was
verified — another worktree, `tezcatlipoca-offline-harness`, is covering the harness), and the
`webui` server's deploy actions.
