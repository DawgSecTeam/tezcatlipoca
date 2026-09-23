# Postmortem — scrim-extreme-cyberfield-2026-09-22 (full-dress deploy + first red-vs-blue E2E on Cyberfield)

Event window: 2026-09-22 ~22:45 EDT → 2026-09-23 ~04:45 EDT (deploy 22:45–01:00, first E2E scrim 02:59–04:21).
Cluster: **Cyberfield** (`10.0.0.193`, node `pve`, datastore `hdrives-zfs`) — a separate Proxmox node from
the Cyber Realm cluster. The Realm was occupied by another operator the whole time; **zero Realm resources
were touched** (except the read-only template lookups used to plan the port).

Deliverables produced by this run:
- A live 2-team extreme competition (`scrim-extreme-cyberfield-2026-09-22`, subnets 192.168.113/114.0/24,
  engine 10.0.0.243), `tz-ready` snapshots on all 10 boxes, 12 injects seeded.
- A first end-to-end red-vs-blue agent scrim against it (bad-auto red01 + 2 qwen3.6 blues).
- A list of concrete, root-caused defects to fix before the real practice (§6).

---

## 1. Why this took a separate range and what a Cyberfield port actually requires

tezcatlipoca is cluster-agnostic *in principle* (everything comes from `.env`), but a real port has four
hidden couplings. Anyone doing this a second time should check all four first:

1. **Box templates resolve by NAME, not VMID.** `terraform/main.tf` builds `template_ids` from VMs tagged
   `template` and matches `boxes.json`'s `box.template` strings against `vm.name`. Cyberfield's copied
   templates carry different names (`base-windows-server`, `base-ubuntu24.04-fix`, `base-debian13-lite-fix`),
   so `boxes.json` must be remapped or terraform fails with "invalid template".
2. **The scoring engine resolves by VMID** (`TF_VAR_template_vm_id`) — and unlike boxes, the engine gets NO
   cloud-init from terraform. The engine template must be hand-prepared: a `sysadmin` account with the
   automation pubkey, passwordless sudo, qemu-guest-agent (main.tf reads its DHCP IP through the agent),
   and it must NOT rely on any box-template provisioning. We built `9088 scoring-engine-base` on cyberfield
   from `base-ubuntu24.04-fix` (clone → guest-exec to bake the account → boot-verify ssh+sudo → template).
3. **Team VMIDs are a pure arithmetic function of the team identifier** (`vm_id_for = 200 + id*10 + index`)
   and identifiers are HARDCODED `100+i` in `collect_teams()`. Default 101/102 → VMIDs 1210–1224, which
   **collide with cyberfield's template range 1201–1211**. This needed a code change: `collect_teams` now
   honors `TF_VAR_team_identifiers` (fallback to the old default), and this event used **113/114** →
   VMIDs 1330–1334 / 1340–1344, subnets 192.168.113/114.0/24.
4. **bad-auto (red01) has its own cluster assumptions** (`red_vmid 999`, `template ubuntu24.04-fix`,
   `red_storage hdd`, `red_ip 10.0.0.198`) — all patched in the orchestrator's config builder for
   cyberfield (vmid 999 is free there, template name remapped, storage `hdrives-zfs`, red IP `.244`).

Also verified during prep: cyberfield's 1000-range template copies were corrupt in a previous migration
attempt (qcow2-import) and were re-copied via ZFS-send; all three box templates boot-verified with the
guest agent, and tagged `template` so terraform's data source can see them (they only carried `general`).

## 2. Deploy timeline (phases, failures, repairs)

All times EDT. Log: `logs/deploy-cyberfield-2026-09-22.log`.

- **22:45 — phase 1-4 clean**: terraform created bridges vmbr113/vmbr114, engine VM 1000, all 10 team boxes
  (Windows clones take 16 min each on first boot — normal). Engine bootstrap: docker + Quotient build OK,
  NAT ensured. One cosmetic warning (bridge delete 400 during cleanup) — harmless.
- **23:20 — phase 5 (nakon plant team1) attempt 1: 22 FAILED configs, `--strict` abort.**
  Root-cause tree (pulled from per-step logs, NOT guessed):
  - `tftpd-hpa-anon-write` (web01+db01): dpkg postinst **exit 82** on Ubuntu Noble — the package wedges
    dpkg, and every later apt/dpkg step on that box fails in cascade (nginx, vsftpd, x11vnc, postgresql,
    install-package, enable-service…). The cascade *looked* like 8 broken configs but had 1 real cause per box.
  - `postgresql-remote-access` (web01+db01): pulls `postgresql-no-auth`, which is on the already-known-bad
    list from the Realm runs.
  - Windows (dc01+win01): `local-user-win` ×2, `powershell-execution-unrestricted`, `rpc-proxy-on-dc-web-win`,
    `unauth-kiosk-app-startup-win`, `mailenable-cleartext-mail-win` — all password-policy/`Set-LocalUser`
    failures on users their own config was supposed to create (svc_directory, kioskuser).
  - **Repair 1:** pruned 15 pins (459→454: tftpd-hpa-anon-write, postgresql-remote-access on web01/db01;
    the 5 Windows user-policy pins on dc01/win01) → catalog check 0 errors → rolled all 5 team1 boxes back
    to `tz-base` → resume `--from-phase 5`.
