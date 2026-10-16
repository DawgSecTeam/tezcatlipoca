# Defect remediation, 2026-10-16

A full pass over `docs/known-issues.md` and the two campaigns that produced it: every entry
either fixed with a regression test, or re-argued with the evidence that it is not what it
looked like. Live estate values live in `infrastructure-status.md` (gitignored); this document
names the file, never the values.

Companion evidence: the three-range campaign that verified the fixes is recorded in
`logs/round3/` and summarised below.

**Read this first, if you read nothing else:** *§1* is the scrim's interaction reality — the one
number the harness exists to move — and *§2* is the defect table with `file:line` and the test
that proves each fix. *§3* records the two design documents this pass found to be wrong, and
*§4* is what is still open and why.

---

## 1. Verification campaign: what the runs actually showed

Three real competitions deployed in parallel on `main` @ `9e3ad19`, with one defect-driven re-run on
`55b8e54`. Raw evidence, including the preflight lines and the lock tasks quoted below:
`logs/round3/campaign-log.md`. **Live estate values are in `infrastructure-status.md`; the
placements are recorded there, not here.**

| competition | run | `engine_head` | deploy | verify |
|---|---|---|---|---|
| pfsense-rvb | `run-4d790ce0` | `9e3ad19` | **succeeded, 8/8** | `run-1dc3cf75` (submitted) |
| scrim-one-light | `run-526c58ea` | `9e3ad19` | **succeeded, 8/8** | `run-d84d10a6` — FAIL |
| crimson-relay | `run-68aa9678` | `9e3ad19` | **failed, phase 4** | — (range destroyed cleanly, `run-9a12aeb9`) |
| crimson-relay (re-run) | `run-dd88db6d` | `55b8e54` | **failed, phase 4** | — |

This is the first campaign in which "which code ran" is a recorded field rather than an inference:
every run's `engine_head` was read back from the store, which is the fix for the round-2 finding
that a whole round executed an older revision than the fixes pushed during it.

### What the new preflight proved

- `management bridge vmbr0 exists on node` — the `mgmt_bridge` knob is read for the first time.
- `engine mgmt IP <x> — UNVERIFIED (2 running guest(s) have no agent: …); proceeding because
  TEZ_ENGINE_MGMT_IP is set explicitly` — the designed asymmetry, on the real estate.
- `red01 IP <x> — UNVERIFIED (…); proceeding because the Compfile sets it explicitly`.
- The **red band scanned entirely free**, so the engine addresses were placed outside it and the
  band belongs to red01 alone; the red01 vmids were derived, not reserved (the Compfiles'
  hand-derived reservations were deleted before the campaign).

### The identity fix, verified end to end

The state documents carry `comp_name: crimson-relay` / `pfsense-rvb-2026-09-28` /
`scrim-one-light` where every dashboard range used to carry `comp`; the engine templates carry
`comp-crimson-relay` instead of `comp-comp`; and the failed range's teardown reported
`estate clean for this ownership set` and retired the range on the board
(`retired_by=run-9a12aeb9`). The competition identity is genuinely not the directory name — the
pfsense range proves it, since that Compfile's `name` differs from its directory.

### The datastore lock is a whole class, not one site

crimson-relay died **twice** on PVE lock contention, in two different places:

1. **Template conversion** (`qmtemplate 2350`: `can't lock file
   '/var/lock/pve-manager/pve-storage-hdd' - got timeout`). PVE left `template: 1` with the volume
   never renamed to `base-`. The volume guard added on 2026-10-15 is what turned that into a named
   refusal at the moment of damage instead of an opaque `HTTP 500 Linked clone feature is not
   supported` two phases later — it worked exactly as designed, and it is why the root cause took
   one query to find. Fixed: the conversion now queues on the same per-node storage lock as the
   clone, and waits on the task PVE returns (its return value had been discarded on the belief that
   it returns none).
