# M4 benchmark — per-competition template lifecycle, validation matrix results

Date: 2026-09-25/26 (m4-validation-2026-09-25 on realm, node `proxmox`, datastore `hdd`).
Lineup: 2 teams × [web01 (ubuntu24.04-fix), app01 (debian13-lite-fix, realmd member),
dc01 (windows-server, AD DC), win01 (windows-server, domain member)] — the first
Windows/domain run of the v2 pipeline, plus the M4 lifecycle matrix.

Verdict up front: **M4 lifecycle, one-team Windows/domain validation, and the two-team
DomainSID-uniqueness proof are GO.** The original two-team run exposed a DomainSID
collision; DCs now use unbooted goldens. Selective single-golden rebuild (scenario 6)
passed live on 2026-09-26 (see below); the engine-side code-drift warning branch of
scenario 7 was re-run the same day (matrix row 7).

## Template build (run 1, cold)

| op | wall |
|---|---|
| engine template build (clone base → bootstrap → clean → qm template) | **1092 s (18.2 min)** |
| terraform apply #1 (engine as linked clone of the template + bridges) | 146 s |
| prepare-engine-from-template (.env → compose up fresh volumes) | 32 s |
| golden set build (4 goldens: clone + bootstrap + strict plant + convert) | ~590 s |
| terraform apply #2 (8 linked clones) | 355 s |
| prep_apt (4 Linux boxes) | 259 s |

Reuse path (run 2, identical config after teams-only teardown): apply #1 132 s +
engine prep 16 s — **the engine side drops from 1270 s (build path) to 148 s
(~19 min saved per deploy)**, and the golden build is skipped entirely.

## Matrix results

| # | scenario | result |
|---|---|---|
| 1 | cold build + Windows/domain validation | GO (below) |
| 2 | teams-only teardown → unchanged redeploy | engine + all 4 goldens REUSED by hash (identical hashes in verify output); scoring DB empty each run (fresh-volume compose up) |
| 3 | consecutive-run empty DB | PASS — every engine clone starts with fresh volumes (verify logins green each run) |
| 4 | team count 2→1 only | goldens + engine REUSED (hash-match log per box); `box_password` now persists across deploys (golden-hash input — without this, every fresh deploy rebuilt everything) |
| 5 | event.conf change only | engine reused — event.conf is engine-side (pushed per-deploy), excluded from the engine hash by design |
| 6 | one golden-stage config change | **PASS 2026-09-26** (`m4-scen6-2026-09-26`, realm): baseline deployed + verified + frozen; `journal-disk-full` removed from web01's `box_vulns.json`; driver re-run `--from-phase 1` kept golden-app01/dc01/win01 (hash-match log) and the engine template (vmid 1232, hash `1bf6f5e39a74…` unchanged), rebuilt ONLY golden-web01 in slot 1242 (`669e79f377bf…` → `0d8d3e820e24…`); team clones recreated by apply #2 as expected. Post-rebuild verify + re-freeze PASS |
| 7 | freeze → config change → deploy | **both branches proven 2026-09-26 (`m4-scen7-2026-09-26`, realm)**. Config drift: web01 `golden_configs` changed while frozen → top-of-deploy gate refused (`ERROR: golden template for 'web01' changed since FREEZE … golden_configs`) before any phase-1 output; before/after inventory identical (no VM touched, all template description hashes unchanged); restoring the input byte-exact cleared the gate. Engine code drift: temporary edit inside `bootstrap_scoring_engine` → phase-2 `WARNING: engine template code drifted since freeze (bootstrap_scoring_engine) — proceeding on the frozen template`, engine template vmid 1210 reused with byte-identical description hash `47990f83…`; the hash record updated to the post-edit value at phase 2 proves the gate compared exactly that edit. Golden code-drift warnings also fired in the same run (a parallel session changed `build_golden_set` between freeze and the run) — see the phase-1 gap note below |
| 8 | team rebuild on frozen comp | PASS — team1’s four VMs rebuilt from frozen goldens; repair → DC promotion/AD plant → Linux + Windows member joins → final-stage evaluation (no selected final configs) → `tz-ready`; second run verified all domain gates (925 s total) |
| 9 | unfreeze confirmation | PASS — `--unfreeze` refused without `--confirm-unfreeze`; confirmed form removed the freeze, then full verification passed and the range was re-frozen |
| 10 | --full --end-of-competition | PASS — frozen `--full` refused before mutation; confirmed end-of-competition teardown destroyed 4 team clones, engine VM, bridge, 4 goldens, and engine template; reserved M4 VMID scan was clean |