- **23:59 — attempt 2: only 2 failures** (web01 `nginx` rc=100 + its `root-nginx` cascade).
  New root cause, different from attempt 1: **apt mirror 404** — security.ubuntu.com dropped
  `nginx 1.24.0-2ubuntu7.17` between index and fetch. Pre-healed web01 by guest-agent `apt-get update`
  + `apt-get install nginx` (resolved to 7.18). Everything else planted clean.
- **00:41 — attempt 3 (MY process error, worth recording):** resumed `--from-phase 5` **without rolling back
  to tz-base first**, re-planting over already-planted boxes. Result: 11 new failures in two clean classes —
  (a) all 3 Linux boxes "Authentication failed: transport shut down or saw EOF" (a planted pin breaks
  post-plant SSH; the deploy's own DNS step fell back to the guest agent), (b) Windows idempotency
  (`Elevate Guest Account`, `never-expires-service-account-win`, `iis-webshell`) on re-run.
  This burned repair cycle 2 of 2 and is why team1 has a mixed state. **Lesson: the plant is NOT
  idempotent over a planted box — always roll back to tz-base before any phase-5 resume.**
- **01:00 — phase 6 (`--from-phase 6`)**: team2 cloned from planted team1 boxes (by design both teams get
  identical DNA), phase-6's plant pass re-planted everything: same symmetric failure classes (Linux EOF,
  3 Windows idempotency failures ×2 teams) — expected, since the boxes were already planted.
- **01:06 — phase 7 attempt 1 FAILED: engine API down.** `quotient_server` crash-looped (620 restarts).
  **Root cause (the best find of the night): the generated engine DB password contained `#`.**
  `postgres://engineuser:#9mT*…@quotient_database:5432/engine` — `#` starts a URL fragment, truncating the
  DSN at the password, so the app connected to a garbage host and failed in ~80 ms with zero DB-side auth
  attempts. Diagnosis path: DB container healthy, psql inside the DB container works, fresh test container
  on the same compose network resolves + connects fine, no auth failures in DB logs, server dies in
  milliseconds → only a malformed DSN explains all of that. **Fix: URL-safe password in `/opt/quotient/.env`
  + `ALTER USER` + `--force-recreate server` → "Credentials seeded successfully" immediately.**
  Action item: `random_password()` must exclude URL-specials for anything that lands in a DSN.
- **01:10 — phase 7 attempt 2: `is live` banner**, but with two gaps: no `tz-ready` snapshots and no domain
  joins. Reason: those steps live at the END of phase 6 (after the nakon sweep that had aborted), and my
  `--from-phase 7` skipped them by design. Ran the phase-6 tail surgically (domain config → beacons →
  `tz-ready` snapshots ×10 → checkpoint(6)) — both forests (`team113.local`, `team114.local`) promoted,
  win01s joined OK.
- **01:30 — `root-nginx` (the last failed pin) planted manually** via guest agent on web01 (one sed line:
  `user root;` in nginx.conf) — nginx test-passes and restarts as root.

Net result: **live range, 10/10 tz-ready, 12/12 injects, all logins working.**

## 3. verify-competition — 6/7 PASS

| check | result |
|---|---|
| logins | PASS |
| no_default_creds | PASS |
| services | UP (all 16) |
| isolation | PASS |
| **misconfig** | **FAIL — verifier cannot SSH to web01 to confirm artifacts** (same Linux-SSH issue as below) |
| misconfig_survival (cross-team) | PASS |
| injects | PASS (12/12) |

The one FAIL is the SSH problem, not missing artifacts — proven independently by blue (§5) and by the
successful guest-agent artifact check.

## 4. Red team (bad-auto) — deployed and ran, but too quiet

- Cyberfield wiring patched in the orchestrator's config builder (red_vmid 999, template
  `base-ubuntu24.04-fix`, storage `hdrives-zfs`, red_ip 10.0.0.244). Deployed first-try, no rework.
- validate-llm + `--once --dry-run` passed; agent ran its real loop on red01.
- **Actual output over ~60 min: 2 actions** (`cred_spray` ×1, `health_check` ×1). Scoreboard never moved:
  team1 7-8 UP / team2 7-8 UP the whole event. No service was ever taken down; no credlist lockout.
- Causes to investigate (not yet root-caused — the LLM decisions and pacing config are the suspect):
  the local endpoint's latency on bad-auto's big digest prompts, `decision_window_min` pacing vs the
  60-minute duration, and whether `validate_decision()` is silently rejecting candidate decisions.
  Fix round must include a bad-auto `--dry-run` trace review with real timing.