2. **A per-VM config lock** (`qmstop 2351`: `can't lock file
   '/var/lock/qemu-server/lock-2351.conf' - got timeout`) on the re-run, in
   `golden_ops.stop_vm`. Fixed in the follow-up pass below: the cause was our own abandoned task,
   not a foreign race — `wait_for_proxmox_task(timeout=120)` gave up on `qmshutdown 2351` while
   PVE's task was still running and still holding the per-VM lock, and the immediate force-stop
   then collided with it. `lifecycle.stop_vm` now hands PVE its own `timeout` (so the task ends on
   its own) and every VM-scoped task in `infra/lifecycle.py` goes through one helper,
   `infra.client.run_vm_task`, which retries a task whose failure names a lock file.

The lesson is recorded rather than generalised into a retry framework: the storage sites were
fixed by queueing before the fact, and the per-VM site turned out to be self-inflicted — a client
timeout outliving the task it gave up on — which is fixed at the cause, with the shared retry
helper as the backstop for contention we do not control.

### `misconfig`: the diagnosis was half right, and the wrong half is instructive

The staging fix shipped in this pass **is** necessary and **did** take effect — the pre-domain stage
config carries `bad-perms-userConfig` for both teams and the golden stage no longer does. It is not
sufficient. The deploy's own evidence:

```
[web01-team150] step 000/4 config: bad-perms-userConfig
[web01-team150]   └─ bad-perms-userConfig: ok (0s)      # planted post-boot, rc 0
```

and the verify minutes later still reads the mode as absent on both boxes.

