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
idempotent). Owning the cutover inside terraform would need the firewall config state round-tripped
into tfvars — deferred until a deploy actually needs it.

### pfSense console bootstrap is blind typing

**Status: OPEN — accepted risk, mitigated.** The QEMU monitor API offers `sendkey` but no screen
reading (PVE's `screendump` writes a host file the API cannot read back; `termproxy` is serial-only
and the pfSense template's console is VGA). `firewall_ops` therefore drives the menu blind:
`CONSOLE_SETTLE_S=90` keeps keystrokes out of the FreeBSD loader, the drive sequence is idempotent,
and success is judged functionally — the fetched config enables SSH, so the WAN probe IS the signal.
A team that never comes up gets a console PNG (`logs/fw-console-<comp>-<team>.png`) plus a re-drive
hint; the fallback for a wedged console is the manual runbook
([pfsense-inpath-2026-09-28.md](pfsense-inpath-2026-09-28.md)). A provisioning-friendly pfSense
template (serial console + SSH on) would replace the whole dance — template work, not code work.


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

### Harness pre-T0 verify fails on packet gates for a stage_author-authored comp

**Status: OPEN — found 2026-10-04, blocks the scrim harness pre-run, unrelated to resets.**
`run-agent-scrim.py --new <id> --from-template competitions/cde-2026` authors a comp whose
regenerated `packet.md` contains NONE of the packet profile's decoy accounts
(scorebot/blackteam/red_scoring), and the harness's pre-T0 `verify-competition.py --packet`
then FAILs `packet_creds` + `packet_accounts` ("missing on web01-team130: scorebot, blackteam,
red_scoring") and aborts the run before the event window
([reports/reset-live-test-2026-10-03-report.md](reports/reset-live-test-2026-10-03-report.md), F7 —
range and red01 were deployed and healthy; teardown was done by hand). Either packet compile
drops the decoys from the regenerated packet.md, or verify reads a different profile revision
than compile planted. **Next action:** diff the profile's account set against
`packet_ops`' compile output for an authored comp; fix whichever side drifted.

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
**`round_loop_guard 1`** (default off). **Remaining:** run it on a live range — kill the loop
deliberately, watch the timer heal it, and only then consider it done.

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

### llama.cpp local-blue context ceiling

**Status: OPEN — standing constraint.** opencode's own base prompt is ~20k tokens, so the local
endpoint's advertised context must leave room for it or opencode self-compacts fatally. `60000` is the
verified local value (cloud blue uses `120000`); do not set `reasoning_effort` for a local endpoint.
**Next action:** a launch-time assertion against the endpoint's real `n_ctx` instead of prose.

### Teardown's pre-stop never matches team 1's boxes

**Status: OPEN — latent; the name pattern is wrong for exactly one team.** `pre_stop_windows_boxes`
exists to hard-stop every clone before `terraform destroy`, because a Windows DC whose guest agent is
down never complies with the provider's graceful shutdown and holds the `qm` lock, hanging the whole
destroy (`destroy_sweep_ops.pre_stop_windows_boxes`). It matches VM names as `<identifier>-<box>`
(the `f"{team['identifier']}-{box}"` match in `destroy_sweep_ops.py`), but terraform deliberately names team 1's VMs `team1-<box>` and only
other teams' `<identifier>-<box>` (`terraform/main.tf:251-261`, comment: "Keys keep the historical
naming (team1-<box>, <identifier>-<box>)"); `targets.enumerate_targets` implements the same
special-case, and every `teams.json` in the tree is keyed `team1…`
(`competitions/*/teams.json`). So team 1's boxes are never pre-stopped and can still hit the
graceful-shutdown hang the function was written to prevent; other teams pre-stop correctly. Not a
destroy failure — terraform still removes team 1 from state, just potentially slowly.

**Next action:** build the name from `range_ops.enumerate_targets` (`vm_name`) or apply the same
`team_key == "team1"` special case instead of re-deriving the pattern. Found 2026-10-03 while
surveying teardown for
[reports/automated-test-artifacts-plan-2026-10-03.md](reports/automated-test-artifacts-plan-2026-10-03.md)
(which must not copy this pattern when it enumerates boxes to collect artifacts from).

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
