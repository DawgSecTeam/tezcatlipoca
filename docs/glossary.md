# Glossary

The vocabulary this project uses in a specific way. Terms are listed with the code that owns them,
because that is the definition that wins when a doc and the code disagree.

## Teams and identity

| Term | Meaning |
|---|---|
| **competition** ("comp") | One event instance: a `competitions/<id>/` directory holding `Compfile`, `boxes.json`, `teams.json`, secrets, and state (`config_ops`). |
| **team key** | The map key in the `teams` roster (`estate-state.json`'s `secrets.teams`): `team1`, `team2`, … Used for ordering, placement, and selection flags (`--teams team3`) (`config_ops.collect_teams`). |
| **team identifier** | The subnet's third octet as a **string**. Drives the team subnet, the `vmbr<identifier>` bridge name, and the vmid stride; `--teams 103` also resolves a team by identifier (`config_ops`, `common/targets`). |
| **anchor team** | `team1` — the first roster key, whose box names the golden set anchors on. On single-node it also hosts the golden set's IPs. |
| **anchor identifier** | The **lowest** team identifier placed on a host — the subnet a node's golden boxes sit on. Slot 0 anchors on `team1`'s identifier, reproducing the original golden math (`placement_record.anchor_identifier`). |
| **slot** | A hosting position in a multi-node placement: slot 0 is the engine node, the rest are satellites. Golden and jump vmids are slot-dimensioned; on single-node everything is slot 0 and nothing in `boxes.json`/`Compfile` mentions slots (`placement_record`). |
| **engine vmid** | The scoring engine's VM id (`--scoring-vmid`, state `scoring_vm_id`). The whole vmid layout hangs off it — the offsets are `common/constants.py` and `placement_record`'s `*_for_slot` helpers. |
| **umbrella: `enumerate_targets()`** | One entry per `(team, box)` — the only place a target's `vmid`/`ip`/`vm_name`/`machine` are derived. Build from the full lists, then filter; never re-enumerate a filtered box list (`common/targets`). |

## Boxes and templates

| Term | Meaning |
|---|---|
| **managed box** | Any box where `utils.is_unmanaged()` is false: it gets cloud-init (Linux), the golden set, the nakon plant, pre-domain/fix_services, and scoring. The default. |
| **unmanaged box** | `"unmanaged": true` — an appliance the pipeline must **not** plant, configure, score, or domain-join. Cloned straight from its own template with no cloud-init identity and no golden, but kept in `boxes.json` so per-box-index vmids stay positional. |
| **in-path firewall** | `"unmanaged": true` + `"in_path": true` — the team's GATEWAY, holding the team's `192.168.<id>.1`. Phase 5 pushes the generated config and cuts the engine over; `verify_in_path` then refuses a range that does not route through it. `utils.is_in_path_fw()` is the predicate, and it is a SUBSET of unmanaged, so every unmanaged skip applies to it. |
| **transit bridge** | The per-team `/30` between the engine and the firewall's WAN, built like `vmbr<id>` and reclaimed by `destroy-competition.py`; a leftover one refuses a fresh deploy's preflight. |
| **box type** | An entry in `boxes.json`. Boxes are keyed by type, never per team: every team defends the identical set, which is what makes Quotient's wildcard-IP checks valid. |
| **golden (golden set)** | One VM per box type, full-cloned from the base template, planted with the **golden stage**, cloud-init-cleaned, stopped, and converted with `qm template`. Every team's box is a **linked clone** of it. |
| **unbooted golden** | A golden that is deliberately **never booted** — the DC box type. Sysprep leaves the machine generalized, so each linked clone specializes its own SID and the team gets a unique DomainSID. |
| **engine template** | The per-competition Quotient template: base image → bootstrap → clean → `qm template`. The deployed engine is a linked clone of it, so every run starts from a known-clean disk and an empty scoring DB. |
| **freeze / frozen** | A verified competition's template set is locked: config-class drift hard-fails, code-class drift warns and proceeds. A full teardown of a frozen competition needs `--end-of-competition`. |

## Nakon: configs, stages, passes, bundles

| Term | Meaning |
|---|---|
| **machine list / config file** | A JSON list of machines for nakon to act on. `generate_stage_configs()` writes one per stage; `nakon-config.json` is the full per-machine expansion of the type-level selection. All carry the box password and are gitignored. |
| **stage** | A **subset of the configurations** that must be planted together because of an ordering constraint. Membership and the naming live in `common/constants.py`; the rationale for the split is canonical in [architecture.md](architecture.md#data-flow). |
| **pass** | One `nakon deploy` **invocation**: one stage config + one bundle, run on the engine. |
| **bundle** | Nakon's content-addressed build output, keyed by config hash. It carries the payloads and **no credentials** — it is built operator-side, so the engine never sees vulndb creds. One bundle per pass. |
| **plant** | Actually applying a configuration to a box (what a pass does). Re-planting an already-planted box is not supported — roll back to `tz-base` first. |
| **misconfig** | A deliberately planted weakness (from `box_vulns.json`), scored by Quotient and never leaked into the competitor packet. Distinct from a **service**, a scored function the box must keep up. |
| **credlist** | The account list Quotient's auth-checking families (Ssh/Smtp/Imap/Sql/Ftp) authenticate with. The names must equal the accounts `fix_services_on_boxes()` creates, or healthy boxes score down. |

## Packets and events

| Term | Meaning |
|---|---|
| **packet** | The public competition document: box lineup, scored services/ports, IP scheme, default credentials, schedule, rules. Modelled as `packet.yaml`. |
| **profile** | The hand-encoded `<comps-root>/<event>/packet.yaml` that `compile-packet.py` turns into a `competitions/<id>/` bundle. |
| **fidelity** | Per-box/service/credential honesty label (`exact`, `substituted`, `unsupported`) plus a note, compiled into `packet-fidelity.md`. |
| **T0** | The event clock start: the moment the competition actually begins. Inject offsets are re-anchored to T0, not to deploy time. |
| **freeze window / end** | `run-schedule.py` windows read from the packet's `schedule:`. The engine has no schedule model — pausing *is* the schedule. |
