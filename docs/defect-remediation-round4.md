# Round-4 remediation: the per-VM lock, the seed's credential gap, and the misconfig re-verify

Follow-up to `defect-remediation-2026-10-16.md`, closing the items that pass left open. Live
estate values live in `infrastructure-status.md`; this document names the file, never the
values. Raw run logs are under `logs/round4/`.

## 1. crimson-relay's third attempt deployed

The two earlier attempts died on PVE lock contention. Attempt 3 (`run-5cfe9c3d`, engine head
`0a8757f4`) **succeeded 8/8**.

### What actually killed attempt 2

Not a foreign race. `lifecycle.stop_vm` gave `POST /status/shutdown` a 120 s *client* timeout
while PVE's own `qmshutdown` task kept running — and kept `lock-2351.conf`. The immediate
force-stop then lost that lock:

```
RuntimeError: Proxmox task UPID:…:qmshutdown:2351:…: timed out after 120s
RuntimeError: Proxmox task UPID:…:qmstop:2351:…: failed: can't lock file
              '/var/lock/qemu-server/lock-2351.conf' - got timeout     (run-dd88db6d)
```

The fix is at the cause plus a backstop:

- `lifecycle.stop_vm` now passes PVE its own `timeout` (120 s) and waits 240 s, so the shutdown
  task ends and releases the lock on its own instead of being abandoned mid-flight; it then
  checks the power state before force-stopping.
- Every VM-scoped task in `infra/lifecycle.py` goes through one helper,
  `infra.client.run_vm_task`, which retries a task whose failure names a lock file (stop / start /
  snapshot are idempotent) and raises a non-lock failure at once — the same shape as the datastore
  clone and conversion retries.

Proving tests: `tests/test_proxmox_task_failure.py::VmScopedLockRetry` (the PVE timeout hand-off,
the retry, its bound, and the non-lock pass-through). Live proof: attempt 3 printed
`Stopping golden boxes …` → `Converting golden boxes to templates … golden-win02 (vmid 2351) is
now a template` with no lock failure (`logs/round4/run-5cfe9c3d.run.log:2481-2487`). The storage
queue also held: `waiting for the storage queue … queuing instead of racing`.

## 2. `misconfig` re-verified on the fixed code

`verify-competition.py` over the attempt-3 range (`run-8669ee5a`):

```
misconfig : PASS  verifiable configs present on every team
  PASS  'bad-perms-userConfig' present on all 2 team(s): ['web01-team180', 'web01-team181']
```

The account-file-mode pins plant in the deploy's last pass, after every account writer
(`7db5750`), so the `useradd` that had been recreating `/etc/shadow` at its default mode no longer
erases them. The run's remaining FAILs are the held-range ones: `round_loop` (the deploy ended
`--hold-start`, so the clock never started) and the frozen-scoreboard consequence. `degradations`
PASSed — the attempt-3 deploy recorded no tolerated failures.

## 3. The seed: "unreachable over SSH" was a missing credential, not a network verdict

The deploy-time seed failed six-for-six with `unreachable over SSH` on every box. It was not
the network. Instrumenting `cred_spray` on a live red01 printed, for every box:

```
TRY 192.168.150.4 'medic' None (False, 'unreachable')
```

`None` is the password. A direct paramiko login with the box's real password succeeded in 0.1 s
from the same host, and a raw `/dev/tcp` to `:22` worked too.

**Cause.** bad-auto's nakon intel reads the box account and credlist from
`<comp_dir>/.deploy_state.json` — its terraform-era contract. The native (terraform-free)
pipeline never wrote that file. With it absent, `known_creds` still offered
`(user, None)` pairs; paramiko raised "no authentication methods"; `cred_spray` folded every
non-auth exception into its network branch and reported `unreachable over SSH`. The misleading
message is why this survived four rounds of live runs as a "network" bug.

**Fix (both ends).**

- tezcatlipoca phase 7 now writes `<comp_dir>/.deploy_state.json` (`box_password`, `box_creds`,
  0600) before the assume-breach step, so it rides to red01 with the other intel files.
- bad-auto's `known_creds` never offers a credential it cannot fill (`candidate_passwords` and
  `candidate_pairs` inherit it, so a re-run does not burn the attempt cap), and `_try_ssh` answers
  `auth` — not `unreachable` — when there is no password to try.

**Live proof.** On the up range, publishing the file took the exact seed command from
`ok: false, access 0/6` to a clean-state run of:

```
"boxes": 6, "access": 6, "root": 6, "implant": 6, "artifacts": 6,
"failed": [], "ok": true
```

Tests: `tests/test_phase_tail_offline.py::Tail::test_phase7_publishes_bad_autos_intel_state`,
`bad-auto tests/test_seed.py::CredentialGap` (three).

### A second, latent defect found while chasing: the shared red segment

The diagnostic detour was worth keeping. Every routed-red range used the **same** segment
(`10.200.0.0/24`, gateway `.1`, red01 `.10`) as a secondary on the shared management bridge, so
two live ranges put two engines on `10.200.0.1` and two red01s on `10.200.0.10` in one broadcast
domain; the engine that answered red01's ARP for `.1` became its gateway, and if that was the
other range's engine red's team traffic had no route. Observed live: both engines held
`10.200.0.1/24` at once, red01's cache flipped between them, and two red01s claimed `.10`.

Fixed by giving each range that stages red its **own** bridge (`vmbrR<run hex>`), created with the
team bridges and destroyed with them; the engine gets an extra NIC addressed as the segment
gateway there, and bad-auto's config names the bridge so red01 attaches its second NIC to it. The
segment addresses are unchanged — the private L2 is what makes them unique.

Tests: `tests/test_red_bridge.py`, `tests/test_estate_apply.py::Engine` (red NIC order),
`tests/test_estate_engine_setup.py::Netplan`, `bad-auto tests/test_engine_nat.py` (the segment and
team route move to the second NIC).

## 4. The estate

Round 3's three ownership sets (`crimson-relay`, `pfsense-rvb`, `scrim-one-light`) were confirmed
gone by an independent tag/bridge sweep, and the one range the earlier pass missed — crimson-relay's
re-run (`run-dd88db6d`, ownership `run-a84c26d4`) — was reclaimed through the normal destroy path,
which reported `estate clean for this ownership set`. The round-4 scrim-one-light range built for
this verification is left to be torn down after its reports are collected.
