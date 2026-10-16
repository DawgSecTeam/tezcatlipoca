# Round-4 remediation: the per-VM lock, the shared red segment, and the misconfig re-verify

Follow-up to `defect-remediation-2026-10-16.md`, closing the three items that pass left open.
Live estate values live in `infrastructure-status.md`; this document names the file, never the
values. Evidence: `logs/round4/`.

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

## 3. The seed's real defect: one fixed red segment on a shared L2

The seed's `unreachable over SSH` was not an SSH bug. Under bad-auto's default `routed` mode every
range put the **same** segment on the shared management bridge: red01 as `10.200.0.10/24`, its
engine as `10.200.0.1/24`. Two ranges at once means two engines claiming `.1` and two red01s
claiming `.10` on one broadcast domain, and whichever engine answers red01's ARP for `.1` becomes
its gateway. If that is the other range's engine, red's `192.168.0.0/16` traffic reaches a host
with no route to those team subnets and every box reads "unreachable".

Proven live (read-only) while a range was up with a second range's engine on the same L2:

- both engines held `10.200.0.1/24` on `eth0` simultaneously;
- red01's cache for `10.200.0.1` was whichever engine had last announced — right engine in the
  sample, which is exactly why this looks intermittent;
- `10.200.0.10` was simultaneously claimed by two different red01s, with two engines' neighbour
  caches pointing at one of them.

### The fix: a private per-run bridge

A range that stages red now gets its own red bridge (`red_bridge_name`: `vmbrR<run hex>`), created
with the team bridges and destroyed by teardown like any other recorded bridge. The engine gets an
extra NIC on it, addressed with the segment gateway by the engine netplan; bad-auto's config names
the bridge, so red01 attaches a second NIC there and keeps the segment off the management L2.
`ensure_segment_addr` and the re-assert script's `ip addr add` are skipped for the private bridge —
the address is netplan-managed on the red NIC, not a secondary on the management interface.

The segment addresses themselves stay `10.200.0.0/24` / `.1` / `.10`; the L2 is what makes them
unique now. `red_mode: masq` and standalone bad-auto (no `red_bridge`) keep the legacy single-NIC
shape.

Proving tests: `tests/test_red_bridge.py` (naming, the "does this range stage red" gate, the
bad-auto config keys), `tests/test_estate_apply.py::Engine` (the red NIC appends after the
transits), `tests/test_estate_engine_setup.py::Netplan` (the engine's red NIC address),
`bad-auto tests/test_engine_nat.py` (the segment + team route move to the second NIC).

Cross-repo: tezcatlipoca `fcff836`, bad-auto `77ac577`.