So the file is rewritten **after** the plant, within the same phase: `fix_services` creates the
credlist accounts with a `useradd` each, and shadow-utils' commonio recreates `/etc/shadow` (and
`/etc/passwd`, `/etc/group`) at its default mode. The `chpasswd` that follows preserves whatever
mode is already there (probed 2026-10-16: `chmod 666` then `useradd` reads `664`; `chpasswd` alone
leaves `666`), so the resetting writer is the account creation, not the password write. The
correct rule is stronger than "plant it post-clone": **account-file modes must plant after every
account writer**. A parallel session reached that independently and landed it as `7db5750` ("golden
plant: account-file modes plant last, after every account writer") — *after* the commit this
campaign's scrim-one-light ran, so the campaign could not have seen it pass.
**The next step for this gate is a re-verify on `≥ 7db5750`, not another design change.**

The two gates that failed alongside it are expected, not defects:

- `pins_registered FAIL 5 pinned checks` at deploy time is the interim reading this pass labelled
  (`--interim`); the same gate PASSes after the red/blue staging.
- `round_loop FAIL` follows directly from the deliberate `--hold-start`.

### The seed is broken, and the harness now says so

The verify's `degradations` gate caught the deploy's own ledger:

```
assume_breach_seed: "detail": "192.168.151.4 unreachable over SSH"
```

bad-auto's seed could not reach the team boxes from red01 — the same failure as the 2026-10-15
scrims, now **recorded as a degradation instead of announced as success** thanks to the exit-code
fix. The pre-T0 gate is correspondingly strict: this is the condition it refuses T0 on, so the scrim
was submitted knowing it would either seed properly or refuse with a named cause. **A
red-versus-empty-room event is never reported as a red success** — if the scrim refuses, that is the
correct and honest outcome, and the seed path (red01 → team boxes over SSH) is the defect to chase
next.

### Still outstanding when this was written

- pfsense-rvb's verify and scrim-one-light's scrim were submitted and running; their results are
  appended to `logs/round3/campaign-log.md` rather than guessed at here.
- Only the failed crimson-relay range had been destroyed, so **the estate was not yet clean** at the
  time of writing; the teardowns are the remaining campaign step. The re-run's own range
  (`run-dd88db6d`) was left live by the ordering — its destroy ran before the re-run failed — and
  is reclaimed in the follow-up pass.
- crimson-relay's third attempt needs the per-VM-lock retry above; that retry is landed and the
  attempt is the follow-up pass's live verification.

---

## 2. Defects fixed in this pass

Each row is a defect that produced a real failure in the 2026-10-15 campaigns, with the code
that was wrong and the test that now holds it shut. The `known-issues.md` entry for each was
deleted as that file's rules require; git history is the record.

### 2.1 The deployment pipeline

| Defect (live symptom) | Root cause | Fix | Proving test |
|---|---|---|---|
| A failed PVE task was polled to its full 1800 s timeout, three parallel deploys including | `wait_for_proxmox_task` treated any non-`running` state as terminal, so a *dead* task with an empty `exitstatus` passed | only `running` is non-terminal; anything else raises with the task's own status | `tests/test_proxmox_tasks.py` |
| A transient datastore lock failed a whole deploy (first fix retried 3× at 20 s and still lost) | PVE's per-VM/iper-datastore lock 500 was not retried, and the backoff landed inside the contention window (failures at +0:03, +0:36, +1:11 while the winner committed seconds in) | 5 attempts at 40/80/120/160 s, asserting the **total** wait, not the attempt count | `tests/test_clone_retry.py` |
| Three parallel deploys still *raced* for that lock, and the loser spent four of its five attempts and ten minutes while the other two built (round 3, live) | retrying harder is not a queue: PVE holds one datastore lock per clone and refuses everyone else, so concurrent ranges contended instead of serialising | our own clones take a bounded flock keyed on (endpoint host, base image) before cloning, so they queue; the retry loop stays as the fallback for contention we do not control, and the wait is bounded so a wedged holder cannot wedge every later deploy | `tests/test_proxmox_task_failure.py::BaseCloneLock` |
| `[4b/8]` died with `HTTP 500 Linked clone feature is not supported for 'hdd:vm-2350-disk-0'` | the unbooted (DC) golden conversion never awaited its `base-` volume rename — the booted path had guarded this since 2026-10-04, the DC path never did. The earlier failure's own `qmtemplate` task had hit a storage-lock timeout and discarded its UPID, so the driver printed success | one shared `_await_base_volumes` guard for both conversion paths | `tests/test_golden_dc_contract.py` |
| Every deploy's log and the dashboard stage jumped `[6/8]` → `[8/8]` | the `[7/8]` banner was printed only on the resume/skip branch (`deploy_lib/phases/final.py`) | banner printed on the normal path in the same position as the other phases | `tests/test_phase_tail_offline.py::Tail::test_phase7_normal_path_prints_the_banner` |
| `.deploy-timings.jsonl` reported per-phase totals up to 1.8× the run's wall clock | `domain_adds`/`domain_join` were filed under phase 6 while their parent `domains` is phase 7, and the summaries counted parent **and** concurrent children (round 2: 4994.7 s of children under a 3062.1 s parent) | children carry phase 7; the shared reader infers each row's span from `ts`+`seconds` and counts only top-level rows, which also reads pre-fix files correctly | `tests/test_timing_sidecar.py` (4) |
| The entry banner printed `endpoint=? node=pve datastore=local-lvm tpl=? engine-mgmt=default` mid-deploy | it printed before the `nodes.json` placement resolved, so a first-time operator read the wrong host/datastore/template | the line is gone; only the resolved `Effective target:` remains | `tests/test_multinode.py::DeployBannerTest` |
| Two teardowns printed `[estate] ok — destroyed 0` and then exited non-zero on `config 'proxmox.endpoint' is not set` | the config contract moved to `TEZ_*` and nothing enforced it: a deploy survives a missing endpoint (nodes.json supplies it) so the gap stayed invisible until teardown needed the ambient config | every driver refuses at startup, naming the key and the profile file, when neither `proxmox.endpoint` nor `proxmox.api_token` resolves (`--plan-only` exempt) | `tests/test_startup_config_refusal.py` (6) |
| The deploy-time verify read as a scoring verdict while contradicting the pre-T0 verify 3 minutes later (`pins_registered FAIL` → `PASS`) | the deploy-time result is an *interim* reading (it runs before the staging it depends on) and nothing said so | `--interim` labels the run log, the test manifest and the report's verify line; the pre-T0 verify keeps the final record | `tests/test_interim_verify_label.py` (5) |
| A test asserted `"python3" not in sys.executable` | it asserted a property of the host's interpreter rather than the spawn contract | asserts the deploy child's `argv[0] == local_python()`, with a case that patches `sys.executable` to a distro python | `tests/test_local_python_spawns.py` |
| The "offline" test suite made real HTTPS requests to the production estate (13, and the affected modules ran 100× slower) | a driver script calls `load_dotenv()` at module level, so importing one for a CLI test wrote the real estate identity into `os.environ` during collection — before any fixture ran | `tests/conftest.py` snapshots the ambient `TEZ_*` set at import time and restores it around each test; the endpoint literals those fixtures had picked up are replaced with a non-routable placeholder, so an incomplete mock fails loudly instead of reaching production | the suite reports zero live calls |

### 2.2 Identity and placement (the `+170` mess)

The three defects here share one cause: every vmid and management address was *verified* against
a collision rather than *selected* from what is free, and three of the seven values an operator
had to supply ended in a per-competition file or a constant.

| Defect | Root cause | Fix | Proving test |
|---|---|---|---|
| Every dashboard-deployed range carried the tag `comp-comp`; `verify_clean` blamed the whole estate on it, and a scrim-one-light deploy adopted **another competition's kept golden as its own** and reclaimed it (`run-a87e0d88`) | the ownership tag, clone marker, console token and portal registration all derived the competition from the workspace **directory** name, and the dashboard's comp dir is literally `<run_dir>/comp` | one resolver, `common.utils.competition_name`, feeds every tag writer and reader (deploy, teardown, orphan reclaim, red01 reclaim, console, portal). Landed as the pre-release identity break the issue called for, deliberately, while the estate holds none of our VMs | `tests/test_preflight_unified.py::CompetitionIdentity`, `tests/test_teardown_node_names.py::OwnershipIdentity` |
| `red01`'s derived vmid landed exactly on a golden slot for any placement using three or more nodes at the tool's own default box count, and the operator's only feedback was a point-in-time refusal that repeated the false claim that `+170` was collision-free | red01's vmid was `engine + 170`, an offset that overlaps the golden band `engine + 150 + slot*10 + box_idx` from slot 2 onward | derived one golden stride past this placement's last band (`engine + 150 + (slots+1)*MAX_BOXES_PER_TEAM`), which is disjoint for every permitted slot count and stays so if the bounds grow. `RED01_VMID_OFFSET` is deleted | `tests/test_preflight_unified.py::SlotBandsAreDisjoint`, `::PlacementSelfProof` |
| Two concurrent runs of one competition could not be given distinct red01 slots without editing shared state (the 2026-10-15 campaign hand-derived a vmid/IP per competition and committed them into two Compfiles) | red01's vmid **and** address resolved from the *competition* directory while everything else about placement is per *run*, and `create-competition.py` had no `--red-vmid`/`--red-ip` | red01 is carried in the per-run placement object: the vmid derived as above, the address SELECTED from a declared red band at deploy time (excluding addresses the placement claims and the legacy `.196`–`.199`), with the resolved value written back into the durable state document. Compfile `red01_vmid`/`red01_ip` survive as the override path — removing them would strand a live red01 | `tests/test_preflight_unified.py::RedAddressSelection`, `tests/test_multinode.py::RedAddressWriteBack` |
| `proxmox.mgmt_bridge` was declared and never read, so only a `vmbr0` estate could host a range — and the failure was a VM attached to a bridge the node may not have, not a refusal | the knob existed in the profile spec and three sites hardcoded `vmbr0` | resolved once (per-node record over profile, defaulting to `vmbr0`), threaded to the engine, the engine template and the jump VM, with a preflight that refuses a bridge the node does not own | `tests/test_estate_apply.py::Engine`, `tests/test_preflight_unified.py::MgmtBridge` |
| The placement object did not exist, so preflight validated arithmetic that was only written down two phases later | `placement` was `None` for single-node ranges and the engine-template/golden identities were recorded in phases 2 and 4, after they had been used | `PLACEMENT_VERSION = 2`; the object carries `engine_template_vmid`, `golden_vmids`, `engine_mgmt_ip`, `mgmt_bridge` and `red01{vmid,ip,node,explicit_ip}`, written in `prepare()` before preflight, with a v1 read-compat shim that resolves red01 the old way and writes it back | `tests/test_multinode.py::PlacementRecordTest` |
| `verify_clean` answered "is red01 gone?" with bare vmid membership across the whole cluster | every vmid the endpoint knew was tested, with no tag, competition or node filter, so an occupant could not be attributed | the occupant is attributed by bad-auto's own marker; an unmarked or unknown occupant still fails closed | `tests/test_teardown_model.py::VerifyClean`, `tests/test_scrim_teardown_red_proof.py::Red01Attribution` |
| The placement record never survived `prepare()`, so every reader re-derived what the redesign recorded once — including red01's **selected** address, which exists precisely so it is never re-derived | the planner writes `placement` straight to the document while the deploy context holds its own copy from `load_prior_deploy_state`; the next `save_state()` wrote that stale copy back. Every state document on the estate through 2026-10-16 read `placement: None` while the run's own log said "Placement recorded in <that file>" (found by inspecting the three live round-3 runs, not by a test) | the save takes the on-disk placement, never the stale in-memory one | `tests/test_estate_state.py::TheDocumentIsTheOnlyWriter::test_a_placement_write_survives_the_deploy_state_save` |
| An orphan reclaim left the range's bridges behind — and a bridge carries no tags, so no destroy path could ever attribute one | bridge comments described the bridge's *purpose* (`"Quotient team 120 — isolated, no uplink"`) rather than its owner, and `teardown/orphan_ops.py` documented "no bridges" as a limitation | the comment carries comp + run ownership; the normal teardown and the orphan reclaim match that comment and remove only this run's, refusing an uncommented or foreign bridge | `tests/test_orphan_reclaim.py::Reclaim`, `tests/test_estate_destroy.py::BridgeAttribution` |

### 2.3 The scrim and event harness

| Defect | Root cause | Fix | Proving test |
|---|---|---|---|
| A 30-minute event was still in `stage=event` at 56 minutes (`run-59a685c0`) | `monitor_loop` stopped at T+30 while the harness waited on a blue cycle that had already blown its 1800 s budget — a 6-minute documented cycle contract against a 20–30 minute measured one, so the event was unbounded | `duration_min` is authoritative: no cycle starts past it, an in-flight cycle is **aborted** at it (capping the join alone would leave a non-daemon thread holding teardown), and capture/teardown always run | `tests/test_scrim_event_deadline.py` (3) |
| The harness announced `assume-breach presence seeded — every box is compromised before T0` for a range where the seed had failed on **every** box (four occurrences, and the direct cause of two scrims with zero interaction) | `seed_assume_breach` gated on bad-auto's exit code alone, and that exit code was 0 with every step failed | bad-auto reports a per-step outcome and exits non-zero when no step took; the harness gates on that report and refuses T0 unless **every box it looked at** was compromised, naming the boxes. An uninterpretable report also refuses | `tests/test_scrim_seed_guard.py`, bad-auto `tests/test_seed.py` |
| Two scrims of one range ran concurrently (each stops nginx for its own fire test, both log in as the same accounts) | nothing reserved the range; the only lock in play was keyed on the engine vmid | a flock keyed on the resolved range workspace, acquired before staging, refusing with the holder named | `tests/test_scrim_range_lock.py` |
| `BLUE-TEAM.md` was a 524-byte stub and blue's narrative had to be reconstructed by hand | blue never wrote its narrative when its cycles timed out | already wired on `main` (`finalize_blue_narratives`); this pass added the missing end-to-end guard so a timeout-only feed still reaches the canonical document | `tests/test_artifacts_ops.py::TestWarningsAndStubs` |
| red01 went unreachable at T+13 and nothing could distinguish "red is dead" from "the path to red is dead" | both were the same probe, and the old `if code` guard skipped exactly the ssh-never-connected shape | red01's hypervisor power state is recorded on **every** failed probe and named in the alert | `tests/test_red_liveness_and_cotenancy.py` |
| `remote_access : FAIL SNAT lines missing` on a range whose identical probe passed minutes before and after | tailscaled's rebind (`rebind-reason=[ips-changed]`) re-applies its ruleset and takes the POSTROUTING chain with it; the 30 s timer left a real window of broken tailnet→team traffic | a unit watching the interface event re-asserts immediately; the timer stays as the fallback | `tests/test_remote_access.py::RebindingReassertTest` |
| "NOT READY" was partly an artefact of the clock: the blue gates demanded ≥6 cycles and 0 timeouts against a 20–30 minute measured cycle | the gate numbers were written before the harness had been rehearsed with them | the blue cadence gates are recalibrated with each number's measured basis recorded beside it, and the doc records that per-team LLM endpoints were considered and declined. The restoration gate was deliberately **not** lowered: every measured zero is red's seed failure, not blue's cadence | `tests/test_rehearsal_gates.py` |

### 2.4 The deploy-time seed and the red identity (cross-repo)

| Defect | Root cause | Fix | Proving test |
|---|---|---|---|
| A dashboard range's red01 was marked `comp=comp` while the deploy's own tags said `comp-<real name>`, so the red01 gate refused to treat its own live red01 as its own on a resume | bad-auto derives its identity marker from `Path(competition_dir).name`, and that directory must stay the workspace (bad-auto reads `credentials.txt` and `teams.json` from it) | a `deploy.comp_name` key, written at the single config chokepoint in `red/badauto_config_ops.py`, read by bad-auto for both the marker it writes and the cross-check on destroy, with the directory name as the fallback for older configs | `tests/test_badauto_isolation.py` (2), bad-auto `tests/test_red_identity.py` (5) |
| bad-auto's seed exits 0 with every box unreachable; `cmd_run` returned 0 on a guardrail halt; the director keyed its own "seeded" verdict on `len(report["access"])` | the same shape three times: work that achieved nothing reported success to its caller | a per-step report with an `ok` verdict, non-zero exits, and the director reading the seed's own verdict | bad-auto `tests/test_seed.py`, `tests/test_guards.py` |
| A re-seed vanished under the director's next save | `World.save()` was last-writer-wins | it re-reads under the flock and merges the other writer's additions before writing | bad-auto `tests/test_state.py` |
| A cloned, unstamped red01 holding the LLM key was left behind when the SSH key did not resolve | the clone happened before `<ssh_key>.pub` was read | the public half is validated first | bad-auto `tests/test_red_identity.py` |
| bad-auto demanded `TF_VAR_proxmox_*` over names the pipeline no longer sets | the config contract moved to `TEZ_*` and bad-auto's read did not | `TEZ_PROXMOX_*` first with the legacy names accepted and normalized as a fallback | bad-auto `tests/test_config_contract.py` |
| The scoreboard's inject page renders markdown literally and prints its title twice — and every engine bootstrap re-cloned stock upstream, re-introducing it | an upstream `dbaseqp/Quotient` bug, patched by hand on one live engine | applied idempotently during engine bootstrap, verified by the step, and checked byte-for-byte against the canonical Quotient commit; a missing file or anchor fails the bootstrap by name | `tests/test_engine_steps.py::QuotientInjectPatch` (6) |

### 2.5 `misconfig`: the one deploy gate that means the scored range is not what the packet promises

`misconfig` failed in **every** verify — deploy-time and pre-T0 — with
`'bad-perms-userConfig' absent on every team` while `plant_coverage` PASSed all machines and the
golden build recorded the step as planted (`rc 0`). Two gates contradicted each other and four
runs showed the same shape.

Comparing the probe to the catalog step body excluded the check-side explanation (the probe is
`stat -c %a /etc/shadow == 666`; the step is `chmod 666 /etc/passwd /etc/shadow /etc/crontab` —
they agree). The mechanism is that **shadow-utils recreates the file**: `useradd`/`groupadd`/
`usermod` (commonio) rewrite `/etc/passwd`, `/etc/shadow` and `/etc/group` at their default
modes, discarding a planted *mode*; `chpasswd` alone preserves it. Probed 2026-10-16 in both
Linux golden bases (Debian 13 and Ubuntu 24.04, shadow-utils 4.17.4): `chmod 666` then `useradd`
reads `664`, reproducibly across runs; `chpasswd` alone leaves `666`.

That localized the defect but the first correction staged it wrong. It is not the clone's boot and
not the pre-domain pass that matters — **two account writers run after the passes a mode pin would
use**:

- `guests.service_fixup_ops.fix_services_on_boxes`, whose script always begins with a credlist
  `useradd` + `chpasswd` for every team box — the end of phase 6, i.e. **after** the pre-domain
  plant;
- bad-auto's assume-breach seed, whose depth-3 persistence `user` method runs `useradd` on every
  rooted box in phase 7 — **after** the final plant.

So the pins plant in the deploy's **last** pass (phase 7, after the seed and before the trail
wipe): `constants.ACCOUNT_FILE_MODE_CONFIGS` — `bad-perms-userConfig`, `passwd-world-readable`,
`writable-etc-group`, plus any pin whose own vars name one of those files (the parameterized
`loosen-file-mode FILE_PATH=/etc/shadow` shape) — carried by a `.nakon-late.json` config, with a
structural guard that fails against any earlier staging.
`tests/test_nakon_stages.py::AccountFileModeStaging`.

