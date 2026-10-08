# Known issues — open and pending

This file is the **open list only**: problems that are not fixed, or that need a decision. Fixed
issues are **deleted, not archived** — git history is the record. The pre-deploy trap checklist is
[e2e-testing.md §0](e2e-testing.md#0-read-before-you-deploy--live-traps); node/storage/template
ground truth is [environment-facts.md](environment-facts.md); credential exposure is
[security-disclosures.md](security-disclosures.md); defects owned by the catalog/nakon are
[upstream-defects-handoff.md](upstream-defects-handoff.md).

## Open — ours

### A `terraform apply` on a LIVE firewall range rewrites the pre-cutover engine netplan

**Status: OPEN — accepted caveat.** `null_resource.team_nics` writes `/etc/netplan/60-team-ifaces.yaml`
with `192.168.<id>.1/24` on the team NICs (the pre-cutover shape that lets the pfSense consoles
fetch their config). Deploy phase 5's cutover rewrites that file (gateway address moved to the
firewalls, routes via `172.31.<id>.2`). The resource is trigger-gated so nothing rewrites it during
a normal deploy, but re-running `terraform apply` on a live firewall range whose triggers changed
re-adds `192.168.<id>.1` under the firewalls' feet — duplicate gateway addresses, ARP flux, and
half the team's traffic bypassing the firewall. If you must re-apply, re-run
`create-competition.py --competition <id> --from-phase 5 --yes` afterwards (the cutover is
idempotent). `verify-competition.py`'s `firewall_in_path` gate now detects the drift (route not
via the transit /30, firewall silent, or the gateway back on the engine). Owning the cutover inside terraform would need the firewall config state round-tripped
into tfvars — deferred until a deploy actually needs it.

### The `pfsense-provision` template exists only on .150, and phase 5 pushes teams one at a time

**Status: OPEN — accepted limitation.** Template `957 pfsense-provision` was built on cyberrange
.150 by `tools/build-pfsense-provision-template.py`; another node (or a multi-node firewall lineup)
needs the tool run there (or the template synced) before a deploy. Phase 5 pushes each team's
config over SSH one team at a time: every clone of the template boots as 192.168.1.1 and the engine
can borrow only one such address per run, so the bootstrap is not yet per-team concurrent. A fresh
clone can also take several minutes before it answers on its WAN address when the node is busy
(the push retries and the apply budget is 600s).

### Planted Linux boxes deny all SSH at the PAM account stage after a restart

**Status: OPEN — root cause unsolved.** Phase-5-planted team1 Linux boxes stop accepting SSH entirely
after their next stop/start: the TCP handshake completes, then the connection dies preauth with
`fatal: Access denied for user <u> by PAM account configuration [preauth]`. Disk-persistent but
boot-triggered; every account module fails against verifiably-clean inputs, so the fault is
stack-level (config parsing, NSS, or module loading) in the account stage's boot-time environment.
Workaround: `redeploy --mode rebuild`, or never restart a planted box. Since 2026-10-03
`redeploy --mode reset` recognizes the preauth signature in its post-rung health probe
(`ssh_ops.classify_ssh_failure`) and escalates past it automatically — the `tz-base` replant
rung lands on the pre-plant disk, and rebuild remains the last resort. **Live observation
2026-10-03/04** (reset matrix, [reports/reset-live-test-2026-10-03-report.md](reports/reset-live-test-2026-10-03-report.md)):
four stop/starts of planted boxes (db01 ×2, ftp01 ×2 through rollback/rebuild) all came back
SSH-clean — the trap did not fire, and remains unreproducible by intent.

Investigated 2026-09-29: the visible pin set does **not** reproduce it, and the standing lab
(`tzlive-pamlab`, vmid 1181) was destroyed in the same cleanup — the incident is currently
unreproducible from the repo. **Next action:** commit the diagnostic kit as a script
(`tools/pam-bisect-lab.py`: plant rungs + `sshd2` on 2225 + `pam_exec` probe log) so the next
occurrence can be bisected. Full evidence and the kit:
[reports/pam-account-stage-after-restart-2026-09-24.md](reports/pam-account-stage-after-restart-2026-09-24.md).