- Also note: red01's own log shows transient paramiko "Error reading SSH protocol banner" spam (same
  network as the Linux-EOF problem — likely the same planted pin interfering with red's sessions to
  Linux boxes).

## 5. Blue team — the big engineering find of the E2E

- **Stage-blues worked; every cycle returned rc=1.** Same "Unexpected server error" on BOTH endpoints
  (100.64.0.9 and 10.0.0.143) while raw `curl` chat-completions to both endpoints were healthy — so not
  an LLM problem.
- Isolation took a while because the failure is *invocation-shaped*:
  - manual `opencode run` from a shell → works, every time;
  - exact same command, exact same env, spawned from python (`subprocess.run`, stdin=DEVNULL,
    `start_new_session=True`) → **opencode's server dies silently right after "llm runtime selected"**,
    empty server log, no OOM, no core, no signal trace.
  - pty attached → still fails. → not a TTY problem.
  - **`bash -c 'exec opencode run …'` → works.** The only working differentiator is having a shell as
    the parent process. Reproduced 3× deterministically.
- **Fix (local, committed on this branch):** `_opencode_run` now spawns
  `bash -c 'exec opencode run -m <provider>/<model> --auto "$CYCLE_PROMPT"'` with the prompt passed via
  the `CYCLE_PROMPT` env var (avoids shell-quoting the multi-KB prompt through the command line).
- After the fix: both blues ran real cycles — team1's agent produced a genuinely useful defense log
  (below). Remaining problem is pacing, not correctness: cycles are *productive but slow* against the
  local endpoint and often hit `CYCLE_TIMEOUT`. Trim the cycle prompt and/or raise the timeout.
- **Team1 blue's actual defense work** (from `blue-team1/LOG.md`):
  - confirmed all 8 scored services UP through its own scoreboard queries;
  - **independently discovered the Wardline persistence** on dc01+win01: HKLM Run keys (`PulseSyncAgent`,
    `BadgeSyncAgent`), `C:\ProgramData\Wardline\` with `WrapMedArchiveSync.exe`, `masync.ps1`,
    `psync.ps1`, `ledger.ps1`, `WrapChartCacheSvc.exe`, `charts.ps1`, `tflush.ps1` and their sleep timers —
    matching injects #3/#4 exactly;
  - correctly diagnosed "Linux boxes unreachable via SSH" and fell back to Windows-side hunting;
  - followed the cycle protocol (notebook → ≤2 actions → stop).

## 6. Fix-round backlog for the real practice (ordered)

1. **Find and neutralize the Linux SSH-breaking pin** (highest priority — it blocks red's foothold
   expansion, blue's hunt on all 6 Linux boxes, the verifier's misconfig check, and phase-6's own DNS
   step). Candidate: `sshd-force-sftp-broken-chroot` or a sibling. Either fix its script, drop it from
   the extreme pin set, or move it to the very end of the plan.
2. **`random_password()` → URL-safe for engine secrets** (the `#`-in-DSN crash loop). Exclude
   `#/?:@%` from anything written into `/opt/quotient/.env`; or quote the DSN.
3. **bad-auto pressure**: review a `--dry-run` decision trace with real timings; check
   `decision_window_min`/`min_standing_services` pacing vs event length; confirm `validate_decision()`
   isn't silently dropping valid plans. Target: ≥1 action per 5 min sustained.
4. **Blue cycle prompt/timeout**: cycles time out mid-work; raise `CYCLE_TIMEOUT` and/or trim the
   cycle prompt (inject list alone is ~2 KB). Also consider a "finish in ≤N tool calls" instruction.
5. **Plant idempotency**: documented above — never resume phase 5 over a planted box; roll back to
   tz-base first. (Also: make the tz-base snapshot re-taken at the START of phase 5, not just once.)
6. **Noble dpkg-breakers** stay pruned (`tftpd-hpa-anon-write`, `postgresql-remote-access`) unless their
   scripts get pre-seed fixes; 5 Windows user-policy pins stay pruned until `local-user-win`'s generated
   passwords meet policy.
7. Minor: `verify-competition` misconfig check should fall back to the guest agent when SSH is dead
   (it would have passed this run).

## 7. Artifacts

- Range: `competitions/scrim-extreme-cyberfield-2026-09-22/` (credentials 0600, gitignored)
- Deploy log: `logs/deploy-cyberfield-2026-09-22.log` (7 phases + 3 repair cycles)
- E2E run dir: `scrim-runs/scrim-extreme-cyberfield-2026-09-22/` — blue workdirs (NOTEBOOK/LOG/feed/
  cycles), `bad-auto-state/events.jsonl`, `monitor.log`, `evidence/`
- Orchestrator log: `logs/scrim-cyberfield-e2e.log`, continuation: `logs/scrim-cyberfield-blues.log`
- Engine fix evidence: engine `.env.bak-urlfix`, journal of 620 restarts before the fix
- Cyberfield wiring notes: `docs/deploy-cyberfield-prompt.md`