Three facts that localized it, none of which explain the *timing* on their own:

- the plant applied on the **golden** (`web01-golden`, `rc 0`), so "never planted" is excluded;
- the sibling probe `writable-sudoers` was present on every team — a **directory** mode neither
  account writer touches;
- the failing pin and the probe both name `/etc/shadow`, the file the writers recreate.

Two things this diagnosis **ruled out**, recorded so they are not reconnected without new
evidence:

- it is not the "PAM account configuration" SSH-lockout entry. `pam_unix`'s auth and account
  paths call `getspnam` and `check_shadow_expiry` and contain no `stat`/mode check on
  `/etc/shadow` (verified against Linux-PAM master; `pam_unix_passwd.c` carries none either), so a
  world-readable shadow does not fail `pam_acct_mgmt`. The misconfig does not lock teams out.
- it is not a phase-4 clone/rollback dropping the plant: the pattern that died is exactly the one
  the account writers recreate, and its sibling that they do not touch survived.

### 2.6 The dashboard

| Defect | Root cause | Fix | Proving test |
|---|---|---|---|
| A run could execute **stale code** while its record said `engine_ref: main` — a whole round of runs executed an older revision than the fixes pushed during it | `_provision_worktree` discarded `git fetch`'s return code, so a failed fetch silently resolved the remote-tracking ref from cache | a failed fetch refuses the run before any worktree is cut, and the resolved commit is recorded as `engine_head` (with an `ALTER TABLE` migration for live data dirs) so the record and the UI state what ran | `tests/test_provision.py` (3), `tests/test_store.py` |
| A refused run's stub document shadowed the live range, and a range reclaimed by tag kept reading `live` | the resolver ranked candidates by run-dir recency and accepted any document with a run id; `has_vms` trusted the workspace's recorded resources, and an orphan reclaim wrote nothing to the store | candidates rank by recorded inventory, ambiguity refuses naming both, and `POST /api/ranges/retire` gives a reclaim a way to retire the range — without the board ever polling Proxmox. The orphan reclaim calls it through the run-queue handoff the service already gives every driver, and prints a copy-pasteable call when the dashboard is unreachable; a refused or unreachable dashboard is reported, never fatal, and a reclaim that could not destroy everything deliberately leaves the range `live` | `tests/test_ranges.py` (4), `tests/test_orphan_reclaim.py` (9) |
| A failed destroy could not be resumed, because the workspace holding its journal had been deleted | the dashboard deleted each run's workspace when the run ended, including `teardown_failed` runs | `workspaces.release` holds the workspace of any run whose range is not retired, reclaiming only bare debris | `tests/test_workspaces.py` (3) |

