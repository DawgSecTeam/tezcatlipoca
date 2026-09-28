# winad-testrun 2026-09-25 — M4 validation, Windows/AD test run, and hybrid scrim

Branch `win-ad-testrun` (from `108184c`), cyberfield range (10.0.0.193). Competition:
2 teams × [dc01 Windows DC, win01 Windows member, web01/db01 Ubuntu, app01 Debian] —
Linux members realmd-joined, beacons + 10 injects. **Nothing pushed.**

## Verdict
The M4 per-competition template lifecycle works, but it shipped in `108184c` **completely
untested** — the very first live run hit NameErrors on the opening statements of
`run_nakon` and the engine-template build, i.e. the feature path had never executed. 18
fixes later, the full validation matrix is green and a live hybrid scrim ran end to end.

## Validation matrix (cyberfield, 2–3 teams)
| # | Scenario | Result |
|---|---|---|
| 1–2 | Fresh build + verify (all gates) | PASS |
| 3 | Windows/AD: **unique DomainSID per team**, all joins, three-pass order, ADWS wait | PASS (2 & 3 teams) |
| 4 | Teams-only teardown → reuse | PASS — all 5 goldens + engine template reused |
| 5a | event.conf change | engine + goldens reused |
| 5b | team-count change (2→3) | everything reused |
| 6 | one golden's config changed | only that golden's hash moves (deterministic offline proof + live keep/rebuild) |
| 7a | frozen + config drift | **hard-fail before phase 1, nothing destroyed** |
| 7b | frozen + code drift | warn + proceed (unit) |
| 8 | team-2 rebuild | only team2 rebuilt; **team1 + engine ran continuously untouched** (uptimes) |
| — | engine recovery | engine re-cloned from template in 46s; **`-target` bug found + fixed** |
| 9 | unfreeze | refused without `--confirm-unfreeze`, succeeded with it |
| 10 | full teardown | frozen `--full` refused without `--end-of-competition`; clones-before-templates; clean vmid scan |

Reuse saves **~29 min/run** on this lineup (golden set ~19, engine template ~10). apply #2
cut **2128s → 351s** with the agent-wait cap. Teams-only teardown ~123s.

## Bugs found & fixed (18 commits, `108184c..HEAD`)
Regressions from the M4 refactor (never-run code):
- `3e08670` `run_nakon` lost its `subprocess.Popen`; `_quote_sshkeys`/`stored_template_hash` unimported.
- `0745848` engine bootstrap shell commands were 1-tuples (trailing comma).
- `1c01a10` `push_event_conf` used plain `mkdir` on root-owned `/opt/quotient/config` (template clean removes credlists dir).

Windows/AD:
- `7ff120b` Windows bootstrap raced sysprep's first-boot reboot (90s exec timeout) → wait for `ImageState`, retry agent drops.
- `82619b6` **all teams got the same DomainSID** (every DC linked-cloned from one booted golden). DC box type now uses an *unbooted* (generalized) golden so each clone specializes its own SID; its configs move to the per-team repair pass. Also: wait for ADWS before AD plants (`svc-support` was silently never created), probe-confirmed join retry.
- `c4e2377` a fresh deploy after teardown kept stale `.nakon-domain-*-adds.json`, so re-cloned DCs were "assumed promoted" and never promoted → all joins failed. Decide promotion by probing the DC; phase 1 drops the markers.
- `4727512` verify gained an automated domain gate (DC serving, joins, AD plants, DomainSID uniqueness) replacing an operator attestation.

Pin/plant:
- `cb0dd78` the pin-var table had the wrong var names (`sudoers-rule` needs `DROPIN_NAME`+`RULE`; `hosts-redirect-linux` needs `IP`+`HOSTS`, `IP` = attacker addr) and the bundle lint **skipped `type=command` steps** — exactly the parameterized catalog configs. Lint now covers them (`${VAR:?}` = required); 108/124 cached bundles clean, 16 flag only real misses.
- `b0f6c87` the apt-settle check matched Ubuntu's permanent `unattended-upgrade-shutdown` daemon → every Ubuntu box read BUSY forever, burning the full 240s each pass (**the bulk of the "516s prep_apt"**). Match the worker by cmdline.