## Windows/domain validation (run 1, A.3)

- **DomainSID: COLLIDED in the original two-team frozen run-3 baseline** — both DCs reported
  `S-1-5-21-3138467823-950274585-2921192615`. The forest's DomainSID derives from the first
  DC's machine SID; both DCs had been cloned from one booted golden. The transferred fix gives
  DC box types an unbooted golden so each team clone specializes before promotion. The current
  one-team rerun and the post-rebuild verification both passed the live AD domain gate; the
  rerun reported `S-1-5-21-2287377195-1353554657-36553089` before teardown. Member machine SID
  duplication remains benign within isolated per-team subnets.
- **Local machine SIDs: duplicated across teams** — both win01 clones carry
  `S-1-5-21-2549159138-1957652177-4266681920` local accounts (identical). Benign:
  domain members use domain-issued SIDs after join; the duplicated local SIDs are
  inert for domain auth.
- **Linked-clone deltas: confirmed for BOTH OSes** — Linux
  (`base-1241-disk-0/vm-1251-disk-0`) and Windows (`base-1242-disk-0/vm-1252-disk-0`)
  share the golden base disk read-only.
- **Three-pass ordering: held live** — repair sweep (21:12) → domains → final
  (disruptive/boot-hostile/identity) pass landed after the domain reboots.
- **Domain readiness ordering** — the transferred fix waits for AD Web Services
  (`Get-ADDomain`) before planting `Add User Account` / `Elevate User Account`, waits for DNS
  SRV records before member joins, and probes/retries transient joins. DC box types now use
  unbooted goldens so each team's promoted forest receives a unique DomainSID; member machine
  SID duplication remains an accepted isolated-subnet property.
- Windows golden plant: strict plant green with `Enable WinRM` (dc01) and
  `Enable WinRM` + `UAC Disabled` (win01) — first Windows configs through the
  golden plant.

## Live-found defects (all fixed on this branch)

1. **1-tuple cmd** — three `bootstrap_scoring_engine` calls kept the pre-refactor
   trailing comma, turning the command string into `("…",)`: every engine-template
   build died in `_fork_exec` ("expected str, bytes or os.PathLike"). Fixed +
   argv guard in `_run_engine_cmd` (fails with full element reprs).
2. **sudoers-rule vars** — payload requires `DROPIN_NAME` + `RULE` (uppercase,
   `:?`-required); the lowercase pin died rc=2 in 0s. REQUIRED_VARS corrected.
3. **Lint blind spots** — `${VAR:?required}` expansions were invisible to the
   brace-closed regex, and type='command' payloads were skipped; lint now scans
   every linux-platform payload blob for any undeclared `${VAR…}`/`$VAR`.
4. **hosts-redirect-linux HOSTS** — payload needs `IP` (identity, auto-filled)
   AND `HOSTS` (operator literal); REQUIRED_VARS updated, pin fixed.
5. **push_event_conf unsudo'd mkdir** — rc=1 inside the root-owned /opt/quotient
   after the template's clean step removed the pre-created dirs; sudo'd.
6. **Per-deploy domain artifacts survived fresh deploys** — run 2's team2 DC
   skipped ADDS because run 1's `.nakon-domain-team2-adds.json` still read as
   done; a fresh deploy now resets them (resumes keep them — that's the guard's
   point).
7. **box_password lifecycle** — a golden-hash input; fresh deploys now reuse the
   competition's box password (spec: "2-team test → 8-team competition must not
   rebuild anything").
8. **destroy-env mismatch** (fixed in `117a5ea`): `destroy-competition.py` now compares
   the loaded environment's endpoint with the competition state's `deployed_endpoint` and
   refuses before Terraform when they differ. Realm teardown still requires the realm
   overrides used for deployment.

