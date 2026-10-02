# Packet profiles — competition packets → ranges

A competition publishes a **packet** days ahead carrying the general shape: box lineup,
scored services and ports, IP scheme, default credentials, scoring weights, schedule,
rules. `compile-packet.py` compiles that shape into a deployable `competitions/<id>/`
bundle, and the pipeline deploys it unchanged. This is the inverse of
`generate-packet.py` (which renders a competitor packet FROM a config).

Two profiles ship as references:

- `packets/cde-2026/packet.yaml` — the internal CDE 2026 (MIRA Corp / Among Us):
  5 boxes incl. in-path pfSense, `mira-{team}.corp.sus`, 9 scored services with
  dual-credit SSH pairs, packet-published credentials, day schedule with a lunch freeze.
- `packets/maccdc-q-2026/packet.yaml` — MACCDC Qualifier rehearsal: the 11-VM pod
  collapses to 8 boxes; every PA/FTD/VyOS/Win11/POP3/public-pool gap is recorded in the
  fidelity report rather than silently dropped.

## The workflow

```bash
python3 compile-packet.py packets/cde-2026/packet.yaml --dry-run   # plan + fidelity, writes nothing
python3 compile-packet.py packets/cde-2026/packet.yaml             # emits competitions/<id>/
python3 create-competition.py --competition cde-2026 --teams 4 --yes --scoring-vmid 1010
python3 verify-competition.py competitions/cde-2026 --packet packets/cde-2026/packet.yaml
python3 run-schedule.py packets/cde-2026/packet.yaml --t0 "2026-10-03 09:30"   # plan
python3 run-schedule.py packets/cde-2026/packet.yaml --execute freeze          # lunch
```

Vulns are deliberately NOT compiled: `box_vulns.json` stays the black team's secret
layer, authored after compilation. A packet-compiled bundle deploys clean
(use `verify --expect-no-vulns` until misconfigs are authored).

## Profile schema (packet.yaml)

| Section | Fields | Notes |
|---|---|---|
| `packet` | `source` | provenance line for the fidelity report |
| `event` | `comp_id`, `name`, `scenario`, `difficulty`, `teams_suggested`, `team_identifiers` | `comp_id` must be free (or pass `--force`). `teams_suggested`/`team_identifiers` are **inert** — no Python consumer reads them; they are documentation for whoever runs `create-competition.py --teams N` (and the `TF_VAR_team_identifiers` suggestion) |
| `domain` | `name_template` | e.g. `"mira-{team}.corp.sus"` → Compfile `domain_prefix`/`domain_suffix`; omit for the `team<id>.local` default |
| `credentials` | `box_username`, `box_password`, `credlists.{linux,domain}`, `domain_accounts[]`, `out_of_scope[]`, `note` | verbatim packet credentials; see below |
| `boxes[]` | `name`, `packet_os`, `template`, `last_octet`, `cpu`, `memory_mb`, `disk_gb`, `disk_iface`, `unmanaged`, `domain_role`, `fidelity`, `note` | `fidelity` ∈ exact/substituted/unsupported is REQUIRED per box |
| `services[]` | `box`, `name`, `port`, `pin`, `display`, `dual_credit`, `vars`, `check`, `fidelity`, `note` | `pin` is a catalog config name, or `score/<slice>` (e.g. `score/tcp`) for a native service scored without a plant |
| `schedule[]` | `label`, `at_min` (T0 offset; null = unscheduled note), `note` | drives `run-schedule.py` and the fidelity report |
| `scoring` | `weights` | metadata only — the engine scores flat 5 pts/check/round |
| `injects[]` | `slug`, `title`, `open/due/close_offset_min`, `description` or `briefing` | compiled to `injects/<slug>/` |

Validation refuses: unknown pins (a pin with no Quotient check would plant and score
nothing), duplicate `<box>-<Display>`, unordered inject offsets, two DCs, unmanaged
boxes carrying services, invalid usernames/legacy-account collisions, missing fidelity
annotations, and a `credlists.domain` without a `domain.name_template`.

Validation does **not** resolve box templates: `packet_ops.validate_profile()` only requires
`boxes[].template` to be non-empty. Whether that name resolves to a template tagged `template` on
the target node is checked later, by the deploy preflight (`config_ops.preflight_gates`), which
hard-fails before touching infrastructure. A compile that passes is therefore not yet proof the
lineup can deploy.

## Authoring a packet.yaml for a new competition

Read [packets/cde-2026/packet.yaml](../packets/cde-2026/packet.yaml) first — it is the reference:
every field is present, and every substituted/unsupported item carries a `note` explaining the
trade. A minimal profile is much smaller:

```yaml
packet:
  source: "ACME Quals 2027 packet v2.1"
event:
  comp_id: acme-2027
  name: ACME Quals 2027
  scenario: "Defend ACME's e-commerce stack."
  difficulty: 6
credentials:
  box_username: acmeblue          # must not collide with a legacy distro account
  box_password: "Spr1ng2027!"     # packet-published, public by design
  credlists:
    linux: {acmeblue: "Spr1ng2027!"}
boxes:
  - name: web01
    packet_os: "Ubuntu 22.04 — Apache"
    template: base-ubuntu24.04-fix   # must resolve on the target node (deploy preflight)
    last_octet: 2
    cpu: 2
    memory_mb: 2048
    fidelity: substituted             # exact | substituted | unsupported (REQUIRED per box)
    note: "no 22.04 template; 24.04 is the proven stand-in"
services:
  - box: web01
    name: "Apache HTTP"
    port: 80
    pin: apache                       # catalog config name, or score/<check> for a native service
    display: http
    fidelity: exact
```

