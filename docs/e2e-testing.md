# E2E testing — running deploys without repeating failures

> **Pipeline version 2** — golden templates + linked clones, phases renumbered (2026-09-24).
> Phase numbers below are v2; the [v1 → v2 table](architecture.md#v1-to-v2-migration-what-moved)
> maps them to the pre-golden numbering that older post-mortems use. Where this doc quotes a
> historical run's phase, the run's own numbering is kept and said so.

How to run a full pipeline test (`create-competition.py` / `run-agent-scrim.py`) so that
failures are triaged once, recovered cheaply, and never debugged twice. This doc condenses
every deploy failure from the July 2026 shakedown through the September scrim / winad / pfSense
red-vs-blue runs; [known-issues.md](known-issues.md) stays the canonical incident log,
[architecture.md](architecture.md) explains the phases, [usage-agents.md](usage-agents.md)
documents the flags, and [pfsense-inpath-2026-09-28.md](pfsense-inpath-2026-09-28.md)
is the in-path-firewall runbook. For a pfSense-fronted, multi-host, or red-team run also read §8.

The three habits this doc exists to enforce:

1. **Triage before touching anything** — regression or known failure class? (§1)
2. **Never redo phases 1–2 for a failure in phases 3–7** — resume, don't rebuild. (§4)
3. **Always capture the deploy log** — several post-mortems were only possible by luck. (§3, §7)

## 1. Triage first: regression or expected?

Before debugging a failed deploy, answer: *what changed since the last green run?*

1. `git log --oneline <last-green>..HEAD` and diff anything that touched a `.py`/`.tf`
   deploy-path file. Comment-only changes don't count — verify before blaming a commit.
   (Example: `af7468c` stripped all comments; AST comparison of every module showed the code
   byte-equivalent, so same-day failures were not regressions.)
2. Match the failure signature against the table below. Only failures that match **nothing**
   get a full debugging session — and get added to this table afterwards.
3. A real regression is a commit pair: introduced-by → fixed-by. Record both in the post-mortem.

| Failure class | Signature | Verdict | Status |
|---|---|---|---|
| strict × pin density | phase 4 `nakon deploy --strict` → `CalledProcessError` exit 1, per-step `FAILED` lines | Expected at max-vuln pin counts: one broken vulndb row aborts the whole phase. Some legacy rows had failed *silently* for months before strict made them visible. **Only the phase-4 golden plant is strict**; phases 5 and 6 pass `strict=False` and record a tally instead | Chronic — recover with trim-then-resume (§5), fix or avoid pinning broken rows |
| timeout sizing | `subprocess.TimeoutExpired` on apt/compose in phase 3 (engine prep) | Was chronic (300s compose build, 180s apt, 60s compose-up all tripped on slow cold starts) | Fixed — engine budgets raised to 600s (`63b9c31`, `0c0547e`) |
| node/storage saturation | HTTP 596, authed API hangs, node reboot, stale `lock = clone` surviving reboot | Environmental — clones saturating the datastore the template lives on; concurrent external provisioners make it worse | Recurring risk — preflight pool headroom, no concurrent provisioners (§7) |
| apt package rotation | apt 404 mid-deploy (e.g. `nginx 1.24.0-2ubuntu7.4x` vanished) | Environmental, self-heals after apt-daily refresh | Known — retry later or repair post-hoc |
| cloud-init clone race | clone has no IPv4 / APIPA; ifupdown address dropped on carrier blip; resolv.conf reverted | Environmental — much rarer on v2 (linked clones come off an already-cleaned golden disk) | **No v2 auto-repair exists**: `clone_ops._repair_box_network`/`ensure_cloned_network` were deleted with `clone_ops.py`. A dead clone now surfaces as `wait_for_boxes_ssh` failing with `diagnose_unreachable_box` output; fix the VM (Terraform replace) or resume |
| domain-join vs ADDS race | Domain Join fails "domain does not exist" right after promotion | Timing race — dc01 still settling | Mitigated (`487b977`: skip re-promotion via ADDS artifact + live membership probes); resume phase 6 and retry |
| deploy-path regression | anything unclassifiable, correlates with a logic-touching commit | Real regression | One on record: `2662fb7` iterated dict keys → `TypeError` in `ensure_cloned_network` at the old phase 6; fixed `9015f06` same day. (`ensure_cloned_network` and `clone_ops.py` are gone in v2 — historical row) |
| event-side (not deploy) | opencode cycle deaths, scoreboard `Forbidden`, red-evidence scp hangs, wrong systemd unit names | Harness/agent-side, not the deploy pipeline | Fixed + policed by `docs/rehearsal-gates.md` |
| engine apt-lock race | phase 2 engine bootstrap `rc=100`, `Could not get lock /var/cache/apt/archives/lock` held by a fresh apt-get | First-boot `unattended-upgrades`/`apt-daily-upgrade` re-spawns apt *after* the preamble's killall; `DPkg::Lock::Timeout` doesn't cover the archives-cache lock | Fixed 2026-09-27 (`engine_ops.py`): `systemctl mask` the units + poll `fuser` on all three locks until free before touching apt |
| non-apt distro prep | fedora/alpine box: `apt-get: command not found` ×5 retries, 240 s settle burned per pass | Box apt-prep + settle-check assumed Debian | Fixed 2026-09-27 (`hardening_ops.py`): `command -v apt-get \|\| exit 0` guard before any apt call |
| Fedora member gaps | app01 (Fedora) golden plant `Bad authentication type`; post-boot httpd/named DOWN; guest-agent flaps | Fedora Cloud image ≠ Ubuntu: SSH password-auth off (`50-cloud-init.conf`), `named` binds localhost-only, httpd hangs on `ServerName` DNS at boot before the DC is up | Fixed by baking `base-fedora44-fix` (cloud-init + `00-tzc-pwauth.conf`) and, per box, `ServerName localhost` + `named listen-on { any; }`. Fold into the template/catalog (still a TODO) |
| teardown stuck DC | `terraform destroy` hangs "Still destroying… Nm elapsed" on a Windows DC | bpg provider does a *graceful* shutdown (long `timeout_shutdown_vm`); a DC whose qemu-guest-agent is down never shuts down | Hard-kill the qemu process: `kill -9 $(cat /var/run/qemu-server/<vmid>.pid)` — terraform then deletes the stopped VM. Pre-`qm stop` the other DCs first |
| scoring stopped post-reboot | scoreboard frozen; `/api/engine` `last_round.StartTime` is stale, `current_round_time` zero | The round loop does not auto-resume after an engine VM reboot | `POST /api/competition/start {started:true}` then `POST /api/engine/pause {pause:false}`; a fresh round appears within `Delay` seconds |
| pfSense in-path | engine→box scoring times out through pfSense, or the firewall clone won't boot | WAN pass rule used a raw CIDR `<network>` (silently dropped); host-side ZFS config-write breaks pfSense boot (OpenZFS 2.4.4 > pfSense's loader) | See §8 and `docs/pfsense-inpath-2026-09-28.md`: `<network>lan</network>`, and inject config guest-side (console `fetch`), never host-side ZFS |

