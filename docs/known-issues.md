# Known issues & incident log

Live-confirmed incidents, standing failure modes, and limitations of this range — the things
that have actually burned a deploy or a scrim. Design consequences live in
[internals.md](internals.md) (deploy pipeline) and [scrim-harness.md](scrim-harness.md) (agent
harness); symptom-level fixes live in [usage-people.md](usage-people.md)'s Troubleshooting table.

## Live-confirmed incidents

### sshd start-limit crash loop (2026-09-03)

Several catalog configs (`ssh-root-login`, `ssh-empty-passwords`, `ssh-password-auth`,
`ssh-max-auth-retries-high`, `ssh-x11-forwarding`, …) each independently run
`systemctl restart ssh`. Landing several on one box back-to-back with no delay trips systemd's
crash-loop protection (`Result: start-limit-hit`), leaving sshd down for the rest of the
competition. Confirmed live on web01-team101: 5 `ssh-*` configs, journalctl showed 5 restarts
inside the same second.

Mitigation (hardening_ops): misconfig passes are spaced so restarts don't land in the same
instant, and the SSH script ends with `systemctl reset-failed ssh` + a conditional start, falling
through to the guest-agent fallback when SSH is already gone — either way sshd ends the pass
running.

### Clones come up without a routable address (e2e-2026-09-19)

Clones can boot without the `ipconfig0` address (a cloud-init race), and ifupdown loses the
address on any later carrier blip. First seen on app01; before the fix this surfaced as a phase
6/7 failure an hour after cloning. Mitigation (clone_ops): `_repair_box_network` re-writes the
address, `ensure_cloned_network` gates phase 6/7 on every box actually holding its IPv4, and
guest-agent exceptions count as "not yet" so a merely-slow cloud-init is never "repaired".

### Quotient cold start exceeds 60 s (e2e-2026-09-19)

The first `compose up` after a `--no-cache` build cold-starts Postgres (initdb) and recreates
every container — measured >60 s twice in that run. The engine bootstrap timeout is therefore
600 s (engine_ops), not a "generous" round number.

### opencode dies 40/40 with "Unexpected server error" (agent-scrim-2026-09-17c)

Every blue session launch in the 17c run failed instantly with an opencode server error caused
by the launch context (a `capture_output` thread launch) — a failure that never reproduced when
run in the foreground. `run-agent-scrim._opencode_run` is hardened instead of trusting the
context: no stdin, own process group, per-team `HOME`/`XDG_*` so opencode's state DB and server
logs live inside the run dir.

### Red evidence nearly lost to scp flakes (2026-09-17b)

