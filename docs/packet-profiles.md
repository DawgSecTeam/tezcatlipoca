# Packet profiles — competition packets → ranges

A competition publishes a **packet** days ahead carrying the general shape: box lineup, scored
services and ports, IP scheme, default credentials, schedule, rules. `compile-packet.py` compiles
that shape into a deployable `competitions/<id>/` bundle, and the pipeline deploys it unchanged. This
is the inverse of `generate-packet.py` (which renders a competitor packet FROM a config).

Two reference profiles ship in the comps repo beside their comp definitions:
`cde-2026/packet.yaml` (in-path pfSense, dual-credit SSH pairs, published credentials, a day
schedule with a lunch freeze) and `maccdc-q-2026/packet.yaml` (an in-path VyOS perimeter, with every
unsupported gap recorded in the fidelity report rather than silently dropped).

## Workflow

```bash
python3 compile-packet.py cde-2026/packet.yaml --dry-run   # plan + fidelity, writes nothing
python3 compile-packet.py cde-2026/packet.yaml             # emits <comps-root>/<id>/
python3 create-competition.py --competition cde-2026 --teams 4 --yes --scoring-vmid 1010
python3 verify-competition.py cde-2026 --packet cde-2026/packet.yaml
python3 run-schedule.py cde-2026/packet.yaml --t0 "2026-10-03 09:30"   # plan
python3 run-schedule.py cde-2026/packet.yaml --execute freeze          # lunch
```

`run-schedule.py` executes the profile's `schedule:` windows — plan with `--t0`, act with
`--execute <window>`. The engine has no schedule model: pausing IS the schedule
([internals.md](internals.md#packet_ops--compile-packet--run-schedule)).

Vulns are deliberately NOT compiled: `box_vulns.json` stays the black team's secret layer, authored
after compilation. A packet-compiled bundle deploys clean (use `verify --expect-no-vulns` until
misconfigs are authored).

## Authoring a packet.yaml

**`config/packet_ops.validate_profile()` is the schema and the validator** — it is what refuses, so
read it for the exact key set and messages. A minimal profile:

```yaml
packet:
  source: "ACME Quals 2027 packet v2.1"
event:
  comp_id: acme-2027
  name: ACME Quals 2027
  scenario: "Defend ACME's e-commerce stack."
  difficulty: 6
credentials:
  box_username: acmeblue
  box_password: "Spr1ng2027!"          # packet-published, public by design
  credlists:
    linux: {acmeblue: "Spr1ng2027!"}
boxes:
  - name: web01
    packet_os: "Ubuntu 22.04 — Apache"
    template: base-ubuntu24.04-fix
    last_octet: 2
    cpu: 2
    memory_mb: 2048
    fidelity: substituted               # exact | substituted | unsupported
    note: "no 22.04 template; 24.04 is the proven stand-in"
services:
  - box: web01
    name: "Apache HTTP"
    port: 80
    pin: apache
    display: http
    fidelity: exact
```

The judgement calls the validator can't make for you:

- **Transcribe the packet literally** — ports, account names, IP scheme, credentials verbatim; don't
  "fix" them. `passwords.json` makes the deploy use them as-is, which is what makes redeploys
  deterministic.
- **Pick the closest existing template and mark `fidelity` honestly** (required per box; an omitted one
  is a refusal, not a default). A substituted box you cannot defend in a briefing is the thing this
  whole layer exists to surface.
- **One `boxes[]` entry per box type**, `last_octet` from the packet's addressing; `unmanaged: true`
  for a clone-but-never-plant appliance, `in_path: true` when it's the routed gateway (one per
  lineup), `domain_role: dc`/`member` for AD members.
- **One `display` per box** — Quotient needs unique `<box>-<Display>` check names.
- **`event.team_identifiers`** are fallback defaults for a fresh deploy, used only when neither
  `TEZ_TEAM_IDENTIFIERS` nor saved local identifiers exist; an explicit environment override wins.
  **`scoring.weights`** is metadata only — the engine scores flat per check per round.

**Validation does not resolve box templates.** It only requires `boxes[].template` to be non-empty;
whether the name resolves on the target node is checked later by the deploy preflight, which hard-fails
before touching infrastructure. A compile that passes is not yet proof the lineup can deploy.

Then: `--dry-run` (writes nothing, prints every fidelity decision) until the substitutions are ones
you'd defend; real compile; `create-competition.py --competition <id> --teams N --yes`; author vulns
separately after; `verify-competition.py competitions/<id> --packet <profile>`.

## What compilation emits

The competition bundle `create-competition.py` consumes, plus `packet-fidelity.md` and three 0600
gitignored secret files: `passwords.json` (packet credentials), `domain_accounts.json` (packet AD
accounts), `box_baseline.json` (decoy accounts).

The fidelity report records exact/substituted/unsupported with reasons for every box, service,
credential and schedule fact. **A rehearsal range never claims parity it doesn't have**, and this is
what you read before briefing anyone on "we replicated X".

## Service pins

Three forms a `pin:` can take (`config/packet_ops._service_pins()` builds them):

- **A catalog config name** — planted by nakon, scored by the matching Quotient check.
- **`score/<slice>`** — score-only: scores a native service the catalog can't plant (AD's own DNS,
  SYSVOL) without planting anything. Stripped from the nakon machine list, present in `event.conf`.
- **With `dual_credit: true`** — the local check plus a domain-credlist twin, so both-up scores full
  and local-only half: the packet's domain-full/local-half ratio, at the cost of a doubled scoreboard
  row. The twin needs the packet AD account planted and the box domain-joined.

A `credlist:` key on any pin swaps which credlist file the check authenticates with.

## Credentials, baseline and AD accounts

`passwords.json` makes the deploy use the packet's published credentials verbatim instead of random
mints — that IS the competition. **`verify --packet` requires every published pair to match
`credentials.txt` exactly.** Domain credlists (`credlists.domain`) are pushed as `<name>.credlist`
next to `linux.credlist`, and cred-carrying checks authenticate over sssd/AD; if the engine rejects
a second credlist, drop the `dual_credit` twins and recompile.

- `out_of_scope[]` plants a decoy account on every managed box with a random password
  (`box_baseline.json`): the packet promises these accounts exist untouched, and teams enumerate them
  in minute-zero IR. `verify --packet` probes for them. Windows boxes hosting cred-carrying services
  get their credlist users as `local-user-win` baseline pins; Linux gets its from
  `fix_services_on_boxes`.
- `domain_accounts[]` plants packet-published AD accounts per team right after DC promotion,
  marker-gated like the misconfig pass.

## Gotchas

- `--packet <profile>` adds `packet_creds` + `packet_accounts` gates to verify; combine with
  `--strict-services` and (pre-vulns) `--expect-no-vulns`.
- Freeze before committing — code drift after `--freeze` trips the frozen gate.
- `--scoring-vmid` must dodge existing VMs.
- The in-path firewall at `.1` is automated, not a runbook step, and `verify_in_path` refuses a range
  that does not route through it — so a lineup declaring `in_path` whose firewall will not come up
  fails phase 5 rather than deploying a range that does not score through it
  ([internals.md](internals.md#netfirewall_opspy)).
