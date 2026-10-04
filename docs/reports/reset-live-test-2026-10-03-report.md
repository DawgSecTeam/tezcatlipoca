# Per-box reset — live validation report (2026-10-03/04)

Worktree `../tezcatlipoca-reset-test`, branch `reset-live-test-2026-10-03`, node cyberrange
(.150), datastore `wkshp-pool`, engine mgmt IP **10.0.0.252** (see finding F5). Comp:
`reset-test` (cde-2026 shape, 1 team × ad01/ftp01/web01/db01), then `scrim-reset` (2 teams) for
the mid-run workflow. Feature under test: `redeploy-competition.py --mode reset` (commit
0b5d7c4) plus the hardening it rides on.

## Verdict

**The per-box reset feature works end-to-end and is live-proven.** Every rung, both escalation
triggers, both domain paths, ownership stamping, failure semantics, scoring recovery, and the
mid-run workflow validated. The matrix also surfaced **four real defects** (F1–F4, fixed on this
branch) and **two findings handed off** (F6 upstream catalog, F7 harness/packet — open).

## Matrix results

| # | Step | Result | Evidence |
|---|---|---|---|
| T0 | `--mode reset --dry-run` | PASS | Per box: full ownership tags incl `run-e7c05c6d`, snapshots `tz-base, tz-ready`, rung preview = 1/3 for all |
| T1 | db01 wrecked (mysql stopped+masked, /etc/mysql deleted) → reset | PASS | Rung 1 rollback → probe healthy (`ssh + scored ports`), summary "fixed via 'tz-ready' rollback", **exit 0**; 3306 confirmed OPEN from engine |
| T2a | db01 wrecked + `tz-ready` overwritten with the wrecked disk → reset | PASS (after F1–F3) | Rung 1 restored the wrecked disk → probe **unhealthy (closed 3306)** → rung 2 dropped the newer `tz-ready`, reached `tz-base`, re-hardened, re-took `tz-ready` → healthy, **exit 0** |
| T2b | ftp01 (Windows member, known-dead `smb 445` pin) full ladder | PASS as designed | Rung 1 → probe refuses → rung 2 with **member domain re-join (ok)** → rung 3 **Windows rebuild** (bootstrap, re-join, both snapshots) → probe still refuses 445 → **STILL BROKEN, exit 1** — the probe correctly never calls a box healthy while a scored check is down |
| T3 | db01 VM destroyed → reset | PASS | No snapshots → straight to rung 3 → rebuilt from **golden 1403 (linked clone)**; API GET: tags `comp-reset-test;run-e7c05c6d;tezcatlipoca` + description `tezcatlipoca-clone comp-reset-test`; both snapshots re-taken; exit 0 |
| T4 | ad01 (DC) paths | PASS | Rung-1 DC rollback validated 3× (see F4 for the wreck-methodology lesson); forced rung 2 via `tz-ready` deletion: rollback to pre-ADDS disk → **stale ADDS marker cascade** → re-promotion (DomainSID unchanged by design) → healthy, exit 0 |
| T5 | PAM planted-box trap (observational) | did not fire | 4 stop/starts of planted boxes (db01 ×2, ftp01 ×2) all came back SSH-clean; the ladder's preauth signature stays armed for when it does fire |
| T6 | Failure semantics | PASS | With goldens hidden + VM gone: rung-3 config error absorbed, summary "STILL BROKEN", **exit 1**; state restored → re-run → rebuilt, **exit 0** |
| T7 | `destroy-competition.py --full` (no escapes) | PASS | Clean destroy incl. 3 rebuilt boxes — ownership stamping proven end-to-end; inventory re-check: zero leftovers |

Scoring recovery: baseline 8/9 (constant known-dead `ftp01-smb`) → after the full matrix
6/9 (scoreboard lagging the last rebuild) → **8/9 within two rounds, verify RESULT: PASS**.

## Mid-run workflow (scrim-reset, 2 teams × 4 boxes, routed red01)

The harness deployed + fired clean, then **died at its own pre-T0 verify on the packet gates
(F7)** — before the agent window. The range stayed live and scoring, so the mid-run reset was
executed from a second shell against it: team1/db01 `--mode reset --yes` → exit 0, rung 1, and
the scoreboard returned to **9/9 UP on both teams, verify PASS**. Cleanup: `badauto destroy`
(red01, firewall rules stripped) + `--full` teardown, inventory clean.