## Follow-up live validation (2026-09-26)

- After the frozen scenario-7 checks, the single-team M4 range was explicitly unfrozen,
  redeployed on realm with the new unbooted-DC golden path, verified, and frozen again.
  The deploy adopted only the exact legacy `golden-dc01` VMID/name/`template` tag tuple;
  unrelated untagged resources remain foreign. The DC received a unique DomainSID and the
  Windows and Linux members both joined successfully.
- The first frozen team-rebuild attempt exposed two rebuild-path defects before/while running
  the range: base-template lookup happened before pipeline-v2 golden selection, and the domain
  rerun received the selected post-clone stage file rather than the full machine list. Both
  were fixed; the successful retry rebuilt team1 from goldens and rejoined `app01` and `win01`.
  Post-rebuild verification passed, then the competition was frozen again.
- End-of-competition teardown on realm completed in dependency order. The remaining M4 VMID
  block (`1090`, `1230`, `1240–1253`) and bridge `vmbr105` were absent in the post-teardown
  scan. The separate winad test range was not targeted by this M4 teardown.

## Two-team DomainSID + selective-rebuild validation (2026-09-26, `m4-scen6-2026-09-26`)

Fresh two-team competition on realm (teams 107/108, engine 1092, engine template 1232,
goldens 1242–1245 with dc01 unbooted), exercising the current `improved-parallelism`
code (per-box payload hashes, top-of-deploy `golden_freeze_gate`, unbooted-DC golden,
hardened `check_domains`):

- **Baseline**: deploy → full verifier PASS → freeze (`--windows-domain-validated`).
  Deploy-time ADWS readiness logged distinct DomainSIDs per team
  (`team107.local S-1-5-21-1395927757-…`, `team108.local S-1-5-21-3648460341-…`).
- **Selective rebuild (scenario 6)**: one web01 golden-stage input removed;
  phase 1 kept the three hash-matching goldens + engine template; phase 4 rebuilt only
  golden-web01 (same slot 1242, new hash); description-hash comparison before/after shows
  every other template byte-identical on unchanged VMIDs.
- **Two-team AD contract (post-rebuild, frozen range)**: full verifier PASS — both DCs
  answered `Get-ADDomain` with valid distinct DomainSIDs
  (`team107 S-1-5-21-751245881-3649018028-2086474458`,
  `team108 S-1-5-21-3776041162-1489268753-1056421383`), `svc-support` present on both,
  Windows members `PartOfDomain`, Linux members realm-joined, "2 team domain(s), all
  DomainSIDs unique", plant coverage 8/8 machines, cross-team isolation blocked.
  Log ordering: ADDS → ADWS/DNSRoot up → AD-aware plants → DNS SRV wait → member joins
  (`domain_ops` gates); final stage was empty for this lineup (no final-stage configs
  selected — machines with empty subsets are dropped by `generate_stage_configs`).
- **Live-found fixes during this validation**: `golden_ops` never imported
  `stored_template_hash` (the partial-converted path — exactly what a selective rebuild
  walks — would have died with NameError); `wait_for_proxmox_task` treated PVE's
  `WARNINGS: n` completed exit as a failure; `bootstrap_scoring_engine` lost twice to the
  base image's unattended-upgrades (lock past `Lock::Timeout`, then a killed mid-upgrade
  leaving old libc6 under new `-dev` packages) — the build window now stops the apt
  timers and fully upgrades before installing.

## Divergences from the M4 spec

- Team rebuild + engine recovery validate on the frozen comp directly (the spec's
  "trust phase" item: the catalog has no trust-type config — the AD-misconfig
  chain in domain_ops covers post-promotion flavor; validated as-is).
- The matrix's missing #9 is intentionally covered by the explicit unfreeze-with-confirmation check: refusal without `--confirm-unfreeze`, removal with it, and re-freeze before any end-of-competition teardown.
- Scenario 5 (event.conf) is demonstrated by observation (engine hash excludes
  it; every run pushes a fresh event.conf to a reused engine).