Template lifecycle / hashing:
- `b7f17a2`+`a26859f` **the golden hash used the whole-bundle `bundle_id`, shared across box types → any single config change rebuilt all four booted goldens.** Now each golden hashes its own plan's `script_sha256` list (build-stable; `tarball_sha256` was not). Phase 1 also sweeps stranded clones left by a team-count decrease.
- `a4cbb58` bpg waited up to 15 min/box for a guest-agent IP (serial) — 20+ min dead time on Windows lineups. Cap at 30s; deploy's own readiness waits gate progress. Engine hash narrowed to the `scoring_engine` resource block (was whole `main.tf`).
- `27a91ef` the engine kept the base image's 10 GB root on a 40 GB disk → **full within one deploy** (apt-cacher 500s, Postgres next). Grow root every deploy; prune build cache; hash the clean step.
- `41c54ac` terraform derived the ownership tag from the Compfile **display name**, not the competition dir — the shipped `example` comp would tag VMs phase 1 then refuses to delete; punctuation broke apply.

redeploy paths:
- `a99beca` team rebuild hard-coded `scsi0` for the disk resize → every Windows (sata0) rebuild failed; engine recovery didn't clear phase-7 flags so its advertised re-seed silently skipped.
- `af96425` verify's isolation probe picked the Windows DC at `.2` → SKIP on every Windows lineup. Probe from a Linux box.
- `0d5926a` **engine-recovery's `terraform -replace` wasn't `-target`ed** → it also destroyed/recreated team boxes that had drifted from state (which `--mode rebuild` produces by design), wiping a team mid-event. Now `-target`ed to the engine + its null_resources.

## Design recommendations (priority order)
1. **CI smoke gate.** `108184c` had import/tuple errors on the first executed line of two core functions. A `python -m pyflakes *.py` + a `--plan-only` that actually runs generate/hash (it currently exits before generation) would have caught most of the day's first three bugs for free.
2. **A repeated-run test for the reuse loop** — M4's headline feature. Three separate blockers (stranded clones, stale domain markers, box-password churn) each meant the *second* run of a competition failed; none survive a single deploy today without the fixes. The teardown→redeploy loop needs an automated 2× run.
3. **Interrupted-clone leftovers are unrecoverable by the token.** A host reboot / kill mid-clone leaves an *untagged, clone-locked* VM in a golden slot that fails preflight as "foreign" and blocks every future deploy — and only `root@pam` can unlock it. Tag the clone target at/just-after clone, and let phase 1 recognise an untagged locked VM in its own slot. Same class: orphaned child zvols (disk with no VM) block golden rebuild; GC them on the specific destroy failure.
4. **Kill terraform on driver interruption.** Killing the Python driver leaves `terraform apply` running as a grandchild holding the state lock and still mutating infra (the scrim harness already learned this — use a process group / `killpg`).
5. **Drop the "warm apt cache in the engine template" premise** — it's empty at template-build time (nothing has used the cacher yet); the cache that matters is built on the linked-clone engine during the deploy and dies with it. Either persist the cacher volume or stop claiming the speedup.
6. **Windows access after promotion / rebuild polish:** rebuild + rerun-domain apply Linux-only post-clone steps (cloud-init wait, `fix_services`) to Windows boxes → harmless but noisy guest-agent failures; `engine-recovery` ignores `--yes` (prompts via `input()`).

## Hybrid scrim (60 min, live)
Red = bad-auto autonomous on red01 (vmid 998, gpt-5.6-luna, intel=nakon). Blue = two Claude
Code subagents (one per team) defending + injects, via tested `mybox`/`myscore`/`submit-inject`
helpers. Red ran the **full clean 60 min** (self-stopped at duration; 168 events: 63 actions —
42 service-impact, 8 cred-spray, 6 privesc, 4 beacon-plant; focus_team team2; 4 footholds).