### Scoring round loop does not auto-resume after an engine reboot

**Status: OPEN — detected, not automated.** After an engine VM reboot the containers restart but the
round loop stays stopped; `/api/engine` shows a stale `last_round.StartTime` and zero
`current_round_time`, and the scoreboard reads as frozen (verify reads the *last scored* round, so a
stopped loop reports stale DOWN as if live).

Mitigated: `verify-competition.py` detects the signature (old StartTime + zero current_round_time,
engine unpaused), **FAILs** the run, and `--fix-round-loop` issues the two POSTs
(`/api/competition/start {"started":true}`, then `/api/engine/pause {"pause":false}`); re-run verify
to confirm a fresh round.

**Next action:** an engine-side watchdog that issues the same POSTs when the stale signature appears
(same pattern as the live `range-firewall.timer`), so a reboot — including the owner-confirmed .193
hard-downs — heals without an operator. **Automation implemented 2026-10-02, opt-in and not yet trialled live:** the blocker was that a
watchdog could not simply log in — Quotient allows one session per account, so the login would evict
whichever operator, `verify-competition` or harness session held `admin`. Every competition now seeds
a **separate `scoring` admin account** for exactly this (`event.conf`'s `admin` list; per-competition
password in `credentials.txt`), and `round_loop.py` holds the one definition of "the loop is
stopped" — shared with verify's gate so the two cannot disagree. The actor is
`tools/round_loop_guard.py`, installed on the engine as a 60s timer when the Compfile sets
**`round_loop_guard 1`** (default off).

**Live trial 2026-10-07 (cde-2026 on .193, clean engine reboot): the guard cannot heal the
signature actually observed.** After the reboot the engine came back with `running:false`
(**paused**) plus the Go-zero `current_round_time`, not the unpaused-stale shape the 2026-09-29
pfsense-ad incident produced. `round_loop_state()` classifies paused as intentional and returns
PAUSED — so the guard (verified via its own `--dry-run`: "paused: nothing to do") stays silent,
AND `verify-competition.py`'s round-loop gate returns PASS ("engine paused") at
`verifier/engine.py` before the `--fix-round-loop` branch is ever reached, so the operator path
cannot fire either. The scoreboard stayed frozen ~10 minutes until the two POSTs were issued
manually (as the `scoring` account); the loop advanced within one Delay of the unpause. **Two
code sites share the gap:** `round_loop.py` (paused ⇒ never stale) and `verifier/engine.py`
(paused ⇒ PASS before the fix branch). Suggested fix: when paused AND `current_round_time` is the
Go zero time AND `last_round.StartTime` is older than the freshness window (a deliberately-paused
live loop has real round times; a reboot-frozen one does not), treat as healable in both the
guard and the fix branch. Until then, healing a rebooted engine is manual: the POST pair
(`/api/competition/start {"started":true}` then `/api/engine/pause {"pause":false}`).

### Fedora goldens cannot be built on SELinux-enforcing nodes

