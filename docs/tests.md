# Tests

The offline suite is a regression net over the deploy-path helpers, not a pipeline test. It never
touches Proxmox, never opens a network connection, and never needs `.env`, credentials, or a
submodule checkout. The real integration gate is
[`verify-competition.py`](usage-agents.md#verify-competitionpy) against a deployed range — nothing
in `tests/` replaces it.

## Patching

Each name is defined in exactly one module; `patch.object` only takes effect on the module whose
function *looks the name up*, so patch the module that owns the caller, not one that merely
imports it — e.g. `golden_disk_ops` for disk helpers, `nakon_run_ops` for `run_nakon`'s
collaborators, `ubuntu_auth_ops`/`apt_dns_ops`/`service_fixup_ops` for hardening,
`engine_identity_ops`/`engine_cmd_ops` for engine steps, the matching `redeploy_<mode>_ops` for
each redeploy mode, and within `infra/`: `infra.client.proxmox_api`, `infra.guest_exec.*`,
`infra.ownership.*`, `infra.lifecycle.*`, `infra.collisions`/`resources.proxmox_api`. An unpatched
`proxmox_api` in a preflight module makes a real network call.

## Run them

```bash
python3 -m pytest tests/ -q          # the whole suite; prints the current count
python3 -m pytest tests/test_packet_compile.py -q
python3 -m pytest tests/ -q -k frozen
git ls-files tests/                  # the current file list — don't hard-code it here
```

One `conftest.py` exists for exactly one reason: `estate.config` is a deliberate process
singleton (multi-node activation points the whole process at the engine's host), so the
fixture resets it around each test. Otherwise each file is standalone `unittest`-style, safe
in any order. A
handful skip themselves when an optional tool is missing (e.g. `pyflakes` in `test_smoke.py`), so
a skip is not a pass.

## What the files cover

Grouped by concern. File set and test count both drift — read them from `git ls-files tests/` and
the pytest run above rather than this table.

**Test artifacts** (`artifacts_lib/` — see [internals.md](internals.md#artifacts_lib))

| File | Guards |
|---|---|
| `test_artifacts_ops.py` | Manifest, planning, collector statuses, stubs, report skeleton, seal, archive |
| `test_test_artifacts_cli.py` | `test-artifacts.py` subcommands, exit codes, output |
| `test_scrim_artifacts.py` | The scrim harness's calls into the facade (`collect`, `ingest_verdict`, `finalize`) |
| `test_teardown_artifacts_hook.py` | Teardown's artifact step through `destroy.artifacts_ops` |

**The estate component** (`estate/` — the one component that mutates infrastructure)

| File | Guards |
|---|---|
| `test_estate_state.py` | The monolithic document: schema version, resource record/forget/filter, checkpoints, the flat-dict round trip, `load_all` |
| `test_estate_config.py` | The config object: file/env/override precedence, canonical `TEZ_` env-name derivation (`knobs.` prefix dropped), `require`/`is_set`, per-node activation, SSH key resolution |
| `test_estate_apply.py` | The create path against a fake API: bridge create, engine NIC order and cloud-init, per-box NIC/cloud-init rules (Windows and unmanaged skip it), adopt-ours/refuse-foreign, grow-only disk sizing |
| `test_estate_engine_setup.py` | The engine's guest-side setup: netplan ens numbering, the sysctls, the persisted satellite-route unit, cold boot, readiness |
| `test_estate_destroy.py` | Teardown tier order (clones before goldens — the base-volume constraint), a refusal that never aborts a tier, `full=False` keeping templates |

**Range/config contracts**

| File | Guards |
|---|---|
| `test_preflight_unified.py` | The one preflight: `ownership_verdict` classes, single-/multi-node share parity, collision/ours/bridge-tolerance rules |
| `test_team_vmid_arithmetic.py` | Team identifier vs engine-derived vmid-slot arithmetic |
| `test_unmanaged_box.py` | An unmanaged (pfSense/appliance) box is skipped by the nakon plant and golden set but keeps its positional vmid |
| `test_golden_unbooted.py` | `unbooted_golden_boxes`: absent role file = no-domain lineup; present-but-invalid fails closed |
| `test_golden_dc_contract.py` | Unbooted-DC golden contract: stage configs and golden build/convert for `dc` roles |
| `test_domain_infra_pins.py` | `ADDS` domain-infra pins, plus IIS HTTP / Alpine ssh service mappings |
| `test_event_conf_pins.py` | Multiple same-check-TYPE pins on one box, and per-pin check overrides |
| `test_username_lint.py` | `box_username` legacy-account lint |
| `test_domain_ops.py` | Domain-chain ordering (ADWS/DNS gates precede AD actions) and the rebuild path's config choice |

**Golden / template lifecycle**

| File | Guards |
|---|---|
| `test_frozen_gate.py` | Frozen-gate machinery: per-box `payload_hash` selectivity, `golden_freeze_gate` classification, `frozen_keep` |
| `test_repeat_run.py` | The reuse loop run twice: stranded clones, stale locks, repeated-run blockers |
| `test_golden_disk.py` | Golden-set disk sizing |
| `test_root_disk_expansion.py` | Guest-side root-disk expansion (cloud-init/partition parsing on btrfs) |
| `test_interrupted_clone.py` | Interrupted-clone recovery: clone marker, stale-lock unlock, orphan handling |
| `test_parallel_golden.py` | `build_golden_set`'s snapshot/convert passes on the 4-worker pool without weakening delete-snapshot→convert ordering |
| `test_nakon_stages.py` | Four-stage split safety (golden / pre-domain / final / last): the golden identity ban on both golden paths (slot 0 and every satellite), the combined post-clone pin merge, and account-file mode pins landing in the last pass |

**Deploy robustness**

| File | Guards |
|---|---|
| `test_smoke.py` | CI smoke gate: undefined names/syntax errors anywhere, plus an offline `--plan-only` run |
| `test_auth_ladder.py` | SSH retry ladders: definitive rejections fail fast instead of burning the full ladder |
| `test_non_apt_prep.py` | apt-prep/settle scripts are honest fast no-ops on non-apt distros (fedora, alpine) |
| `test_thin_headroom.py` | Datastore headroom gate and the `TEZ_THIN_HEADROOM` factor |
| `test_destroy_continuation.py` | A failed/interrupted destroy resumes instead of restarting |
| `test_stream3_robustness.py` | AD-misconfig replays, `.postclone-swept` invalidation |
| `test_windows_bootstrap.py` | Windows bootstrap script content (firewall profiles, RDP listener checks) |
| `test_redeploy_hardening.py` | Redeploy hardening stays Linux-scoped — Windows boxes never reach the Linux-only executors |
| `test_verify_domains.py` | `check_domains` fails closed on DSID shape, role-file schema, box names |
| `test_verify_injects.py` | Verify's closed-inject warning |

**Deploy module split, sequencing, pipeline API**

| File | Guards |
|---|---|
| `test_deploy_sequencer.py` | Phase order, checkpoint gating (only a phase that ran may write `last_phase`), one-banner-per-phase, the engine-context hook between phases 2 and 3, the mid-phase failure/resume hint |
| `test_deploy_pause.py` | A pause request holds the pipeline at the end of the phase in progress (a skipped phase is no boundary), the hold is on disk as `paused_after_phase`, deleting the request resumes in-process, and an unset `TEZ_PAUSE_FILE` leaves manual deploys alone |
| `test_deploy_gates.py` | The single-gate-point contract: `prepare()` runs `gates.check_resume_gates`/`run_range_gates` before any secret is minted; `deploy()` takes the lock first; a held lock refuses |
| `tests/_deploy_patch.py` | Not a test: `dpatch(name)` patches one name in every `deploy_lib` module that binds it |
| `test_deploy_phases.py` | Phase-boundary defects offline: golden hash loop (every box, unmanaged included), the `--from-phase` resume guard, phase 6's always-persist coverage repair |
| `test_deploy_phase_units.py` | Per-phase units for `deploy_lib/phases/` with every infra call patched |
| `test_pipeline_api.py` | `create-competition.py --plan-only` runs through `pipeline_api` |
| `test_engine_steps.py` | `engine_cmd_ops`: every remote step names itself on failure, and Quotient `.env` secrets never appear in argv |
| `test_redeploy_state.py` | Redeploy's state writes go through the document API; its Linux hardening order holds (sudoers grant before DNS fix) |
| `test_jump_parallel.py` | `build_jump_vms` runs satellites concurrently (bound 4) without dropping one or turning a hard abort into a partial build |

**verify's gate model**

These tests load `verify-competition.py` by path for its re-exported gate API; anything patched
(an SSH hop, `run_concurrent`, `guest_agent_exec_*`, a gate inside `main()`) is patched in its
defining `verifier.*` module — the entrypoint only re-exports.

| File | Guards |
|---|---|
| `test_verify_gates.py` | Gate correctness: fail-closed plant coverage, tri-state SKIP semantics, the isolation decision table, strict-services "nothing scored" failure, SUMMARY/exit-code single source of truth (`--allow-unverified`) |
| `test_parallel_verify.py` | Domain/misconfig-survival/beacon loops on the 8-worker pool: parallel and re-serialized runs produce byte-identical stdout, statuses, exit code |

**Packets, scrim, hygiene**

| File | Guards |
|---|---|
| `test_packet_compile.py` | Packet-profile compilation and the pin semantics it leans on (score-only pins, credlist swaps, dual credit) |
| `test_scrim_harness_teams.py` | Inject clocks re-anchored at T0, every scoreboard/evidence loop scaling past 2 teams |
| `test_blue_watchdog.py` | The blue watchdog script shape (pure string build, no SSH) |
| `test_amongus_cde.py` | Windows service mappings and post-domain AD configs |
| `test_multinode.py` | Multi-node placement, jump rules, slot vmid math, the sync planner — offline (the largest file) |
| `test_secret_hygiene.py` | No secret-bearing file is tracked, and no env var drifts — fails the suite if code reads a variable `estate/config.py`'s `_SPEC` does not declare and `INTERNAL_ENV` does not allowlist |
| `test_packet_validation.py` | Compile-boundary packet shapes that would otherwise die later in a live deploy |
| `test_service_fixups.py` | `fix_services_on_boxes`'s generated hardening script is byte-identical to the pre-refactor output across the full service case matrix |
| `test_scrim_defects.py` | Red-vs-blue scrim defect audit: process-tree timeouts, worker supervision, lineup, secret hygiene. Imports `scrim/`/`scrim_report/` directly and patches where a function is defined (module map: [scrim-harness.md](scrim-harness.md)) |

## Adding one

1. `tests/test_<subject>.py`, matching style: `unittest.TestCase`, temp dirs, `unittest.mock.patch`
   for anything that would otherwise reach Proxmox or the network.
2. Never import a subprocess — import the module under test directly (dash-named entry points load
   via `importlib.util.spec_from_file_location`, as `test_smoke.py` shows). Set env vars inside the
   test and restore in `tearDown`.
3. Put the invariant being protected in the module docstring's first line.
4. A new env var your code reads: add its key to `estate/config.py`'s `_SPEC` (the default lives
   there; the canonical `TEZ_` env name is derived from the key) and document it in
   [usage-people.md](usage-people.md#configure-the-event), or list it in `INTERNAL_ENV` in
   `tests/test_secret_hygiene.py` with a reason — the drift test fails the suite otherwise, by
   design. `NAKON_*`/`VULNDB_*` names are vendored-tool config read by `vendor/nakon`, not this
   repo.
5. `python3 -m pytest tests/ -q` and confirm the count went up.

## What the suite deliberately does not do

- No Proxmox/Quotient/nakon integration, no live range, no VM.
- No end-to-end phase ordering (closest: `test_smoke.py`'s offline `--plan-only`).
- No timing/performance assertions — budgets are documentation, not tests.

For anything the suite can't reach, the gate is a real deploy plus `verify-competition.py`; see
[e2e-testing.md](e2e-testing.md).