## 2. Failure history digest

What actually happened, condensed. "Clean" runs are listed because they bound what the
pipeline does *not* break on.

| Run | Date | Outcome | Dominant failure class | Recovery used |
|---|---|---|---|---|
| July shakedown (cde-test, e2e-2026-07-20) | 07-09→07-20 | 15+ pipeline bugs fixed | Everything: silent apt failures, DNS ordering, 596 under parallel clones, compose timeout | Fix + redeploy (~15 commits) |
| solo-8box-2026-08-10 | 08-10 | Clean, phase 7 | — | — |
| win-domain-2team-2026-08-13 | 08-13→14 | 4 Windows-sizing/sequencing bugs, fixed live | Clone timeout, ADDS reboot wait, missing AD vars, DSRM prompt | Fix `2f6470d` + redeploy |
| win-linux-practice | 08-14→15 | Pass; exposed strict vs non-idempotent joins | nakon v0.1.2 duplicate-step crash | `strict=False` opt-out; v0.1.3 fix |
| e2e-2026-09-03 | 09-03 | Phase 7; sshd start-limit crash loop found | Config interaction (5 `ssh-*` rows, same-second restarts) | Hotfix `ec2d746`, re-validated |
| fire-scrim-2026-09-13 | 09-13 | Clean | — | — |
| agent-scrim-2026-09-16 | 09-16 | 5 deploy attempts to reach phase 7 | TF var gap + strict + Windows row rc=1s | Per-fix + redeploy ×4 |
| agent-scrim-2026-09-17 | 09-17 | Deploy clean; event ran; scoreboard unreachable all event | Undiagnosed then (later: Quotient session semantics) | — |
| agent-scrim-2026-09-17b | 09-17→18 | 4 mid-phase-6 resumes | Join race, APIPA, OOM, vulndb DHCP drift | Resume ×4 + guest-agent repairs |
| agent-scrim-2026-09-17c | 09-18 | ~17.5 h wall (normal: 3–5 h) | Node crash / local-lvm saturation (596), join race ×2, strict ×2 | Disk moves, console unlock, `--from-phase 2` |
| e2e-2026-09-19 | 09-19→20 | Full pass + verify PASS | Timeout sizing (apt 180s, compose 60s ×2) | `--from-phase 4` ×3; budgets raised |
| scrim-dress-2026-09-20 | 09-20→21 | Max-vuln rehearsal; repeated strict aborts | strict × 16 broken rows + 1 real regression (phase-6 `TypeError`) | Trim-then-resume; regression fixed `9015f06` |
| winad-testrun-2026-09-25 | 09-25 | Windows-heavy AD comp, live | ADDS/join timing | Resume phase 6 |
| scrim-live-2026-09-26 | 09-26 | Subagent-blue vs OpenRouter-red, both 8/8 | bundle var-lint false positives blocked deploy | Lint fix (`nakon_ops.py`) + regression test |
| pfsense-ad-2026-09-27 | 09-27→28 | 4-team AD + in-path pfSense, all green | apt-lock race; Fedora member gaps; pfSense in-path (host-ZFS dead end) | Fixes above; pfSense inserted guest-side (§8) |
| pfsense-rvb-2026-09-28 | 09-28 | 2-team AD + pfSense + bad-auto red, 60 min | (see this run's report) | — |

Ranked recurring modes (most frequent first): strict × pin density → cloud-init clone race →
phase-4 timeout sizing (now fixed) → domain-join/ADDS race → vulndb row bugs surfacing only
under max-vuln pins → node/storage saturation → vulndb VM IP drift → event-side harness
failures. Note the pattern in the biggest time sinks (17c's 17.5 h, scrim-dress's repeated
aborts): **environmental saturation and unvetted pin density, not pipeline code.**

## 3. What you can rely on (recovery toolbox)

- **State file + resume.** `competitions/<id>/.deploy_state.json` records `last_phase`, teams,
  and every secret. `--from-phase N` reloads it, skips confirmation, and reuses the original
  credentials (a missing secret is regenerated once and written back). Resuming without the
  state file is a hard error by design — fresh secrets while skipping destructive phases
  desyncs the range (see `docs/architecture.md`, Operational invariants).
- **Resume markers.** `.postclone-swept` (written only after a clean full repair sweep; a fresh
  deploy unlinks it, and apply #2 unlinks it again after re-creating the team boxes),
  `.nakon-domain-<team>-adds.json` + live join probes (skips DC re-promotion on resume).
  `cloned_vms.json` is a **legacy** pre-golden marker only — v2 ranges have every team in Terraform
  state and re-creation is gated by the marker/template logic, not by that file.
  Phase 7 sub-steps are flag-gated (`seeded` / `engine_unpaused` / `injects_created`) — but
  `unpause_engine` is not idempotent, so never resume "to be safe" past a completed phase 7.
- **Snapshots.** `tz-base` is taken once for **all** boxes at the end of the phase-4 block (goldens
  take theirs pre-plant inside the golden build); `tz-ready` on all boxes at the end of phase 6.
  Disk-only, replace-existing, **never raises** (a failed snapshot is only a WARNING — verify with
  `list_snapshots` if you plan to rely on one). `redeploy-competition.py` modes: `rollback-ready` /
  `rollback-base` / `reconfigure` / `rebuild` / `resync` / `engine-recovery` (+ `--reset-event`),
  with a fail-fast snapshot precheck. Snapshots need a snapshot-capable datastore — the current
  `hdd` zfs pool is fine; thick LVM cannot snapshot at all.
- **Verify gate.** `verify-competition.py` (logins, services, isolation, misconfig survival,
  injects, no-default-creds, plus pins/plant-coverage/domains/red-identity/packet gates when
  applicable — see [usage-agents.md](usage-agents.md#verify-competitionpy)) is the pass/fail gate
  for any deploy claim. The offline suite (`python3 -m pytest tests/`) covers deploy-path helpers
  but is **not** a pipeline test — this gate against a deployed range *is* the integration test.
  See [tests.md](tests.md).
- **Logs — nothing captures them by default.** `run-agent-scrim.py --run-dir` writes
  `<run_dir>/deploy.log`; `run-deploy.sh` tees to repo-root `deploy.log`; a bare
  `create-competition.py` run leaves only the console scrollback. Two post-mortems (17b/17c)
  were possible only because a run-dir happened to capture output. Always launch with capture
  (§7).

## 4. Per-phase failure cost map

The cost asymmetry is the whole game: phases 1–2 are the expensive, dice-rolling part
(Proxmox/Terraform/clone); phases 3–7 are mostly idempotent re-entries. A failure late in the
pipeline never justifies starting over. Numbering is v2 (see the
[v1 → v2 table](architecture.md#v1-to-v2-migration-what-moved) for the older scheme).

| Phase fails | Typical cause | Cheapest recovery | Never do this |
|---|---|---|---|
| 1 | (it *is* the teardown) | re-run | — |
| 2 | Terraform abort; or "already exists" state mismatch; engine-template build/config PUT | If state mismatch: the printed hint is correct — `--from-phase 1` is the only clean path. If TF died cleanly: fix cause, re-run `--from-phase 2` (17c did this mid-life after disk moves) | Don't hand-create/destroy TF-managed resources around TF — that's what creates the mismatch |
| 3 | apt/compose timeout on the engine (engine prep from template) | `--from-phase 3` — the prepare-engine steps are idempotent; resumed this way 4 times across runs | Don't rebuild boxes for an engine-side timeout |
| 4 | golden plant strict abort on a bad vulndb row; golden build/convert; apply #2 clone race | Trim broken rows (§5) → `--from-phase 4`. **This is the only strict pass**, so it is the one that aborts on pin density | Don't `--from-phase 1` — you'd re-roll every golden and environmental dice to avoid one bad vulndb row. Note a partially *converted* golden set is fatal (templates can't be un-templated) |
| 5 | repair-stage sweep (lenient, so usually a tally not an abort); DNS/auth retries; fix_services | Trim (§5) → `--from-phase 5`. `.postclone-swept` gates the sweep; it is unlinked by apply #2 so a phase-4 re-entry always re-sweeps. If it was a transient (scp reset, one flaky step): re-run once first | Don't assume a phase-5 resume re-clones — cloning is phase 4 |
| 6 | domains (ADDS/join race, realmd timeouts); final-stage pass; beacons; `tz-ready` | Trim (§5) → `--from-phase 6`: join races are almost always just resume-and-retry, and the ADDS artifact guards re-promotion. Don't trust it on a domain comp without checking the `.nakon-domain-*-adds.json` artifacts exist | Don't expect a phase-6 resume to re-clone |
| 7 | seed/inject/unpause failure | Re-run `--from-phase 7` — sub-steps are individually flag-gated | Don't re-run past an already-unpaused engine |

Rule of thumb: **the resume you already have is cheaper than the redeploy you're considering.**
Every full teardown also re-rolls the environmental failure classes (clone races, apt
rotation, datastore pressure) that caused maybe half of all historical failures.

## 5. Trim-then-resume for strict failures

The dress run worked example. `generate_nakon_config` rebuilds `nakon-config.json` from
`box_vulns.json` on *every* invocation — including resumes — so trimming pins and resuming is
safe and takes effect immediately:

1. Read the `FAILED` step names from the deploy log; note which box(es) each hit.
2. Classify: transient (scp reset, one flaky apt) → re-run once before trimming. Deterministic
   (same row fails on multiple boxes, rc=1/2/127, missing payload) → row bug, trim it.
3. Remove the broken row names from `competitions/<id>/box_vulns.json` (a 20-line throwaway
   script in the run dir is fine — the dress run trimmed 16 rows / 27 entries this way). Keep
   every trim recorded in your findings/notes; each is a vulndb bug or pin mismatch.
4. Re-run with the phase where nakon ran: `--from-phase 4` (the strict golden plant), `--from-phase 5`
   (repair-stage sweep on every team box) or `--from-phase 6` (final-stage pass). In v2 the tail
   passes are lenient, so a broken row usually shows up as a tally rather than an abort — but it will
   fail again on every box, so trim it before resuming anyway.
5. **Trim before the first post-change resume** — every team runs the same rows on equivalent boxes,
   so the same failure will repeat otherwise.

Corollary: under `--strict`, pin density is risk. A full-catalog pin run (534 pins/team)
should be expected to iterate; don't author it the night the range needs to be green.

## 6. Upgrades that would make failures cheaper

1. **Per-phase snapshot checkpoints.** `checkpoint(n)` in `deploy.py` is the single choke
   point called exactly once per completed phase — the natural hook. Spec: after saving state,
   take `tz-phase<N>` snapshots (disk-only, replace-existing, non-fatal WARNING — same
   semantics as `range_ops.take_snapshot`) on the boxes that exist at that point (the engine from
   phase 2, every team box from phase 4). Payoff: a phase-5/6 failure could roll boxes back to
   a known-good state instead of re-running the sweep into a possibly-dirty one. Rollback path:
   extend the redeploy `rollback-*` modes with a snapshot-name override (they're currently
   pinned to the `tz-base`/`tz-ready` constants). Caveats: zfs snapshots are cheap on `hdd`,
   but rollback requires stop/start, snapshots are per-VM (no atomic group), and the scoring
   engine is never snapshotted — engine recovery is rebuild-only today
   (`redeploy --mode engine-recovery`).
2. **Always-on deploy logging.** Default the deploy to tee stdout/stderr to
   `<comp_dir>/deploy-<timestamp>.log` (the comp dir already holds 0600 secret files, so a log
   there leaks nothing new). Until
   then, §7's launch commands include the capture explicitly. This is the cheapest fix on this
   list and retroactively enables every post-mortem.
3. **Preflight pin lint** — **implemented 2026-09-23** (`config_ops.preflight_gates`, run
   automatically before terraform apply on every fresh deploy): `nakon catalog check`
   (boxes/services/vulns) is now a blocking gate, alongside template resolution (every
   `boxes.json` template must resolve to a tagged template cluster-wide, engine template vmid
   included) and a datastore-headroom check (free ≥ teams × Σ disk, unset disks counted at
   40 GB). The remaining spec piece: linting against a maintained known-broken-rows list so
   the same vulndb row doesn't abort two runs in a row.
4. **Datastore headroom check** — **implemented 2026-09-23**, part of the same
   `preflight_gates` (see item 3). The worst incident on record (17c node crash) was local-lvm
   saturation from clones landing on the template's pool; the gate fails the deploy before
   phase 1 teardown when headroom is short. Still manual: confirming templates/disks live on
   the intended pool.

## 7. Step-by-step: an e2e test run

**Preflight** (each item preempts a known failure class; total cost: minutes):

- [ ] Working tree has no deploy-path changes you can't attribute; note the current commit so
      the next triage has a `<last-green>` baseline.
- [ ] `boxes.json` templates exist on the node and none are in the known-broken list — since
      2026-09-23 the deploy itself gates template resolution before touching anything (§6.3);
      the known-broken check (`debian13-lite`, `ubuntu24.04`) is still warn-only.
- [ ] `nakon catalog check` passes — automatic on fresh deploys since 2026-09-23 (§6.3); for
      repair resumes (which skip the gate so trim-then-resume stays workable), run it by hand.
- [ ] Target datastore has headroom for all clones — automatic on fresh deploys since
      2026-09-23 (§6.4); esp. Windows, ~60 GB each.
- [ ] Nothing else is cloning on the node (workshop portal provisioner included).
- [ ] `create-competition.py --competition <id> --plan-only` and actually read the plan.
- [ ] Choose capture: harness run dir, or explicit tee (below).

**Launch** (from the repo root — state/resume paths resolve from cwd):

```bash
# full harness (deploy + verify + agents + evidence + teardown):
.venv/bin/python run-agent-scrim.py --competition <id> --new --teams 2 ...

# deploy-only test with a persistent log:
python3 -u create-competition.py --competition <id> --teams 2 --yes 2>&1 | tee deploy.log
```

**On failure** — work the decision tree, don't improvise:

1. Capture the tail (you did launch with capture, right?) and the phase that aborted.
2. Triage against §1. Regression candidate? Baseline diff before anything else.
3. strict exit 1 → §5 trim-then-resume. Timeout → check if it's the known fixed class; if it's
   a new budget, raise it and record it. Environmental (596, apt 404, clone race) → wait/repair,
   then resume at the same phase.
4. Resume `--from-phase N` where N is the phase that failed (§4 cost map). Only accept a
   `--from-phase 1` when Terraform state has actually diverged.
5. **Bound the repair loop: max 2 repair-resume cycles, then stop and report.** The
   cyberfield dress run (scrim-extreme-cyberfield-2026-09-22) burned 3 repair cycles chasing
   an unsafe resume path before the honest move became obvious: fix the roots, make ONE
   bounded attempt, and take a fresh full deploy if it fails. A third consecutive resume is
   not a repair, it's a resume-loop.

**After:**

- Run `verify-competition.py <id>` — no deploy counts as green without it.
- Post-mortem: new failure class → add a row to §1's table and an incident to
  `docs/known-issues.md`; regression → record the introduced/fixed commit pair; event-side
  near-miss → check whether `docs/rehearsal-gates.md` needs a new gate.
- Trims and repairs: record them; every trimmed row is a vulndb bug someone should see.
- Teardown unless the next step needs the range live (`destroy-competition.py --yes`).

## 8. Multi-host, pfSense-fronted, and red-team runs

Everything above assumes one host and a bare AD/Linux range. Three add-ons have their own gotchas.

### 8.1 Which host (cyberfield vs cyberrange)

Two non-clustered Proxmox hosts, each with its **own** `.env`, node name, template vmids, and
datastore. Never assume a vmid across hosts.

| | endpoint | node | datastore | env file | Windows / Ubuntu / Fedora tmpl |
|---|---|---|---|---|---|
| cyberfield | `10.0.0.193` | `pve` | `hdrives-zfs` | `.env` | 1008 / 1007 / 1015 |
| cyberrange | `10.0.0.150` | `proxmox` | `hdd` | `.env.realm-backup-20260923` (secrets — never commit) | 953 / 955 / 1016 |

- Load the env safely — the file has unquoted JSON/pubkey values that break naive `source`; parse
  it (`shlex.quote` per `TF_VAR_*` line) into a sourced file, and **`unset TF_VAR_teams
  TF_VAR_boxes_per_team TF_VAR_scoring_vm_id`** so the stale range values don't override what the
  deploy generates. Set `TF_VAR_template_vm_id` to a real engine-base on that host (cyberrange has
  no dedicated one — use the ubuntu base, e.g. 955), and pick a `--scoring-vmid` that is **not**
  1000 (the cyberrange's live `quotient-engine`).
- The cyberrange is a busy production host (~70 VMs, a live comp, infra). Pick free vmid blocks
  (`200 + id*10 + box_index`) and identifiers whose blocks are clear; the preflight collision gate
  catches overlaps but choose deliberately.
- Cross-host template move: `vzdump` → transfer → `qmrestore` (keep the original vzdump filename
  pattern or `qmrestore` says "couldn't determine archive info").
- A host reboot that must come back: boot is ext4/LVM (a suspended ZFS *data* pool can't block it);
  confirm the bad pool isn't in `zdb -C`; `onboot=1` VMs auto-start but **snapshot the full running
  set first** (`/root/running-before-reboot.txt`) and restart the rest, throttled, afterward.

### 8.2 In-path pfSense (full runbook: `docs/pfsense-inpath-2026-09-28.md`)

Per team: `engine → transit<id> (172.31.<id>.1/30) → pfSense WAN .2 / LAN 192.168.<id>.1 → boxes`,
pure router, outbound NAT off. The two traps, both fixed/known:

- **Never inject config host-side** (import the pfSense ZFS pool on the Proxmox host) — OpenZFS
  2.4.4 makes the pool unmountable by pfSense's loader (`mountroot error 22`). Inject **guest-side**:
  boot the clone, drop to the console shell (option 8), `ifconfig vtnet1` a temp IP on the
  *unassigned* LAN NIC (pfSense won't revert it), `fetch` the per-team `config.xml` from an HTTP
  server on the engine's team `.1`, reboot. Keep the pool name `pfSense` (the loader hardcodes it).
- **WAN pass rule** in `gen_pfsense_config.py` must be `<network>lan</network>`, not a raw CIDR, or
  pfSense silently drops it and scoring times out.
- Engine cutover, persistently: one engine reboot with all transit NICs added and the netplan
  MAC-matching each (`set-name: transit<id>`, `172.31.<id>.1/30`, route via `.2`) and **no** `.1`
  on the team NICs. NIC hotplug past the first NIC is unreliable — reboot, don't hotplug.
- Firewalls need `onboot=1`. The Windows-DC guest-agent may be down after a reboot — that only
  breaks `verify-competition`'s agent probe, not scoring (WinRM is a network check).

### 8.3 bad-auto red teamer against a pfSense range

`../bad-auto`, OpenRouter LLM, `python3 -m badauto {deploy,run,status,destroy}`, `config.yaml`
(`competition_dir`, `event.duration_min`, `deploy.red_*`). Point it at the comp dir and set the
duration before `deploy`.

- The engine-side firewall (`badauto/deploy/engine_nat.py`) has two modes (`deploy.red_mode`):
  **routed** (default) keeps red's source address from a dedicated segment (`red_subnet`,
  default `10.200.0.0/24`; the engine holds the segment gateway on its mgmt iface) — blue can
  see, hunt, and firewall red while scoring keeps sourcing from the team gateway. **masq**
  (legacy) MASQUERADEs `red_ip → 192.168.0.0/16`, sourcing attacks from the team gateway IP,
  which is unblockable-by-IP by design (blocking it cuts blue's own gateway + scoring).
- **pfSense caveat (both modes):** neither mode is validated against the pfSense in-path
  topology. Routed mode needs a red-segment → transit route on pfSense (engine transit
  `172.31.<id>.1` sources attack traffic as before — the WAN pass rule allows it — but return
  traffic to `10.200.0.0/24` needs a route back via the engine). The **raw-socket beacon C2
  does not** survive pfSense in either mode: blue boxes beacon to their gateway `.1`, which is
  now pfSense (not the engine that DNATs the beacon port to red01). Deploy without the beacon,
  or add a matching pfSense LAN→red01 port-forward. Core attacks (recon/spray/exploit) don't
  need it.
