# Smoke report — same-type-2box-2026-09-29, refactor integration gate (2026-10-02)

End-to-end practice run of the post-refactor pipeline (prepare() split, fail-closed verify
gates, golden boot-smoke gate, `config_ops.write_state`, parallelized host loops) against
`10.0.0.193`. Worktree `../tezcatlipoca-smoke`, branch `smoke-2box-20261002` (cut from
`main` @ cdb6911). 564 tests + 92 subtests passed before the run.

## Verdict

**The refactored pipeline works end to end.** Deploy reached live state through all seven
phases, the new boot-smoke gate passed for both goldens, verify registered 6/6 pins with the
exact expected check names, and teardown exited clean on the first run.

One gate FAILED (verify exit 1): **`plant_coverage` — "coverage was never recorded"**. Root
cause is an integration gap in the new fail-closed gate, not a live defect in this deploy:
for a lineup whose configs are all golden-stage (no repair-stage, no final-stage configs —
exactly this comp), phases 5 and 6 skip their nakon passes entirely, so nothing ever writes
`plant_coverage_failed` or `nakon_failed_steps` into `.deploy_state.json`, and the new
fail-closed gate correctly refuses to pass on a missing record. Deploy never records the
golden plant (phase 4 is strict and aborts on failure, so it was assumed unnecessary). Before
the D2 fail-closed change this gate failed open and passed vacuously — so this comp *shape*
passed verify on 2026-09-29 and cannot pass it now. Suggested fix direction (NOT applied, per
no-fixes-during-a-run): have the strict golden plant write an explicit clean coverage record,
or let verify treat strict-golden + empty-repair + empty-final as recorded-clean.

## Environment deviations from the task brief (all verified against the live host)

The task brief's environment facts were stale in three places; the pipeline's own preflight
caught two of them fail-closed:

1. `TF_VAR_template_vm_id=9088` does not exist on the `.193` cluster. First deploy attempt
   aborted at preflight with
   `ERROR: engine base image vmid 9088 (TF_VAR_template_vm_id) does not exist on this cluster`
   — nothing was touched. Fixed by setting `1007` (`base-ubuntu24.04-fix`), the value the
   amongus-cde deploy used on this node (`competitions/amongus-cde-2026/terraform/.env-deploy-193`).