**It was a real interaction, not parallel monologues:** red's own log recorded blue restoring
`web01-http` on *both* teams at T+2 min, then re-attacked. team1 held ~8/8 the whole event
(one 3-min web01 blip) and its blue tore out win01's Wardline persistence (3 services, 3 Run
keys, 2 scheduled tasks). team2's blue found the `svc-support` rogue Domain Admin and submitted
inject 01, but **both subagents were killed mid-event by API errors** (team2 the session limit,
team1 a transient DNS blip) — so red's focus-team nginx stop/mask on team2 web01 went
uncontested in the back half (~21 min down). Caveat: blue coverage was partial for that reason,
not a scrim-mechanics failure; a clean re-run with the limit reset would give full-duration blue.

Evidence: `scrim-runs/winad-testrun-2026-09-25/` (`evidence/red/events.jsonl`, `world.json`,
`red-report.md`, `scoreboard-state.jsonl`, per-team `NOTEBOOK.md`/`sub-01.md`).

---

## Addendum: reconcile + scrim 2 (2026-09-26/27)

### Reconcile
The `improved-parallelism` branch had independently fixed the same M4 bugs (different code —
`payload_hash`/`golden_payload_hash` vs this branch's `plan_sha`/`golden_machine_plan_sha`, 7
conflict regions). Its own staged WIP already ported the unbooted-DC-golden and domain-verify-gate
fixes. Reconciled as: commit that WIP as a checkpoint, then squash the remaining unique fixes
(engine root-grow, prep_apt settle-check cmdline match, engine-recovery `-target`, bpg 30s cap +
narrowed engine hash, stranded-clone sweep, redeploy disk-iface/phase-7-flag fixes, Windows
ImageState wait) onto it as one commit (`8931e70` on `improved-parallelism`). `pyflakes` clean,
single hash/frozen-gate implementation confirmed. New branch `winad-scrim` (worktree
`tezcatlipoca-winad-scrim`) cut from there for the re-run; nothing pushed.

### Scrim 2 setup
Same live `winad-testrun-2026-09-25` competition, reset in place (`redeploy-competition.py --mode
rollback-ready`, all boxes) rather than a fresh deploy. Stale red01 destroyed and redeployed
(`bad-auto deploy --start`, vmid 998, 60 min, intel=nakon). Blue changed from "one long subagent
run" to **operator-supervised bounded shifts** (~18 min each, continued via message rather than
re-spawned, so a mid-event API error costs one shift, not the whole defense) — the direct fix for
scrim 1's uncontested ~21-min gap.

**T0 = 22:47:03Z** (red service start; ~7 min after the intended 22:40:25Z anchor). Red self-stopped
cleanly at **23:47:07Z**, full clean 60:03 run, 0/success, no crash.