**Status: OPEN — mitigated; the template recipe is fixed, the templates are not rebuilt.** The
qemu-guest-agent domain is confined: on a Fedora 44 clone, guest-exec is root inside `virt_qemu_ga_t`,
where `dnf`/`rpm` and writes under `/etc` are `Permission denied`, `systemctl` is `Access denied`, and
`setenforce 0` plus the `virt_qemu_ga_run_unconfined` boolean are denied. Golden build reaches the box
by SSH when the gateway path works, and **SSH is the escape hatch** — the image's cloud user with the
repo key lands in `unconfined_t` with passwordless sudo (verified). So the block is agent-only flows;
keep Fedora out of lineups where the agent is the only path. **Fix — recipe applied 2026-10-02,
templates not yet rebuilt:** the template-build recipe now sets `SELINUX=permissive` in
`/etc/selinux/config` before sealing
([usage-people.md](usage-people.md#non-ubuntu-linux-box-templates-fedora-alpine)), which only takes
effect on the next rebuild, so the Fedora templates currently on the nodes still enforce. Detail and
the verified SSH route: [environment-facts.md](environment-facts.md#templates).

## Pending upstream — owned by the catalog / nakon

- `mailenable-cleartext-mail-win` — the only Windows pin still in `KNOWN_BROKEN_CONFIGS`. The row
  plants no mail service at all: `choco install mailenable` names a package the community feed does
  not have, so only its firewall rule lands. A fix candidate (MailEnable's own installer + the real
  service names) is recorded in [vulndb-fixes/mailenable-cleartext-mail-win.candidate](vulndb-fixes/)
  but is **unverified** — the silent install only completes the MAPI-connector component.
  Handoff: [upstream-defects-handoff.md §5](upstream-defects-handoff.md).
- `nakon randomize` handing out names whose script needs vars it cannot supply — **catalog half
  fixed**: `nakon catalog check` now reports `missing-vars` (`f86c11d`), and the driver additionally
  filters unplantable bare names on the fresh path (`nakon_ops._drop_unplantable_bare`). `randomize`
  itself was deliberately left unchanged, so a direct `nakon randomize` can still return such a name;
  `catalog check` is the gate.
- Windows package-manager fallback — the step now writes `.nakon-step-rc` and states plainly that it
  has never run against real winget/chocolatey (`12c3195`). Still not live-verified on Windows.

The repo-side guard for the broken catalog names is `constants.KNOWN_BROKEN_CONFIGS`, enforced by
`nakon_ops._validate_known_broken_pins` at generate time and by `packet_ops` at packet-compile time.
It is now down to the single Windows row above (`mailenable-cleartext-mail-win`). Scoping,
deliberately: a **fresh selection** that gets past
the driver's `_drop_unplantable_bare` filter is a hard error; a competition that **already records**
the pin (any comp dir with `box_services.json`/`box_vulns.json`) gets a WARNING, so the historical
comps stay re-deployable. Residual gap: a brand-new hand-authored comp dir is on that "reuse" path
and therefore warns rather than errors (packet comps are caught at compile). When `mailenable` is
fixed, delete its bullet — do not archive it.

## Standing limitations — not fixable here

- **Windows scoring is port-open only.** The driver maps every Windows service check to the generic
  `Tcp` slice (`quotient/setup.py`: WinRM 5985, SMB 445, RDP 3389), so it cannot tell a healthy
  service from one merely listening. Quotient upstream is not vendored here, so this is verified from
  the driver's slice set rather than Quotient source — a native check type we have not mapped cannot
  be ruled out, but nothing in this pipeline uses one.
- **Nakon step failures are recorded but never fatal by design.** Code-verified 2026-10-02: there is
  no `install_package` any more; the generated package step (`vendor/nakon/nakon/gen/bash.py`) runs
  the real apt/dnf/yum/apk command and its rc *is* recorded (the "deliberately no `set -e`" comment is
  explicit), so a failed install shows as a `FAILED` step instead of being swallowed. The deploy still
  never raises: `run_nakon` counts `FAILED` step lines into `.deploy_state.json`
  (`nakon_failed_steps`), which `verify-competition.py` surfaces as a plant-integrity line, and the
  staged bundle is verified to have landed engine-side before a plant.
- **Orphaned ranges and vmid contention on the shared nodes.** Not a code bug: past competitions whose
  comp dir was discarded leave running VMs holding vmids, disk, and golden slots (43 VMs / 20 running
  at the 2026-10-02 check). Reclaim with `destroy-competition.py` from the matching comp dir — never an
  ad-hoc sweep. Detail and the per-node table:
  [environment-facts.md](environment-facts.md#vmid-occupancy); pre-deploy checks:
  [e2e-testing.md §0](e2e-testing.md#0-read-before-you-deploy--live-traps).