2. The "known-good identity" vmids are taken by other comps now on `.193`: vmid 1230 is
   `golden-ad01` (a live comp's golden, its engine at 1080, team boxes 1400–1413 running).
   Used `--scoring-vmid 1100` instead → engine 1100, engine template 1240, goldens
   1250/1251 — all free.
3. Team identifier 120 (default-derived vmid block 1400–1409) collides with the live comp's
   running boxes at 1400–1413. Exported `TF_VAR_team_identifiers=122` (box vmids 1420/1421,
   subnet 192.168.122.0/24 — free).
4. Engine base preflight + phase-1 sweep confirmed no damage to the foreign comps (1400–1413,
   2410–2414, engines 1080/1900, all kept goldens) — checked explicitly after the run.
5. `TEZ_THIN_HEADROOM` was **not needed**: strict headroom gate passed on its own
   (`hdrives-zfs` 2058 GB free vs ~90 GB needed).
6. One mid-run environmental failure (not a refactor regression): nakon's first phase-4
   plant died on SSH host-key verification — `/home/hna/.tezcatlipoca/known_hosts:33` held
   the stale key of a previous engine that sat at `10.0.0.250`. Removed that one line
   (scoped edit, no other entries touched), resumed `--from-phase 4`. The resume rolled both
   goldens back to `tz-base` and re-planted cleanly — idempotent re-entry works.

The comp dir carried no `.deploy_state.json` in the worktree (only tracked files), so
**box_password was freshly generated, not reused**.

## 1. Commands

```bash
git worktree add -b smoke-2box-20261002 ../tezcatlipoca-smoke main
cd ../tezcatlipoca-smoke && git submodule update --init
cp ../tezcatlipoca/{.env,proxmox} . && cp ../tezcatlipoca/vendor/nakon/.env vendor/nakon/.env && chmod 600 proxmox
python3 -m pytest tests/ -q                                   # 564 passed, 92 subtests
sed -i 's/^TF_VAR_template_vm_id=9088/TF_VAR_template_vm_id=1007/' .env
export TF_VAR_team_identifiers=122
python3 create-competition.py --competition same-type-2box-2026-09-29 --teams 1 --scoring-vmid 1100 --plan-only
python3 -u create-competition.py --competition same-type-2box-2026-09-29 --teams 1 --scoring-vmid 1100 --yes   # died in phase 4
python3 -u create-competition.py --competition same-type-2box-2026-09-29 --teams 1 --scoring-vmid 1100 --from-phase 4 --yes
python3 verify-competition.py competitions/same-type-2box-2026-09-29 --expect-no-vulns
python3 destroy-competition.py --competition same-type-2box-2026-09-29 --yes --full
```

## 2. Deploy

- Banner sequence across both runs: `1→2→3→4` (first run, died in 4) then on resume
  `1,2,3 "Skipped (resume)" → 4 → 4b → 5 → 7`. **No `[6/7]` banner exists on the normal
  path** — `phase6_domains_and_final` prints only its "Skipped (resume)" variant. Checked
  against pre-refactor history (`2cb4fa5~1`): the old monolithic `deploy()` behaved the same,
  so this is a **pre-existing cosmetic gap**, not an extraction regression. (The refactor did
  remove a real bug here: pre-split code printed `[5/7] Skipped (resume).` twice.)
- Boot smoke (new gate): both goldens pass —
  `web01: throwaway clone (vmid 113) reached multi-user — golden disk boots`,
  `win01: … reached multi-user`. It ran after the plant and before `POST /template`, and
  properly required a clean shutdown first.
- Wall time: first attempt 00:44–01:15 (died in the phase-4 nakon plant on the host key),
  resume 01:24–02:25. Sum of timed ops 38.3 min; dominant: engine template build 1914.9 s,
  terraform apply #1 110.1 s, apply #2 91.4 s. Golden clones / boot smoke / nakon plants are
  not timed entries in `.deploy-timings.jsonl` (35 ops recorded) — worth adding if timings
  are meant to explain wall time.

## 3. Verify (`--expect-no-vulns`, exit code 1)

```
logins             : PASS
no_default_creds   : PASS
services           : PASS  UP (query ok) [informational]
pins_registered    : PASS  6 pinned checks
isolation          : PASS  rule present (single team — probe not applicable)
misconfig          : SKIP  --expect-no-vulns [informational]
misconfig_survival : SKIP  --expect-no-vulns [informational]
injects            : SKIP  competition ships no injects/ dir [informational]
round_loop         : PASS  loop advancing
plant_coverage     : FAIL  coverage was never recorded   ← the only gate failure
domains            : SKIP  no domain_roles.json [informational]
templates          : engine f818cafcb053 | golden {"web01": "992d2acba774", "win01": "aea862205620"}
```

Scoreboard checks confirmed via the engine API — exactly the six expected names, all rounds
green: `web01-dns`, `web01-http`, `web01-roundcube`, `win01-iis`, `win01-iis-alt`,
`win01-winrm`. The same-TYPE display-override behavior (`win01-iis` vs `win01-iis-alt`) is
intact.

## 4. Teardown

First run exited clean: 7 terraform resources destroyed, engine 1100 + engine template 1240 +
goldens 1250/1251 + boot-smoke clone 113 gone, `vmbr122` removed. No re-run needed. All
foreign infrastructure verified intact after teardown.

## 5. Findings list

| # | Severity | Finding | Classification |
|---|---|---|---|
| 1 | High | `plant_coverage` can never PASS for a golden-only lineup (no repair/final-stage configs ⇒ no coverage record ⇒ fail-closed gate fails). Blocks verify exit 0 for comp shapes like this one. | Regression of this refactor wave (D2 gate tightened without extending recording to the golden stage) |
| 2 | Medium | Task-brief env facts stale: `TF_VAR_template_vm_id=9088` dead on `.193` (use 1007); vmid 1230 and identifier 120 now occupied by other comps. Preflight caught #1 fail-closed; identifier collisions rely on the operator picking `TF_VAR_team_identifiers`. | Environmental / doc drift |
| 3 | Low | `[6/7]` banner never prints on the normal path. | Pre-existing (verified in pre-refactor code) |
| 4 | Low | Golden clone, boot smoke, and nakon plants emit no `.deploy-timings.jsonl` entries. | Pre-existing observability gap |
| 5 | Info | Stale engine host key in `~/.tezcatlipoca/known_hosts` for a reused mgmt IP kills nakon mid-deploy with a confusing MITM warning. A preflight known-hosts probe (or a pointed error message) would turn this into a 5-second fix instead of a deploy failure. | Environmental; worth a known-issues entry |

## 6. Not verified / caveats

- `misconfig_survival` and `injects` gates are SKIP-by-comp-shape; their new strict behavior
  was not exercised against a comp that actually plants misconfigs/injects.
- The isolation gate's new "blocked requires reachability proof" path was not exercised
  (single-team comp — the gate reports the probe as not applicable).
- The multi-node paths and `--timeout` verify budget were not exercised (single node).
- The `--allow-unverified` waiver path was not tested.