### What actually happened
Red planted a raw-socket beacon (binary disguised as `/usr/local/lib/netstatsd/netstatsd`, UDP to
gateway:4470) on both teams' web01, plus the same rogue-DA/Wardline-persistence/hosts-poison/
sudoers-sabotage/SUID/hidden-cron playbook as scrim 1. Both blue shifts eradicated it thoroughly
(uid-0 rogue accounts, backdoor SSH keys, `svc-support` rogue Domain Admin, `transit` added to
BUILTIN Administrators, hidden cron, SUID GTFOBins, and — team2's shift 2 catch — extensive
`C:\ProgramData\Wardline\` services/tasks/Run-keys on **both** Windows DCs/members that shift 1 had
falsely reported clean (plain-`cmd` quoting swallowed the check; fixed by routing all Windows work
through base64 `-EncodedCommand`).

The recurring, unresolved fight was **web01 nginx**: red logs in over SSH as `medic` using a
harvested/shared password, privescs to root, and runs `stop_mask` on nginx. Team1's own log shows
this exact cycle repeating **every ~65–70s from 23:19:47 to 23:46:49** (red's own `events.jsonl`) —
28 minutes of continuous re-masking. Blue's shift-1 fix (`systemctl unmask; enable --now`) was
correct but not durable, because it never closed the actual entry vector (medic's password over
SSH); team1's dying shift-2 agent correctly diagnosed this and was mid-way through disabling SSH
password auth when it was killed by the account-wide rate limit (see below).

**Both blue subagents were killed by the same event — an account session-limit 429, not a
per-agent flake** — partway through shift 2. Team1's agent had one page of very sharp diagnosis
in flight ("red logs in as medic using medic's password... I can lock it out via
`PasswordAuthentication no`, my own access is pubkey") and died before applying it. Team2's agent
died mid-idle (a timed monitoring sleep). Because the rate limit is account-wide, **no resume was
possible until the limit's own reset time**, which landed *after* red had already finished its run
and self-stopped. Net effect: team1's web01-http was down, unopposed, from **23:10:03Z until
manually restored at ~01:12Z** — roughly two hours, almost entirely after red itself had stopped
attacking, simply because nobody was watching. Team2's web01-http similarly went down at
23:46:45Z (red's last act before self-stopping) and sat down until the same manual restore.

**Wrap-up (post-limit-reset), done directly rather than via a further blue shift** since red had
already finished and the marginal value of a third subagent shift was low: unmasked/restarted
nginx on both web01 (back to 8/8 UP both teams, confirmed via each team's own `/api/services`
scoreboard poll), killed and removed the still-live `netstatsd` beacon process+binary on both
web01 (no persistence mechanism found — cron/systemd/rc.local all clean — so kill+`rm` was
sufficient), and applied team1's identified fix (`PasswordAuthentication no` in `sshd_config` +
the cloud-init drop-in, reloaded) on both web01 to close the harvested-credential re-entry path.
Pulled red's `events.jsonl` (174 lines, passwords already `***`-redacted at source) and `world.json`
from red01 as evidence before it could be torn down.

### Inject-window finding (root cause, reconciled precisely)
Both blue teams independently reported all 12 injects as `{"error":"Inject is closed"}` for the
entire scrim, and both separately pinned the close time at **~21:22Z** — well before scrim 2's own
T0 (22:47Z). This is real and reproduces exactly: inject `open/due/close` fields are
**`_offset_min` minutes from the competition's scoring-event start**, not from whenever a scrim's
red/blue actors actually start acting, and the "rollback-ready" range reset re-baselines the
boxes but **does not restart Quotient's event clock or reopen the inject window**. The event's
last `create_injects` phase-7 deploy in `.deploy-timings.jsonl` is `2026-09-26T15:22:23`
(logged in local America/New_York time, UTC-4) → **19:22:23Z**; the latest inject's
`close_offset_min` is 120 → **21:22:23Z**, matching both agents' reported close time to the
second. Both blue teams still did the remediation work behind every inject's checklist on the
live boxes (see notebooks) and wrote their deliverables as evidence, correctly declining to burn
further shift time on resubmission once the mechanism was understood.

### Design recommendations (additions to the priority list above)
7. **Bounded blue shifts are necessary but not sufficient against an account-wide rate limit.**
   Scrim 1's fix (shorter shifts, continued not re-spawned) assumed failures were per-agent and
   independent; scrim 2 hit a single account-wide session limit that killed **both** shifts at
   once, with no ability to resume until its own reset — which landed after the event window had
   closed. A live scrim needs either a non-LLM dead-man's-switch defense (e.g. a cron'd
   `systemctl unmask/enable` sweep for the scored unit list) that keeps services up through an
   API outage, or scrim scheduling that leaves slack against the current usage window before
   committing to a fixed end time.
8. **Inject scheduling needs a real anchor**, not the last `create_injects` deploy timestamp.
   Either `--mode rollback-ready` should offer to re-anchor/reopen the inject window to "now" for
   scrim/rehearsal use, or the offsets should be resolved against a scoring-engine "round zero" /
   event-start concept that a range reset actually rewinds. As shipped, any reset-and-rerun of a
   live competition (exactly this scrim's use case) silently invalidates every inject before the
   rerun even starts.
9. **A durable service-impact fix needs the entry vector closed, not just the symptom masked.**
   Red's most effective single play across both scrims was medic-password SSH → root → mask
   nginx. `unmask; enable --now` treats the symptom and gets re-undone every cycle; the actual
   fix (kill password auth, keep key auth) is cheap and was already diagnosed correctly by blue —
   it just needs to happen *before* the agent that found it gets to act on it.

Evidence: `scrim-runs/winad-scrim2/` (`T0.txt`, `scoreboard-state.jsonl`, `red-evidence/events.jsonl`
+ `world.json`, per-team `blue-team{1,2}/NOTEBOOK.md` + `sub-*.md`).