red01's `events.jsonl` is the only complete record of what red did. The direct
operator→red01 scp hung past 60 s in the 09-17b run; the file had already been at risk twice.
`run-agent-scrim` now grabs it at every snapshot tick (a teardown-time flake can't lose it) and
falls back to tunneling through the scoring engine as a jump host.

### "Scoreboard unreachable" / `{"error":"Forbidden"}` — three-round mystery

Two independent causes, each of which produced weeks of confusing Forbidden errors:

- **Quotient allows ONE session per account.** Every login kills that account's previous cookie,
  so any ad-hoc login (browser, curl debugging) silently invalidates the harness's session.
  Rule: never log in ad hoc; run `./qlogin` only when a request returns `Forbidden`. The newest
  login always wins, so concurrent qlogin refreshes converge on the only valid cookie.
- **The services API keys on the engine's own IDs** (1, 2, …). Querying it with the subnet
  identifier from `teams.json` (101, 102) answers `Forbidden` even with a valid cookie — the
  second half of the mystery (`_team_tid` in run-agent-scrim maps team → engine ID).

### Datastore saturation under parallel clones (HTTP 596)

Concurrent full clones saturate the datastore/Proxmox API (pvestatd hangs, HTTP 596 errors).
Terraform therefore runs with `-parallelism=1`, the apply timeout scales per Windows box
(60 GB vs 15 GB Linux images), and Proxmox task waits budget 1800 s for slow storage.

### Proxmox source-VM lock race

Proxmox locks the source template during a clone; concurrent clones from the same template race
for the lock and the loser gets a short timeout. `terraform/main.tf` gives clones `retries = 15`
(~2 minutes of retry budget) — enough for the winning clone to finish and release.

### Docker wipes iptables on every start/restart

Docker re-syncs iptables on any container start/restart: `FORWARD` policy goes to `DROP` and the
custom team-subnet MASQUERADE + team-to-team DROP rules vanish. The MASQUERADE loss is silent —
nakon swallows the resulting apt failures (`install_package` ignores exit status), so services
just don't install. Mitigations (engine_ops): `ensure_nat_forwarding` re-asserts both rules
before each nakon pass, `range-firewall.timer` re-asserts them every 30 s for the life of the
range, and `range-healthcheck.timer` logs rule/container status every 60 s.

### llama.cpp context ceiling (agent-scrim-2026-09-16)

opencode's own base prompt is ~20k tokens; the endpoint's advertised context must leave room for
it or opencode self-compacts fatally. 60000 was verified against the slot limit — don't raise
model context claims above what the slot actually holds, and don't set `reasoning_effort`.

## Known-broken templates

- **`106` / `ubuntu24.04`** and **`920` / `debian13-lite`**: bad cloud-init — clones never get a
  working network/SSH. Use the `-fix` variants instead (see usage-people.md "Adding a template
  VM"). `deploy()` warns but does not block: a reused competition replays its saved `boxes.json`
  exactly, so the interactive box-picker warning can be silently outlived — the deploy-time
  warning is unconditional (not gated by `--yes`) because non-interactive/CI deploys skip the
  prompts too.

## Infrastructure failure modes

- **Fresh clones boot with an empty `/etc/resolv.conf`** (cloud-init ignores `dns.servers` when
  the IP is static), and nakon installs every service with `apt-get`. `fix_dns_on_boxes` exists
  because of a now-deleted helper (`terraform/scripts/prepare_boxes.py`) whose write-up is the
  cautionary tale: nakon reports none of this back — install failures are ignored and the deploy
  never raises — so a box that skips DNS repair installs nothing, Terraform still exits 0, and
  the whole range comes up with every service down.
- **Never point boxes at their own team's `dns*` box from Terraform.** Deadlock: its bind9 is
  installed by nakon, via apt, which needs a resolver that already works — and `fix_dns_on_boxes`
  forces `8.8.8.8` over the top anyway, so the dns* box was never actually serving its team. To
  make a dns* box its team's real resolver, repoint boxes *after* nakon has run.
- **Thick LVM cannot snapshot.** Snapshot-capable datastores: ZFS, LVM-thin, Ceph, or qcow2 on
  file storage. A range deployed on thick LVM has no `tz-base`/`tz-ready` restore points and
  `redeploy-competition.py` falls back to `reconfigure`/`rebuild`.
- **netplan refuses (and warns loudly about) world-readable configs** — the engine's team-NIC
  netplan file is written mode 0600.
- **New VirtIO NICs need a cold boot** — a guest-level reboot does not trigger the PCI scan that
  enumerates them, so the engine's team NICs are addressed only after a hypervisor-level
  stop/start.
- **Quotient crash-loops without `event.conf`, and every container restart drops the team-subnet
  NAT** — hence `event.conf` is pushed before nakon ever runs, and the firewall timer exists.
- **Windows package-manager fallback untested**: nakon's `winget`/`choco` fallback path was never
  exercised by any catalog config used in the verified Windows pass.

## Standing limitations

- **Box/credlist usernames are themeable, not secret.** Login and credlist account names default
  to `ubuntu`/`admin`/`user1`/`user2` and can be renamed per competition via
  `competitions/<id>/users.json` — the names aren't secret either way. The *passwords* are always
  generated fresh per competition.
- **Quotient's Postgres/Redis passwords live plaintext on the engine** (`/opt/quotient/.env`),
  generated fresh per deploy by the driver — not in this repo and not from `.env`, but readable
  on the scoring engine by anyone holding it.
- **Windows scoring is port-open only.** Quotient has no native SMB/RDP/WinRM check type, so
  those get a generic `Tcp` dial-and-connect check — it cannot tell a healthy service from one
  that is merely listening.
- **Nakon failures are silent by design.** `install_package` ignores apt exit status and the
  deploy never raises; a mis-seeded box looks exactly like a successful one from the deploy log.
  `verify-competition.py` exists partly because of this.
- **Scrim evictions are not yet observable** — `scrim-report.py` counts them as 0 (metric gap in
  the interaction score), so the report understates blue activity by whatever evictions happened.

## Security disclosure history

- **2026-08-06 git-history audit**: found a real admin password and team passwords for the CDE
  2026 competition (since torn down) in a pre-2026-07-09 commit predating the
  "stop tracking competition secrets" history rewrite. The commit is no longer reachable from
  any branch but was pushed to GitHub before the rewrite — treat it as permanently public. The
  exposed passwords were rotated and are unused. No real Proxmox API token or nakon/vulndb
  password was found anywhere in this repo's history. A separate `nakon` checkout must audit its
  own history independently.