## Findings fixed on this branch

- **F1 — pipeline v3 broke the golden-path gates** (4de4d8e): `mode_rebuild`/`prepare_nakon_assets`
  gated on `pipeline_version == 2`; a v3 range fell into the v1 template path, whose engine-base
  exclusion then refused the box template outright (box base == engine base vmid on this env).
  Now `_is_golden_pipeline` (>= 2).
- **F2 — `tz-base` rollback was latently broken on PVE** (4de4d8e): PVE only rolls a disk back to
  its MOST RECENT snapshot, and `tz-ready` always postdates `tz-base` — "can't rollback, 'tz-base'
  is not most recent snapshot". `mode_rollback` now drops the newer `tz-ready` first (the
  replant/rebuild re-takes it). This also fixes standalone `--mode rollback-base`.
- **F3 — empty postclone stage crashed the replant** (9f9bacb): comps whose plants are all
  golden-stage (cde-2026 style) legitimately have a zero-machine `.nakon-postclone.json`;
  `build_nakon_bundle` refuses empty machine lists. `prepare_nakon_assets` now returns no stage
  and the replant skips nakon while still re-running auth/DNS/hardening.
- **F4 — a reset DC now voids ALL of the team's domain markers** (907fd1f): deleting only the
  ADDS marker re-promoted the DC but left `svc-support` missing (the `ad-misconfigs` marker
  survived and skipped account re-creation) and skipped the member re-join; verify's domain gate
  failed. The whole `.nakon-domain-<team>-*` set is now cleared on a DC reset, and the member
  join is forced (the member's "already joined" self-report is stale against a fresh AD).
  Live-validated: markers cleared → chain re-ran → `svc-support present`, verify domains PASS.

## Handed off / open

- **F5 — engine mgmt IP .250 is poisoned on this cluster's tailnet path** (known long-standing;
  re-confirmed live): the default `DEFAULT_ENGINE_MGMT_IP=10.0.0.250` dropped SSH mid
  `docker compose build` (exit 255). Resumed with `TF_VAR_engine_mgmt_ip=10.0.0.252`. Worth a
  default change for .150-based envs.
- **F6 — upstream nakon catalog: "Domain Join" cannot re-join an already-joined member**:
  after a DC reset, the member self-reports joined and `Add-Computer` refuses ("already in that
  domain"), so the trust stays dead (no machine account on the fresh AD). Needs a
  reset-trust/leave-first branch (`Reset-ComputerMachinePassword` or `Remove-Computer`+rejoin).
  Recorded in [upstream-defects-handoff.md](upstream-defects-handoff.md).
- **F7 — harness packet-gate mismatch (open, not reset-related)**: `stage_author`-authored
  `scrim-reset` regenerated a `packet.md` containing none of the profile's decoy accounts
  (scorebot/blackteam/red_scoring), and the harness's pre-T0 `verify --packet` then failed
  `packet_creds`/`packet_accounts` and killed the run. Triage: packet compile vs profile vs
  verify expectation. Left in [known-issues.md](../known-issues.md).
- **Test-methodology lesson (F4 context): a stopped-service Windows wreck is not
  disk-persistent** — service Stop-Service state is RAM-only and `sc config` registry writes
  flush lazily; a rollback's reboot undoes both. The DC's rung-1 rollback healing it is correct
  behavior; forcing escalation needs a `tz-ready` deletion (or a genuinely disk-corrupting wreck).

## Leftovers observed, not touched

The scale8-scrim-2026-10-01 leftovers (jump VM **2031 running**, goldens 2060–2064) are still on
.150 — foreign to this run per the run-ownership rules; reclaiming them is the owner's call via
the matching comp dir.

## Cost

Phase 0+1 ≈ 4.5 h wall (two deploys, one .250-trap retry), Phase 2 ≈ 2.5 h, teardowns clean.
Offline suite: 1040 green with the new regression tests (ladder policy, rollback ordering,
v3 gates, empty-postclone, marker cascade, stamping, failed-steps merge).
