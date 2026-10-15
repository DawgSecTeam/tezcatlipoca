# Packet profiles — competition packets → ranges

A competition publishes a **packet** days ahead carrying the general shape: box lineup, scored
services and ports, IP scheme, default credentials, scoring weights, schedule, rules.
`compile-packet.py` compiles that shape into a deployable `competitions/<id>/` bundle, and the
pipeline deploys it unchanged. This is the inverse of `generate-packet.py` (which renders a
competitor packet FROM a config).

Two reference profiles ship under `packets/`: `cde-2026/packet.yaml` (in-path pfSense, dual-credit
SSH pairs, published credentials, a day schedule with a lunch freeze) and
`maccdc-q-2026/packet.yaml` (an in-path VyOS perimeter, with every unsupported gap recorded in the
fidelity report rather than silently dropped).

## Workflow

```bash
python3 compile-packet.py packets/cde-2026/packet.yaml --dry-run   # plan + fidelity, writes nothing
python3 compile-packet.py packets/cde-2026/packet.yaml             # emits competitions/<id>/
python3 create-competition.py --competition cde-2026 --teams 4 --yes --scoring-vmid 1010
python3 verify-competition.py competitions/cde-2026 --packet packets/cde-2026/packet.yaml
python3 run-schedule.py packets/cde-2026/packet.yaml --t0 "2026-10-03 09:30"   # plan
python3 run-schedule.py packets/cde-2026/packet.yaml --execute freeze          # lunch
```

Vulns are deliberately NOT compiled: `box_vulns.json` stays the black team's secret layer,
authored after compilation. A packet-compiled bundle deploys clean (use `verify --expect-no-vulns`
until misconfigs are authored).

## Profile schema (`packet.yaml`)

| Section | Fields | Notes |
|---|---|---|
| `packet` | `source` | Provenance line for the fidelity report |
| `event` | `comp_id`, `name`, `scenario`, `difficulty`, `teams_suggested`, `team_identifiers` | `comp_id` must be free (or pass `--force`). `team_identifiers` are fallback defaults for a fresh deploy when neither `TEZ_TEAM_IDENTIFIERS` nor saved local identifiers exist; an explicit environment override wins |
| `domain` | `name_template` | e.g. `"mira-{team}.corp.sus"`; omit for the `team<id>.local` default |
| `credentials` | `box_username`, `box_password`, `credlists.{linux,domain}`, `domain_accounts[]`, `out_of_scope[]`, `note` | Verbatim packet credentials — see below |
| `boxes[]` | `name`, `packet_os`, `template`, `last_octet`, `cpu`, `memory_mb`, `disk_gb`, `disk_iface`, `unmanaged`, `in_path`, `domain_role`, `fidelity`, `note` | `fidelity` ∈ exact/substituted/unsupported is required per box; `in_path` (with `unmanaged`) makes the box the team's in-path gateway at `.1` |
| `services[]` | `box`, `name`, `port`, `pin`, `display`, `dual_credit`, `vars`, `check`, `fidelity`, `note` | `pin` is a catalog config name, or `score/<slice>` (e.g. `score/tcp`) for a native service scored without a plant |
| `schedule[]` | `label`, `at_min` (T0 offset; null = unscheduled note), `note` | Drives `run-schedule.py` and the fidelity report |
| `scoring` | `weights` | Metadata only — the engine scores flat 5 pts/check/round |
| `injects[]` | `slug`, `title`, `open/due/close_offset_min`, `description` or `briefing` | Compiled to `injects/<slug>/` |

Validation refuses: unknown pins, duplicate `<box>-<Display>`, unordered inject offsets, two DCs,
unmanaged boxes carrying services, invalid usernames/legacy-account collisions, missing fidelity
annotations, and `credlists.domain` without `domain.name_template`.

Validation does **not** resolve box templates — `validate_profile()` only requires
`boxes[].template` to be non-empty. Whether the name resolves on the target node is checked later
by the deploy preflight, which hard-fails before touching infrastructure. A compile that passes is
not yet proof the lineup can deploy.

## Authoring a packet.yaml

A minimal profile:

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
    fidelity: substituted             # exact | substituted | unsupported (required per box)
    note: "no 22.04 template; 24.04 is the proven stand-in"
services:
  - box: web01
    name: "Apache HTTP"
    port: 80
    pin: apache                       # catalog config name, or score/<check> for a native service
    display: http
    fidelity: exact