Recipe, in order:

1. **Transcribe the packet literally.** Copy the published credentials, ports, account names, and
   IP scheme verbatim — do not "fix" them (the CDE profile deliberately ships both `n0t_sus1` and
   `n0t_sus!`, because the packet does).
2. **One `boxes[]` entry per box type**, with `last_octet` from the packet's addressing. Pick the
   closest template that exists on the node and mark `fidelity` honestly (`substituted` +
   `note` beats pretending). Use `unmanaged: true` for an appliance you clone but never plant on
   (pfSense), and `domain_role: dc`/`member` for AD members.
3. **Map each scored service to a `pin`** — a nakon catalog config name, or `score/<check>`
   (e.g. `score/tcp`) for a service that is scored without a plant. Check `display` uniqueness
   per box. The pin must be a config the catalog actually has, or validation refuses.
4. **Add `schedule[]` windows** (`at_min` offsets from T0; `null` for an unscheduled note) if the
   event pauses — `run-schedule.py` executes them.
5. **Compile and read the fidelity report**:
   `python3 compile-packet.py packets/<event>/packet.yaml --dry-run`. It writes nothing and prints
   every exact/substituted/unsupported decision; fix the profile until the substitutions are ones
   you would defend in a briefing.
6. **Real compile + deploy**: `python3 compile-packet.py packets/<event>/packet.yaml`, then
   `create-competition.py --competition <id> --teams N --yes`. Deploy preflight resolves every
   `template` name — a typo fails there, not at compile.
7. **Author vulns separately, after** (see below), then verify with
   `verify-competition.py competitions/<id> --packet packets/<event>/packet.yaml`.

## What compilation emits

`Compfile` (incl. `domain_prefix`/`domain_suffix` + `packet_source`), `boxes.json`,
`users.json`, `box_services.json`, `domain_roles.json`, `injects/`,
`packet-fidelity.md`, and three 0600/gitignored secret files: `passwords.json`
(packet credentials), `domain_accounts.json` (packet AD accounts),
`box_baseline.json` (decoy accounts, see below).

## Pin extensions this layer added

- **Score-only pins** — `{"name": "score/tcp", "score_only": true, "check": "Tcp",
  "display": "dns", "port": 53}` scores a NATIVE service the catalog can't plant
  (AD's own DNS on 53, the AD SYSVOL share on 445). Stripped from the nakon machine
  list and the catalog check, present in event.conf and `expected_service_names`.
- **`IIS FTP` now scores** — mapped to the Ftp check (port 21, credlist auth); it used
  to plant and silently score nothing.
- **`credlist` pin override** — `{"name": "ssh", "credlist": "domain",
  "display": "ssh-domain"}` swaps which credlist file the check authenticates with.
- **Dual-credit emulation** — `dual_credit: true` emits the local check plus a
  domain-credlist twin. Both score 5: both-up = 10, local-only = 5 — the packet's
  "domain full / local half" 2:1, at the cost of doubled scoreboard rows. The domain
  twin needs the packet AD account planted (below) and the box domain-joined.

## Packet credentials

`passwords.json` makes the deploy use the packet's published credentials verbatim
instead of random mints — that IS the competition (teams get the packet and rotate at
minute zero), and it makes redeploys deterministic. `verify --packet` inverts the
default-credential guard: instead of rejecting default literals, every published pair
must match `credentials.txt` exactly.

Domain credlists (`credlists.domain`) are pushed as `<name>.credlist` next to
`linux.credlist`; the cred-carrying checks authenticate over sssd/AD. This is the
newest engine surface — if the engine rejects a second credlist, drop the
`dual_credit` twins in the profile and recompile (single-check fallback).

## Baseline and AD accounts

- `out_of_scope[]` (e.g. CDE's `scorebot`/`blackteam`/`red_scoring`) plants a decoy
  account on every managed box with a random password (`box_baseline.json`): the
  packet promises these accounts exist untouched, and teams enumerate accounts in
  minute-zero IR. `verify --packet` probes for them.
- Windows boxes hosting cred-carrying services also get credlist users as
  `local-user-win` baseline pins (IIS FTP authenticates against local Windows users;
  Linux gets its credlist users from `fix_services_on_boxes` as before).
- `domain_accounts[]` plants packet-published AD accounts per team right after DC
  promotion (CDE's blueteam + color admins), marker-gated like the misconfig pass.

## Schedule

The engine has no schedule model — pausing IS the schedule. `run-schedule.py` reads
`schedule:` and executes the windows: `start` (competition/start + unpause),
`freeze`/`resume` (pause/unpause — CDE's 12:00–12:45 lunch), `end` (capture the final
scoreboard + injects into `<comp>/evidence/` BEFORE pausing). Inject offsets anchor at
T0 via the usual re-anchor path (`run-agent-scrim.py` / redeploy).

## Fidelity honesty

`packet-fidelity.md` records exact / substituted / unsupported for every box, service,
credential, and schedule fact, with reasons. A rehearsal range never claims parity it
doesn't have — read the report before briefing anyone on "we replicated X".

## Gotchas carried from the pipeline

- Verify gates in packet mode: `--packet <profile>` adds `packet_creds` +
  `packet_accounts` on top of the standard gates; combine with `--strict-services`
  and (pre-vulns) `--expect-no-vulns`.
- Freeze before committing (code drift after `--freeze` trips the frozen gate).
- `--scoring-vmid` must dodge existing VMs; on .150 never 1000 (orphan engine).
- The in-path pfSense at `.1` is a manual runbook step
  (`docs/pfsense-inpath-2026-09-28.md`), not terraform — for validation deploys the
  box can sit idle while the engine holds `.1`.