---

## 3. Where the design documents were wrong

Both were working documents, and both would have been copied again if the errors had been left in
place.

- **`logs/campaign/placement-simplification.md` §6.3's red01 vmid formula.**
  `engine + max(JUMP_VMID_OFFSET + slots_used, 4) * 10 + 10` evaluates to `engine + 1320` for one
  slot — inside the team blocks (`200 + identifier*10` starts at 1210) and directly contradicting
  the prose beneath it, which says "engine + 90 at worst, below engine + 140". The invariant, not
  the formula, is what matters; the document is corrected in place and the derivation now used is
  `engine + 150 + (slots_used+1) * MAX_BOXES_PER_TEAM`.
- **The same section's positional claim that red sits below the lowest team block is false in
  general.** Team vmids are absolute (`200 + identifier*10`) while red is engine-relative, so
  engine 1300 with identifier 101 is disjoint but *below* red. The property test proves pairwise
  disjointness for every permitted input and the positional claim only for blocks above the engine
  span; the discrepancy is documented in the test rather than tested away.
- **`collect_teams` cannot host the widened reserved-band check**: it runs before the slot count
  exists, and reserving the maximum span there would refuse valid single-node configurations. The
  check lives in `placement_record.check_team_blocks`, called from `compute_placement`, against the
  bands the placement actually reserves.

