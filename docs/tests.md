# Tests

The offline suite is a **regression net over the deploy-path helpers**, not a pipeline test. It
never touches Proxmox, never opens a network connection, and never needs `.env`, credentials, or a
submodule checkout. The real integration gate is
[`verify-competition.py`](usage-agents.md#verify-competitionpy) against a deployed range — nothing
in `tests/` replaces it.

## Run them

```bash
python3 -m pytest tests/ -q          # the whole suite; prints the current count
python3 -m pytest tests/test_packet_compile.py -q
python3 -m pytest tests/ -q -k frozen
git ls-files tests/                  # the current file list — don't hard-code it here
```

No fixtures or `conftest.py` exist; each file is standalone `unittest`-style and safe to run in any
order. A handful skip themselves when an optional tool is missing (e.g. `pyflakes` in
`test_smoke.py`), so a skip is not a pass.

## What the files cover

Grouped by what they protect. Several exist because of a specific incident — those carry the run
name in the docstring, which is the fastest way to find the "why". File set and test count both
drift, so read them from `git ls-files tests/` and the pytest run above rather than from this table.

**Range/config contracts**

| File | Guards |
|---|---|
| `test_team_vmid_arithmetic.py` | Team identifier vs engine-derived vmid-slot arithmetic (live-found 2026-09-29: identifiers whose 10-slot block overlapped the engine/template/golden block) |
| `test_unmanaged_box.py` | An unmanaged (pfSense/appliance) box is skipped by the nakon plant and the golden set but keeps its positional vmid |
| `test_golden_unbooted.py` | `unbooted_golden_boxes`: absent role file = no-domain lineup; present-but-invalid file fails closed |
| `test_golden_dc_contract.py` | Unbooted-DC golden contract: stage configs and golden build/convert for `dc` roles |
| `test_domain_infra_pins.py` | `ADDS` domain-infra pins, plus IIS HTTP / Alpine ssh service mappings |
| `test_event_conf_pins.py` | Multiple same-check-TYPE pins on one box, and per-pin check overrides (`display`/`port`/`credlist`) |
| `test_username_lint.py` | `box_username` legacy-account lint (distro-matrix-2026-09-27: an operator-chosen name collided with a distro account and bricked auth setup) |
| `test_domain_ops.py` | Domain-chain ordering (ADWS/DNS gates precede AD actions) and the rebuild path's machine-config choice |

**Golden / template lifecycle (M4)**

| File | Guards |
|---|---|
| `test_frozen_gate.py` | Frozen-gate machinery: per-box `payload_hash` selectivity, `golden_freeze_gate` classification, `frozen_keep` |
| `test_repeat_run.py` | The M4 reuse loop run twice (winad-testrun rec 2): stranded clones, stale locks, repeated-run blockers |
| `test_golden_disk.py` | Golden-set disk sizing (svc-matrix: a 15 GB template disk filled mid-plant) |
| `test_root_disk_expansion.py` | Guest-side root-disk expansion (regression-4x1: cloud-init/partition parsing on btrfs) |
| `test_interrupted_clone.py` | Interrupted-clone recovery (winad-testrun rec 3): clone marker, stale-lock unlock, orphan handling |
| `test_golden_boot_smoke.py` | The boot-smoke gate (2026-09-24 `systemd-system-masked`): stop → smoke → convert ordering, fail-closed tri-state, the `golden_boot_smoke 0` waiver, throwaway-clone teardown |
| `test_parallel_golden.py` | `build_golden_set`'s snapshot/convert passes on the 4-worker pool without weakening the boot-smoke barrier or the delete-snapshot→convert ordering |
| `test_nakon_stages.py` | Three-stage split safety: the golden identity ban on both golden paths (slot 0 and every satellite), and the combined post-clone pin merge |

**Deploy robustness**

| File | Guards |
|---|---|
| `test_smoke.py` | CI smoke gate: undefined names / syntax errors anywhere, plus an offline `--plan-only` run |
| `test_auth_ladder.py` | SSH retry ladders: definitive rejections fail fast instead of burning the full ladder (cyberrange loadtest 2026-09-30) |
| `test_non_apt_prep.py` | apt-prep / settle scripts are honest fast no-ops on non-apt distros (fedora, alpine) |
| `test_thin_headroom.py` | Datastore headroom gate and the `TEZ_THIN_HEADROOM` factor for thin-provisioned pools |
| `test_run_terraform.py` | `run_terraform` process-group isolation, so an interrupted driver cannot orphan terraform |
| `test_destroy_continuation.py` | A failed/interrupted destroy resumes instead of restarting |
| `test_stream3_robustness.py` | shakedown-5x4 closeout fixes: AD-misconfig replays, `.postclone-swept` invalidation, related robustness |
| `test_windows_bootstrap.py` | Windows bootstrap script content (svc-matrix: template firewall profiles and RDP listener checks) |
| `test_redeploy_hardening.py` | Redeploy hardening stays Linux-scoped — Windows boxes never reach the Linux-only executors |
| `test_verify_domains.py` | `check_domains` fails closed on DSID shape, role-file schema, and box names |
| `test_verify_injects.py` | verify's closed-inject warning (winad-scrim2: every inject read as closed) |

**Deploy module split, sequencing & the pipeline API (2026-10-01/02)**

| File | Guards |
|---|---|
| `test_deploy_sequencer.py` | The sequencer after the phase split: phase order, checkpoint gating (only a phase that ran may write `last_phase`), the byte-identical one-banner-per-phase set, the terraform-context hook between phases 2 and 3, and the mid-phase failure/resume hint |
| `test_deploy_phases.py` | Phase-boundary defects offline: the M4 golden hash loop (every box gets an entry, unmanaged included), the `--from-phase` resume guard against `last_phase`, and phase 6's always-persist coverage repair |
| `test_deploy_phase_units.py` | Per-phase units for the extracted `deploy_phases.py` — the branching, ordering and state writes that used to be buried in `deploy()`, with every infra call patched |
| `test_pipeline_api.py` | `pipeline_api`'s explicit surface: one minimal `__all__`, every name resolved from the module that owns it, and no return of the importlib `driver` loader |
| `test_helper_dedup.py` | One definition per shared helper (audit 2026-10-02): `is_windows_template`, `os_to_platform`, and the generated-secret charset cannot be silently re-duplicated |
| `test_engine_steps.py` | engine_ops: every remote step names itself when it fails (timeout/non-zero instead of an undifferentiated SSH argv), and the Quotient `.env` secrets never appear in argv |
| `test_destroy_teardown.py` | `terraform destroy` is bounded per attempt and a timeout is a retryable failure (pfsense-rvb: a hung Windows-DC destroy held the lock and printed nothing) |
| `test_redeploy_state.py` | `.deploy_state.json` writers all go through `config_ops.write_state` (D1, winad-testrun), and redeploy's Linux hardening order holds (sudoers grant before the DNS fix) |
| `test_jump_parallel.py` | `build_jump_vms` runs satellites concurrently (bound 4) without dropping one or turning a hard abort into a partial build (multinode-spread-2026-09-30) |

**verify's gate model**

| File | Guards |
|---|---|
| `test_verify_gates.py` | verify gate correctness, each against a reproduced defect (D1–D8): fail-closed plant coverage, tri-state SKIP semantics, the isolation decision table, strict-services freshness, and the SUMMARY/exit-code single source of truth (`--allow-unverified`) |
| `test_parallel_verify.py` | verify's domain / misconfig-survival / beacon loops on the 8-worker pool, with parallel and re-serialised runs producing byte-identical stdout and the same statuses and exit code |

**Packets, scrim, hygiene**

| File | Guards |
|---|---|
| `test_packet_compile.py` | Packet-profile compilation and the pin semantics it leans on (score-only pins, credlist swaps, dual credit) |
| `test_scrim_harness_teams.py` | shakedown-5x4 event fixes: inject clocks re-anchored at T0, and every scoreboard/evidence loop scaling past 2 teams |
| `test_blue_watchdog.py` | The blue watchdog script shape (pure string build, no SSH) |
| `test_amongus_cde.py` | amongus-cde-2026 wiring: Windows service mappings and post-domain AD configs |
| `test_multinode.py` | Multi-node placement, jump rules, slot vmid math, and the sync planner — all offline (the largest file) |
| `test_secret_hygiene.py` | Repo hygiene: no secret-bearing file is tracked, and no env var drifts — it fails the suite if code reads a variable neither declared in `.env.example` nor allowlisted in `INTERNAL_ENV` |
| `test_bundle_lint.py` | Bundle var-lint false positives that once blocked a valid deploy (live-found 2026-09-26) |
| `test_packet_validation.py` | Compile-boundary packet shapes that used to survive `validate_profile` and die later in a live deploy — some only at phase 6, after DC promotion (D3) |
| `test_service_fixups.py` | `fix_services_on_boxes`' generated hardening script is **byte-identical** to the pre-refactor output across the full service case matrix |
| `test_scrim_defects.py` | The red-vs-blue scrim defect audit findings D1–D12 (process-tree timeouts, worker supervision, lineup, secret hygiene) — each pins a live-only failure path |

## Adding one

1. Put it in `tests/test_<subject>.py`. Match the existing style: `unittest.TestCase`, `tmp_path`-
   style temp dirs, and `from unittest.mock import patch` for anything that would otherwise reach
   Proxmox or the network.
2. **Never import a subprocess.** Tests import the module under test directly (the dash-named entry
   points load via `importlib.util.spec_from_file_location`, as `test_smoke.py` shows). If a helper
   needs `os.environ`, set it inside the test and restore it in `tearDown` — `test_team_vmid_arithmetic.py`
   is the pattern.
3. Put the incident or invariant in the module docstring, first line, with the run name if there is
   one. That is what makes a later failure legible.
4. If your code reads a new environment variable, declare it in `.env.example` (the naming
   standard there: `TF_VAR_<name>` / `TEZ_<NAME>` / `NAKON_*`/`VULNDB_*`) and add it to
   [usage-people.md](usage-people.md#configure-the-event). A variable that is none of those kinds
   must be listed in `INTERNAL_ENV` in `tests/test_secret_hygiene.py` with a reason;
   `test_secret_hygiene.py` fails the suite otherwise, by design.
5. Run `python3 -m pytest tests/ -q` and make sure the count went **up**.

## What the suite deliberately does not do

- No Proxmox/Quotient/nakon integration, no live range, no VM.
- No end-to-end phase ordering. The closest thing is `test_smoke.py`'s offline `--plan-only`.
- No timing/performance assertions — budgets are documentation, not tests.

For anything the suite cannot reach, the gate is a real deploy plus
`python3 verify-competition.py competitions/<id>`; see
[e2e-testing.md](e2e-testing.md) for how to run one and triage its failures.
