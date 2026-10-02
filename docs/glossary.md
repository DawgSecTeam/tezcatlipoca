# Glossary

The vocabulary this project uses in a specific way. Terms are listed with the code that owns them,
because that is the definition that wins when a doc and the code disagree.

## Teams and identity

| Term | Meaning | Owner |
|---|---|---|
| **competition** ("comp") | One event instance: a `competitions/<id>/` directory holding `Compfile`, `boxes.json`, `teams.json`, secrets, and state. `<id>` matches `[a-z0-9._-]`. | `config_ops` |
| **team key** | The map key in `teams` / `TF_VAR_teams`: `team1`, `team2`, … Used for ordering, `for_each`, placement, and selection flags (`--teams team3`). | `config_ops.collect_teams` |
| **team identifier** | The subnet's third octet as a **string**: `"101"`, `"102"`, …. Drives `192.168.<identifier>.<host>`, the bridge name `vmbr<identifier>`, and the vmid stride. `--teams 103` also resolves to a team by identifier. | `config_ops`, `range_ops` |
| **anchor team** | `team1` — the team the pipeline hard-assumes exists (looked up **by name**). On single-node it also hosts the golden set's IPs; on multi-node each node has its own anchor (see *anchor identifier*). | `terraform/main.tf` (`team1_key`) |
| **anchor identifier** | The **lowest** team identifier placed on a host — the subnet a node's golden boxes sit on (`192.168.<anchor>.<240+i>`). Slot 0 anchors on `team1`'s identifier, which reproduces the original golden math. | `nodes_ops.anchor_identifier` |
| **slot** | A hosting position in a multi-node placement: slot 0 is the engine node, slots 1–4 are satellites (max 4). Golden vmids and jump vmids are slot-dimensioned. On single-node everything is slot 0, and nothing in `boxes.json`/`Compfile` mentions slots. | `nodes_ops`, `nodes_ops.golden_vmid_for_slot` |
| **engine vmid** | The scoring engine's VM id: `TF_VAR_scoring_vm_id`, default `1000`. The whole vmid layout hangs off it (engine template `+140`, goldens `+150 + slot*10`, jump `+130 + slot`). Overridable per run with `--scoring-vmid`. | `constants`, `deploy` |
| **umbrella: `enumerate_targets()`** | One entry per `(team, box)` — the only place a target's `vmid`/`ip`/`vm_name`/`machine` are derived. Build from the full lists, then filter; never re-enumerate a filtered box list. | `range_ops` |

## Boxes and templates

| Term | Meaning |
|---|---|
| **managed box** | Any box where `is_unmanaged(box)` is false: it gets cloud-init (Linux), the golden set, the nakon plant, repair/fix_services, and scoring. This is the default. |
| **unmanaged box** | `"unmanaged": true` — an appliance (in-path pfSense) the pipeline must **not** plant, configure, score, or domain-join. It is cloned straight from its own template with operator-specified NICs, has **no golden**, and is kept in `boxes.json` so per-box-index vmids stay positional. Every plant/config/scoring path skips it via `utils.is_unmanaged()`. |
| **box type** | An entry in `boxes.json` (`name`/`template`/`cpu`/`memory_mb`/`disk_gb`/`last_octet`). Boxes are keyed by type, never per team: every team defends the identical set, which is what makes Quotient's wildcard-IP checks valid. |
| **golden (golden set)** | One VM per box type, full-cloned from the base template, planted with the **golden stage** in strict mode, cloud-init-cleaned, stopped, and converted with `qm template`. Apply #2 creates every team's box as a **linked clone** of it. Lives at `engine_vmid + 150 + slot*10 + box_idx`. |
| **unbooted golden** | A golden that is deliberately **never booted** — the DC box type. Sysprep leaves the machine generalized, so each linked clone specializes its own SID before ADDS promotion and the team gets a unique DomainSID. Owned by `golden_ops.unbooted_golden_boxes()`; only a valid `domain_roles.json` can produce one. |
| **engine template** | The per-competition Quotient template (`engine_vmid + 140`): base image → bootstrap → clean → `qm template`. The deployed engine is a linked clone of it, so every run starts from a known-clean disk with an empty scoring DB. |
| **freeze / frozen** | A verified competition's template set is locked: config-class drift hard-fails, code-class drift warns and proceeds on the frozen template, and `--full` teardown needs `--end-of-competition`. Recorded in `.frozen.json` by `verify-competition.py --freeze`. |

## Nakon: configs, stages, passes, bundles

| Term | Meaning |
|---|---|
| **machine list / config file** | A JSON list of machines for nakon to act on (`id`/`name`/`ip`/`os`/`user`/`password`/`configurations`). `nakon-config.json` is the **full** per-machine expansion of the type-level selection. |
| **stage** | A **subset of the configurations** that must be planted together because of an ordering constraint. Exactly three: **golden** (identity-free, non-disruptive — rides the cloned disk), **repair** (sshd/sudoers touchers — must land after cloning and before the domain pass), **final** (disruptive + boot-hostile — must land after the domain reboots). Membership lives in `constants.REPAIR_STAGE_CONFIGS` / `FINAL_STAGE_CONFIGS`; the rationale is canonical in [architecture.md](architecture.md#data-flow). |
| **stage config file** | The per-stage machine list `generate_stage_configs()` writes: `.nakon-golden.json`, `.nakon-repair.json`, `.nakon-final.json`, plus the combined `.nakon-postclone.json` view (`repair ∪ final`) that redeploy replays. Multi-node adds `.nakon-golden-slot<N>.json`. All carry `box_password` and are gitignored. |
| **pass** | One `nakon deploy` **invocation**: one stage config + one bundle, run on the engine. v2 runs three passes per deploy (golden in phase 4, repair in phase 5, final in phase 6). AD steps get their own single-machine pass because they reboot the box. |
| **bundle** | Nakon's content-addressed build output (`nakon build`), cached under `vendor/nakon/bundles/` and keyed by config hash. It carries the payloads (and no credentials) and is built **operator-side** — the engine never sees vulndb creds. One bundle per pass. |
| **plant** | Actually applying a configuration to a box (what a pass does). "The plant is not idempotent over a planted box" is a resolved caveat in [incident-archive.md](incident-archive.md). |
| **misconfig** | A deliberately planted weakness (from `box_vulns.json`), scored by Quotient and never leaked into the competitor packet. Distinct from a **service**, which is a scored function the box should keep up. |
| **credlist** | The account list Quotient's auth-checking families (Ssh/Smtp/Imap/Sql/Ftp) authenticate with: `linux.credlist` (and, for packet comps, `<name>.credlist`). The names must equal the accounts `fix_services_on_boxes()` creates, or healthy boxes score down. |

## Packets and events

| Term | Meaning |
|---|---|
| **packet** | The public competition document: box lineup, scored services/ports, IP scheme, default credentials, schedule, rules. Modelled as `packet.yaml`. |
| **profile** | The hand-encoded `packets/<event>/packet.yaml` that `compile-packet.py` turns into a `competitions/<id>/` bundle. |
| **fidelity** | Per-box/service/credential honesty label: `exact`, `substituted`, or `unsupported`, plus a `note`. Compiled into `packet-fidelity.md`; a rehearsal range never claims parity it does not have. |
| **T0** | The event clock start: the moment the competition actually begins. Inject offsets are re-anchored to T0, not to deploy time. |
| **freeze window / end** | `run-schedule.py` windows read from the packet's `schedule:` (`start`, `freeze`/`resume`, `end`). The engine has no schedule model — pausing *is* the schedule. |