---

## 4. Still open

Not everything closed, and the reasons matter more than the list. Full entries:
`docs/known-issues.md`.

- **The retired-range kept-templates loop.** `--teams-only` retires a range and keeps its golden
  and engine templates for the next run — but the retired range then blocks the only path that can
  destroy them, so reclaiming a competition's templates still requires deploying it again. The
  dashboard resolver now skips retired ranges; the teardown side is unchanged.
- **`pam_unix` and the planted-box restart lockout.** Unreproduced since 2026-09-29, and this pass
  ruled out the misconfig as its cause. The diagnostic lab the entry asks for does not exist
  anywhere on this host — it would have to be written from scratch for a bug that has not recurred.
- **The scoring round loop still does not auto-resume after an engine reboot.** The classifier and
  `verify-competition.py --fix-round-loop` are in place; it needs a live reboot trial to be called
  validated.
- **Fedora goldens are still built on SELinux-enforcing nodes** with `SELINUX=permissive` set only
  on rebuild, so the current templates still enforce. The recipe is fixed; the rebuild is a run.
- **The student portal's unproven paths** — the Cloudflare tunnel, `pveproxy` under event-size
  concurrency, box rebuilds, multi-node, teardown revocation.
- **The template builders reach their scratch VM through node shell.** An operator-side alternative
  needs a config key naming the operator's local NIC (host ground truth, not a profile value), a
  local privilege model the repo does not have, and an L2 precondition that is unprovable from
  code — so it is a decision, not a patch.
