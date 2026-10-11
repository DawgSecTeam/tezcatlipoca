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

**Status: OPEN — .150 side done (2026-10-08); .193 blocked on node shell access.** Template
`pfsense-provision` on .150 is now **vmid 958**: the agentless 957 was retrofitted with the
qemu-guest-agent offline (`tools/build-pfsense-provision-template.py agent-*` subcommands;
qemu-ga 11.1.2 + deps from `pkg.freebsd.org/FreeBSD:14:amd64`, ABI-matched to the guest's
own `pkg config abi`, started at boot via the `<system><afterbootupshellcmd>` tag in
config.xml — see internals.md). The agentless 957 is kept as
`pfsense-provision-agentless-bak`; 956 stock is untouched. **.193 has no pfsense-provision
at all** (it never did), and every transfer path is closed from the dev host: no SSH key or
password is configured for root@10.0.0.193 (`cyberfield.md` is gone from
`~/.hermes/infra-registry/`), the `root@pam!agent` API token cannot take file uploads
(`storage/upload` 400s even for tiny files — same token limit as the amongus import-from
rejection), and `termproxy` with a token lands on the raw console getty, not a shell. When
node shell access exists again: `vzdump 958 --stdout | ssh root@10.0.0.193 "qmrestore - 958
--storage hdrives-zfs"`, then convert to template + tag `template` (or just re-run the
agent-* subcommands against .193 with its env). Phase 5 still pushes each team's config one
at a time (every clone boots as 192.168.1.1; the engine can hold only one borrowed address),
and a busy node can still take minutes before a fresh clone answers on its WAN.

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

**Status: OPEN — unattended healing remains opt-in.** After an engine VM reboot the containers
restart but the round loop can stay stopped; `/api/engine` may show `running:false` (paused), a stale
`last_round.StartTime`, and the Go-zero `current_round_time`, while the scoreboard reads as frozen.

The 2026-10-07 live finding is fixed in the shared classifier: a paused engine with zero current time
and a last round older than the freshness window is now `STALE`; a deliberate pause with a real
current round time, or without the old-round reboot signature, remains `PAUSED`. Both the engine-side
watchdog and `verify-competition.py` consume that state, so the watchdog can issue its POST pair and
`--fix-round-loop` can reach its repair branch. Regression tests cover both cases. A live reboot trial
is still needed to validate the code path on an engine.

The watchdog remains opt-in: a competition must set `round_loop_guard 1` in its Compfile to install
`tools/round_loop_guard.py` as a 60-second timer using the dedicated `scoring` account. Without that
setting, verify detects and FAILs a frozen loop; `--fix-round-loop` issues the start/unpause POSTs,
and verify must be rerun to confirm a fresh round.

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

### Student portal: what the first live run did not cover

**Status: OPEN — needs follow-up runs.** The first live run (2026-10-08, `portal-live-2026-10-08`
on cyberfield .193: 2 teams × Ubuntu 20.04 + Windows Server 2019) proved several things:
- the engine reaches `:8006`;
- the console token is minted with exactly one `TezConsole` ACL per team VM;
- noVNC (capped at RFB 3.3) renders real consoles on Linux and Windows in a real browser;
- keystroke typing reaches the guest;
- the gate and cross-team refusals hold;
- the full `verify-competition.py` passes ([portal.md](portal.md#verification)).

Still unproven:
1. **The Cloudflare tunnel path.** No tunnel token was used. This includes the one-owner check and
   cookies and websockets behind Cloudflare.
2. **`pveproxy` under event-size concurrency.** At most one console was open at a time.
3. **Box rebuilds.** Whether Proxmox drops a rebuilt VM's console ACL
   (`portal-access.py <comp> resync`).
4. **Multi-node.** No satellite consoles yet.
5. **Teardown revocation.** The run was left up for inspection, so `destroy-competition.py` has not
   yet removed a live console user.

### Quotient did not enforce "one session per account" on 2026-10-08

**Status: OPEN — the documented rule may be stale.** Several modules and docs build on "Quotient
allows ONE session per account; a second login kills the first cookie":
- the `scoring` second-admin account (`quotient/setup.py` `_admin_accounts`);
- `engine_guard_ops.py`;
- `deploy_lib/stages.py`;
- the round-loop entry above;
- `usage-people.md`.

On `portal-live-2026-10-08`'s engine (Quotient `dab6a98`, 2026-09-13), two logins in a row as
`team1`, and separately as `admin`, both left the **first** cookie valid: `/api/teams` returned
200 for both, and 401 anonymously. Either upstream changed, or the eviction depends on something
this probe didn't reproduce (client identity, a different endpoint). Re-test before relying on the
rule either way. The separate `scoring` account is harmless if the rule is gone. The student
portal doesn't depend on it, because it never logs into Quotient
([portal.md](portal.md#logins-never-touch-quotient)).

### Student portal logins are shared per team

**Status: OPEN — accepted limitation.** The portal reuses Quotient's logins, and those are one
account per team (`team1`…). The only per-student attribution is the optional display name typed
at login, which is self-asserted and recorded in the access log. Per-student identity would mean
moving Quotient itself to LDAP/OIDC.

### Hand-run realm deploys need the FULL realm block in bad-auto's config.yaml

**Status: OPEN — operator trap, live-found 2026-10-11 (scrim-one redeploy).** The deploy derives
red01's config from the operator's `~/dev/dawgsec/bad-auto/config.yaml` through field allowlists;
a realm block that sets only `enabled`/`c2_ip`/`base_url` (what `realm_c2_ops.apply_local_c2`
rewrites) silently takes the legacy path server-side too: ONE http1 beacon per box aimed at the
team gateway address (dead behind an in-path firewall), zero tavern rows, and no DNAT rules for
any transport — while the provisioner still prints "tavern live … all transports" and the seed
records `implant: True` per box. Everything looks green but the C2 is dark. The block must carry
`transports` (all five, with gw_port/c2_port/domain) AND `callback_ip` (the engine transit IP,
e.g. `172.31.120.1` on this estate) — `config.example.yaml` `realm:` is the canonical shape. Repair
path used live: fix config → `realm_c2_ops.prepare_local_c2` → merge the `_red01_realm_cfg` subset
into red01's `/etc/bad-auto/config.yaml` (preserve `realm.pubkey`) → `engine_nat.install` with the
deploy-stamp args → clear `meta.seed[ip].implant` → re-seed. Same run also found the GX10 chat
template 500s on `reasoning_effort: "minimal"` ("Unexpected reasoning effort") — every red LLM
decision then falls back to scripted tactics; leave the key off for local endpoints.

### bad-auto world.json has no concurrent-writer protection

**Status: OPEN — upstream-worthy (bad-auto).** `World.save()` is a bare atomic write of the
in-memory dict: last writer wins. The director runs for the whole event holding a stale copy, so
any operator-side `badauto seed` that adds artifacts (e.g. re-running the implant stage after a
config fix) is clobbered by the director's next `save()` — live-found 2026-10-11: the 07:46
multi-transport re-seed's 24+ realm-imix artifact records vanished when the director's 07:47:23
health_check saved its 07:31-era world, while the planted units themselves stayed live on the
boxes (tavern had all 36 rows). The record-keeping undercounts red's real presence until the next
plant. A fix needs a save-side reload-merge (or director file locks), not just an flock — defer
to bad-auto, hand off with the upstream defects.

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
