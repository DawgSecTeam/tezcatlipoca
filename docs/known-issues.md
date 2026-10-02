# Known issues — open and pending

This file is the **open list only**: problems that are not fixed, or that need a decision. Resolved
incidents live in [incident-archive.md](incident-archive.md) (kept for the *why* behind each
mitigation); the pre-deploy trap checklist is
[e2e-testing.md §0](e2e-testing.md#0-read-before-you-deploy--live-traps); node/storage/template
ground truth is [environment-facts.md](environment-facts.md); credential exposure is
[security-disclosures.md](security-disclosures.md); defects owned by the catalog/nakon are
[upstream-defects-handoff.md](upstream-defects-handoff.md).

Last triage: **2026-10-02** — [known-issues-triage-2026-10-02.md](known-issues-triage-2026-10-02.md).

## Open — ours

### Planted Linux boxes deny all SSH at the PAM account stage after a restart

**Status: OPEN — root cause unsolved.** Phase-5-planted team1 Linux boxes stop accepting SSH entirely
after their next stop/start: the TCP handshake completes, then the connection dies preauth with
`fatal: Access denied for user <u> by PAM account configuration [preauth]`. Disk-persistent but
boot-triggered; every account module fails against verifiably-clean inputs, so the fault is
stack-level (config parsing, NSS, or module loading) in the account stage's boot-time environment.
Workaround: `redeploy --mode rebuild`, or never restart a planted box.

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
to confirm a fresh round. **Next action:** an engine-side watchdog that issues the same POSTs when
the stale signature appears (same pattern as the live `range-firewall.timer`), so a reboot — including
the owner-confirmed .193 hardware hard-downs — heals without an operator.

### `clean_engine_for_template` can wipe a *foreign* live engine on a shared mgmt IP

**Status: OPEN — mitigation only.** The engine-template build VM is booted on the planned engine
static mgmt IP, and the cleanup (`compose down -v`, `.env`/`event.conf` deletion, host-key wipe) is
delivered by **SSH to that IP**. With two engines of two comps (or an engine and a build VM) up on
one node, ARP flaps can make the cleanup land on a live foreign engine — observed destroying an
engine's `/etc/ssh/ssh_host_*`, `/opt/quotient/.env` and containers while its listener stayed up.

Mitigations: give each concurrent comp its own `TF_VAR_engine_mgmt_ip` (now in
[e2e-testing.md §0](e2e-testing.md#0-read-before-you-deploy--live-traps)); `ssh_via_gateway` self-heals
the resulting stale TOFU pin; recovery of a hit engine is `ssh-keygen -A` + `systemctl restart ssh`
via the guest agent, then `--from-phase 3`. **Next action:** address the target by VM identity
(vmid/guest-agent) instead of a shared IP, and refuse when the IP is ambiguous — see
[the triage §3 B3](known-issues-triage-2026-10-02.md).

### Fedora goldens cannot be built on SELinux-enforcing nodes

**Status: OPEN — workaround; live-confirmed 2026-10-02.** The qemu-guest-agent domain is confined:
on a Fedora 44 clone, guest-exec is root inside `virt_qemu_ga_t`, where `dnf`/`rpm` and writes under
`/etc` are `Permission denied`, `systemctl` is `Access denied`, and `setenforce 0` plus the
`virt_qemu_ga_run_unconfined` boolean are denied. Golden build reaches the box by SSH when the
gateway path works, and **SSH is the escape hatch** — the image's cloud user with the repo key lands
in `unconfined_t` with passwordless sudo (verified). So the block is agent-only flows; keep Fedora out
of lineups where the agent is the only path. **Fix (cheap, not yet applied):** flip the template's
`/etc/selinux/config` to permissive at template-build time. Detail and the verified SSH route:
[environment-facts.md](environment-facts.md#templates).

### llama.cpp local-blue context ceiling

**Status: OPEN — standing constraint.** opencode's own base prompt is ~20k tokens, so the local
endpoint's advertised context must leave room for it or opencode self-compacts fatally. `60000` is the
verified local value (cloud blue uses `120000`); do not set `reasoning_effort` for a local endpoint.
**Next action:** a launch-time assertion against the endpoint's real `n_ctx` instead of prose — see
[the triage §3 B6](known-issues-triage-2026-10-02.md).

### Template with no cloud-init drive could pass the template preflight

**Status: FIXED 2026-10-02** (moving to the archive). The preflight verified only that a selected
template resolved to a *tagged template*, so a template with no cloud-init drive — e.g. .150 `920
base-debian13-cloudinit`, despite its name — passed and its clones booted unreachable an hour later.
`config_ops._cloudinit_gate` now reads each selected Linux template's config (single-node and the
per-node multinode path, placed before the `check_free` skip so resumes are covered) and refuses with
the vmid, ostype and the `-fix` alternative. Windows and other non-Linux ostypes are exempt (identity
comes from `bootstrap_windows_box`); an unreadable config prints UNVERIFIED and does not block. The
usable/dead template inventory is in [environment-facts.md](environment-facts.md#templates).

### Freeze: the recorded commit was never read

**Status: FIXED 2026-10-02** (moving to the archive). Code-verified at the time: `.frozen.json`
recorded the code state (`commit`, `dirty`) but **no gate ever read it** — the deploy-time drift gate
compares per-template function/`main.tf` hashes from `.template-hashes.json`, and code-class drift is
*warn-only*. So the documented rule "commit before `--freeze` or the drift gate trips" was never what
the code did. Fix, in two parts:

- `--freeze` refuses when there are uncommitted **deploy-path code** changes (`.py/.tf/.sh/.j2/.ps1`);
  non-code dirt (comp JSON, `placement.json`, `nodes.json`, terraform state, `.env` backups) is
  expected after a run and only prints a note. The scoping is the load-bearing detail: the first cut
  refused on *any* `git status --porcelain` entry, which made `--freeze` impossible in a normal
  post-run worktree (verified on the scale8 worktree: a modified `placement.json`, an untracked
  `nodes.json`, a `.env` backup). `git_commit_info`'s recorded `dirty` now means the same thing, so
  runtime state cannot raise a false drift warning later.
- `template_ops.frozen_code_drift` reads the recorded commit at deploy time and warns loudly on a
  moved commit or uncommitted code, naming both commits and the `--unfreeze --confirm-unfreeze` path.
  Deliberately warn-only: resuming after a docs commit is normal, and no strict-drift toggle exists.

## Pending upstream — owned by the catalog / nakon

The four Linux catalog rows that were listed here were **fixed and verified live** on 2026-10-02
(noble/debian13/fedora44), removed from `constants.KNOWN_BROKEN_CONFIGS`, and archived with their
corrected root causes: [vulndb-fixes/](vulndb-fixes/), [incident-archive.md](incident-archive.md).
What remains:

- `mailenable-cleartext-mail-win` — the only Windows pin still in `KNOWN_BROKEN_CONFIGS`. The row
  plants no mail service at all: `choco install mailenable` names a package the community feed does
  not have, so only its firewall rule lands. A fix candidate (MailEnable's own installer + the real
  service names) is recorded in [vulndb-fixes/mailenable-cleartext-mail-win.candidate](vulndb-fixes/)
  but is **unverified** — the silent install only completes the MAPI-connector component.
  Handoff: [upstream-defects-handoff.md §5](upstream-defects-handoff.md).
- `local-user-win`, `powershell-execution-unrestricted`, `rpc-proxy-on-dc-web-win`,
  `unauth-kiosk-app-startup-win` — **fixed, verified live and removed** from
  `KNOWN_BROKEN_CONFIGS` on 2026-10-02 (lab vmid 131). The old
  "Set-LocalUser/password-policy ordering" story was an inference from three unrelated names and
  was wrong: none of these four touches an account or password policy. Real defects were a
  string-vs-integer flag comparison, a terminating `Set-ExecutionPolicy` under nakon's
  Process-scope Bypass, non-existent feature/property names, and a prerequisite gap reported as a
  script bug. Bodies and evidence: [vulndb-fixes/](vulndb-fixes/).
- `local-user` (linux) — **closed**: the live catalog already carries the portable fix, and there is
  no seed file anywhere (`vulndb-interfaces/schema.sql` is schema only), so nothing can revert it;
  the nightly dumps in the vulndb VM are the only restore path.
- `nakon randomize` handing out names whose script needs vars it cannot supply — **catalog half
  fixed**: `nakon catalog check` now reports `missing-vars` (`f86c11d`), and the driver additionally
  filters unplantable bare names on the fresh path (`nakon_ops._drop_unplantable_bare`). `randomize`
  itself was deliberately left unchanged, so a direct `nakon randomize` can still return such a name;
  `catalog check` is the gate.
- Windows package-manager fallback — the step now writes `.nakon-step-rc` and states plainly that it
  has never run against real winget/chocolatey (`12c3195`). Still not live-verified on Windows.
- `install_package` — **stale doc, no defect**: the function no longer exists, and the current package
  step records its rc into `report.tsv` and the FAILED tally.

The repo-side guard for the broken catalog names is `constants.KNOWN_BROKEN_CONFIGS`, enforced by
`nakon_ops._validate_known_broken_pins` at generate time and by `packet_ops` at packet-compile time.
It is now down to the single Windows row above (`mailenable-cleartext-mail-win`). Scoping,
deliberately: a **fresh selection** that gets past
the driver's `_drop_unplantable_bare` filter is a hard error; a competition that **already records**
the pin (any comp dir with `box_services.json`/`box_vulns.json`) gets a WARNING, so the historical
comps stay re-deployable. Residual gap: a brand-new hand-authored comp dir is on that "reuse" path
and therefore warns rather than errors (packet comps are caught at compile). The list is down to the
one row above; it shrinks further when `mailenable` is fixed.

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