- **The engine management IP is resolved once but not selected.** The red01 address is now selected
  from its band; the engine's is resolved from the flag, the node record, the profile, or the
  marked default, in that order. On an estate where the default is poisoned *and* every candidate
  is unverifiable (agent-less co-tenant guests), selecting one unattended would trade a wrong
  answer for a guess, so it stays an explicit per-run input.
- **The red band's residual race.** Selection is point-in-time and excludes provable takers and the
  placement's own claims, but two ranges that reach the same phase within the same window can still
  select the same address, and guests whose agent cannot answer are invisible to it (this estate
  has two). The selection prints those guests by name. Concurrent runs that care should pin
  `red01_ip`, which is what the override path is for.
- **Nothing closes the "one range per node while an event is live" residual.** The harness warns
  (`staging.warn_co_tenancy`); refusing is a policy decision.
- **Upstream, unchanged:** `IIS FTP`'s timing-fragile golden plant, `mailenable-cleartext-mail-win`,
  `nakon randomize` handing out unplantable names, the Windows package-manager fallback, and the
  Quotient inject page's upstream bug (worked around at bootstrap rather than fixed upstream).

### Standing limitations, not defects

Windows scoring is port-open only; nakon step failures are recorded and never fatal by design; and
orphaned ranges on a shared node are a reclaim job, not a code bug.