```

1. Transcribe the packet literally — ports, account names, IP scheme, credentials verbatim; don't
   "fix" them.
2. One `boxes[]` entry per box type, `last_octet` from the packet's addressing. Pick the closest
   existing template and mark `fidelity` honestly. `unmanaged: true` for a clone-but-never-plant
   appliance; `in_path: true` when it's the routed gateway (`last_octet` must be 1, and only one
   per lineup); `domain_role: dc`/`member` for AD members.
3. Map each scored service to a `pin` (a catalog config name, or `score/<check>` for a service
   scored without a plant); check `display` uniqueness per box.
4. Add `schedule[]` windows if the event pauses — `run-schedule.py` executes them.
5. `compile-packet.py packets/<event>/packet.yaml --dry-run` — writes nothing, prints every
   fidelity decision; iterate until the substitutions are ones you'd defend in a briefing.
6. Real compile, then `create-competition.py --competition <id> --teams N --yes` (the deploy
   preflight resolves every `template` name — a typo fails there, not at compile).
7. Author vulns separately, after, then `verify-competition.py competitions/<id> --packet
   packets/<event>/packet.yaml`.

## What compilation emits

`Compfile` (incl. `domain_prefix`/`domain_suffix` + `packet_source`), `boxes.json`, `users.json`,
`box_services.json`, `domain_roles.json`, `injects/`, `packet-fidelity.md`, and three 0600
gitignored secret files: `passwords.json` (packet credentials), `domain_accounts.json` (packet AD
accounts), `box_baseline.json` (decoy accounts).

## Pin extensions this layer added

- **Score-only pins**: `{"name": "score/tcp", "score_only": true, "check": "Tcp", "display": "dns", "port": 53}` scores a native service the catalog can't plant (AD's own DNS, SYSVOL). Stripped from the nakon machine list and catalog check; present in `event.conf` and `expected_service_names`.
- **`credlist` pin override**: `{"name": "ssh", "credlist": "domain", "display": "ssh-domain"}` swaps which credlist file the check authenticates with.
- **Dual-credit emulation**: `dual_credit: true` emits the local check plus a domain-credlist twin (both-up = 10, local-only = 5 — the packet's domain-full/local-half 2:1, at the cost of a doubled scoreboard row). The twin needs the packet AD account planted and the box domain-joined.

## Packet credentials

`passwords.json` makes the deploy use the packet's published credentials verbatim instead of
random mints — that IS the competition, and it makes redeploys deterministic. `verify --packet`
requires every published pair to match `credentials.txt` exactly.

Domain credlists (`credlists.domain`) are pushed as `<name>.credlist` next to `linux.credlist`;
cred-carrying checks authenticate over sssd/AD. If the engine rejects a second credlist, drop the
`dual_credit` twins and recompile.

## Baseline and AD accounts

- `out_of_scope[]` plants a decoy account on every managed box with a random password
  (`box_baseline.json`): the packet promises these accounts exist untouched, and teams enumerate
  them in minute-zero IR. `verify --packet` probes for them.
- Windows boxes hosting cred-carrying services also get credlist users as `local-user-win`
  baseline pins; Linux gets its from `fix_services_on_boxes`.
- `domain_accounts[]` plants packet-published AD accounts per team right after DC promotion,
  marker-gated like the misconfig pass.

## Schedule

The engine has no schedule model — pausing IS the schedule. `run-schedule.py` reads `schedule:`
and executes the windows: `start` (competition start + unpause), `freeze`/`resume`, `end`
(captures the final scoreboard and injects into `<comp>/evidence/` before pausing). Inject offsets
anchor at T0 via the usual re-anchor path (`run-agent-scrim.py`/redeploy).

## Fidelity honesty

`packet-fidelity.md` records exact/substituted/unsupported for every box, service, credential,
and schedule fact, with reasons. A rehearsal range never claims parity it doesn't have — read the
report before briefing anyone on "we replicated X".

## Gotchas carried from the pipeline

- `--packet <profile>` adds `packet_creds` + `packet_accounts` gates to verify; combine with
  `--strict-services` and (pre-vulns) `--expect-no-vulns`.
- Freeze before committing — code drift after `--freeze` trips the frozen gate.
- `--scoring-vmid` must dodge existing VMs.
- The in-path firewall at `.1` is automated, not a runbook step: phase 5 generates the per-team config, pushes it, waits for SSH on the WAN, cuts the engine's gateway over, and `verify_in_path` refuses the range when a team subnet does not route through its firewall ([internals.md](internals.md#net-firewall_opspy)). A lineup that declares `in_path` and cannot bring that firewall up fails the phase rather than deploying a range that does not score through it.
