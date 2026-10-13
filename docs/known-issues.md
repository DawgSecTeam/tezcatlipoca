# Known issues

Temporary writepad, not a ledger: every entry has a concrete next action and is drained each run cycle.

## Open

### `terraform apply` on a live firewall range rewrites the pre-cutover engine netplan

Accepted caveat. `null_resource.team_nics` writes the pre-cutover netplan (`192.168.<id>.1/24` on team NICs); phase 5's cutover moves the gateway to the firewalls. Trigger-gated, but a re-apply on a live firewall range whose triggers changed re-adds `.1` under the firewalls' feet. Fix: `create-competition.py --competition <id> --from-phase 5 --yes` afterward (idempotent). `verify-competition.py`'s `firewall_in_path` gate detects the drift. Owning the cutover inside terraform needs the firewall config state round-tripped into tfvars — deferred until needed.

### `pfsense-provision` exists only on .150

.150: vmid 958 (agent retrofitted; 957 kept as `-agentless-bak`). .193 has none, and node shell access from the dev host is closed (no SSH key/password for root@.193, the API token can't take file uploads, `termproxy` lands on the raw getty). **Next action**: when shell access exists, `vzdump 958 --stdout | ssh root@10.0.0.193 "qmrestore - 958 --storage hdrives-zfs"`, convert and tag.

### Planted Linux boxes deny all SSH at the PAM account stage after a restart

Root cause unsolved. A stop/start on a planted box can kill SSH preauth (`Access denied ... by PAM account configuration`) while services still listen. `redeploy --mode reset` recognizes the signature and escalates past it. Unreproduced since 2026-09-29 (four stop/starts came back clean); the standing repro lab was destroyed. **Next action**: commit the diagnostic kit (`tools/pam-bisect-lab.py`) so the next occurrence can be bisected.

### Scoring round loop does not auto-resume after an engine reboot

No unattended healing. The shared classifier now distinguishes a stale post-reboot pause from a deliberate one; `verify-competition.py --fix-round-loop` is the repair. **Next action**: a live reboot trial to validate the code path on a real engine.

### Fedora goldens cannot be built on SELinux-enforcing nodes

Mitigated, not closed. The qemu-guest-agent domain is confined on Fedora 44 (dnf/rpm/systemctl denied, `setenforce 0` denied); SSH is the escape hatch (cloud user lands `unconfined_t` with passwordless sudo). The template recipe now sets `SELINUX=permissive` before sealing, but only on rebuild — current node templates still enforce. **Next action**: rebuild the Fedora templates. Keep Fedora out of agent-only lineups until then.

### Student portal: unproven paths from the first live run

2026-10-08 proved console access, the gate, and verify PASS. Still unproven: the Cloudflare tunnel path (no token used), `pveproxy` under event-size concurrency (only one console was open), box rebuilds (console ACL survival), multi-node, teardown revocation (range was left up). **Next action**: a run exercising each.

### Quotient "one session per account" not reproduced on 2026-10-08

Several modules assume a second login evicts the first cookie (the `scoring` second-admin account, a comment in `deploy_lib/context.py`, `usage-people.md`). On `portal-live-2026-10-08`'s engine, two logins as `team1` and separately as `admin` both left the first cookie valid. **Next action**: re-test before relying on the rule either way; the `scoring` account and the student portal are harmless regardless.

### bad-auto `world.json` has no concurrent-writer protection

Upstream-worthy (bad-auto). `World.save()` is last-writer-wins; the director's periodic save can clobber an operator-side `badauto seed` that ran in between (live-found 2026-10-11: a 24+ artifact re-seed vanished under the director's next health-check save, though the planted units stayed live). **Next action**: hand off to bad-auto — needs a save-side reload-merge, not just a lock.

## Pending upstream — catalog / nakon

- **`IIS FTP` golden plant is timing-fragile and non-idempotent** (catalog DB step body). `Start-Service FTPSVC`/`New-WebFtpSite` can race the just-installed config store; the step's `$Error.Count -gt 0` footer turns a transient into a hard failure, and the body doesn't re-run cleanly without the phase-4 `tz-base` rollback. Recovery: `--from-phase 4 --yes`. Failed-run logs are preserved on the engine (`/opt/nakon/failed-<tag>-<ts>`) since 2026-10-11.
- **`mailenable-cleartext-mail-win`** — the only remaining `KNOWN_BROKEN_CONFIGS` row. `choco install mailenable` names a package the community feed doesn't have, so only the firewall rule lands; MailEnable's real installer is the likely fix.
- **`nakon randomize` can hand out names whose script needs vars it can't supply** — catalog half fixed (`catalog check` reports `missing-vars`; the driver filters unplantable bare names). `randomize` itself is unchanged, so `catalog check` remains the gate.
- **Windows package-manager fallback** — writes `.nakon-step-rc` and states plainly it has never run against real winget/chocolatey. Still not live-verified.

`constants.KNOWN_BROKEN_CONFIGS` is the repo-side guard, enforced at generate time and packet-compile time: a fresh selection hitting a broken row is a hard error; a competition that already records the pin gets a warning instead (so historical comps stay re-deployable). Delete a row here when fixed upstream — don't archive it.

## Standing limitations — not fixable here

- **Windows scoring is port-open only.** Every Windows service check maps to the generic `Tcp` slice (WinRM 5985, SMB 445, RDP 3389); it can't distinguish a healthy service from one merely listening.
- **Nakon step failures are recorded but never fatal by design.** A failed install shows as `FAILED` in `nakon_failed_steps`, surfaced by verify as a plant-integrity line; the deploy never raises on it.
- **Orphaned ranges and vmid contention on shared nodes.** Not a code bug — discarded comp dirs leave VMs holding vmids/disk/golden slots. Reclaim with `destroy-competition.py` from the matching comp dir, never an ad-hoc sweep.
