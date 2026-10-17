# Deploy: tezcatlipoca + tlaloc on red01

Who owns what (design principle 3):

- **tezcatlipoca** owns the range: cloning red01, the red segment, the engine's
  red rules and C2 DNAT (with their 30s re-assert timer), the Mythic install,
  destroy, and the teardown that captures red's verdict.
- **tlaloc** owns decisions and effects: what to do, to which box, with what
  mechanism, and the restore knowledge for everything it changed.

## Compfile red block

Every competition stages tlaloc in phase 7: there is no `red.engine` knob and no
fallback engine. The Compfile's `red.*` block is the competition's ROE and the
only red input the deploy reads:

```
red.assume_breach full      # full | light | off
red.depth 3                 # 0 access · 1 +escalate · 2 +channel · 3 +persistence/evasion
red.duration_min 150
red.caps.max_down 4
red.caps.min_standing 1
red.windows.endgame_teardown 15
red.windows.lockout 3
red.budget.extra_pressure_max 2
red.clef_url http://203.0.113.30:8080
red.c2_url https://10.0.0.210
red.c2_callback_port 4470   # else the Compfile's `firewall_dnat` supplies it
red.c2_ip 10.0.0.210        # optional: the C2 box; default is red01
red.c2 off                  # opt out of Mythic/C2 (instead of the red.c2* values)
red.disk_gb 60
red01_vmid <vmid>           # else derived one golden stride past the golden band
red01_ip 10.0.0.245         # else picked from the red band at deploy time
red_yaml red.yaml           # a nested block (ladder/tiers) as YAML, if you prefer
```

`red_yaml` is merged first and dotted `red.*` keys win over it.

`red/tlaloc_ops.validate_red_block()` checks the block at deploy time and the
deploy REFUSES to plant red when it is invalid (unknown mechanism in a ladder
tier, unsorted tiers, `min_standing` below 1, a bad `assume_breach`): a
malformed ROE must fail before T0, not mid-event.

## The phase-7 sequence

`deploy_lib/phases/final.py` calls `red/tlaloc_ops.plant_assume_breach()` before
the `tz-ready` snapshot, so the restore point every box carries is already
compromised:

1. **render** `competitions/<id>/.tlaloc-config.yaml` from the Compfile red block
   (the file red01 will read),
2. **provision red01** (`red/red01_ops.py`): clone from `base-ubuntu24.04-fix` on
   this run's reserved vmid, attach the management NIC and the run's private
   red-bridge NIC, pin the interfaces with netplan (cloud-init networking is
   disabled on the box), grow the disk to `red.disk_gb` (default 60G), and stamp
   the run's ownership tags. An occupied vmid is a refusal naming the owner, never
   a silent reuse,
3. **install Mythic** with Gaia unless `red.c2 off` (on red01, or on `red.c2_ip`)
   and merge the agent's `c2.*` block into the rendered config; a competition
   with no callback port fails here,
4. **install the engine's red rules** (`range-red-firewall.service` + `.timer`,
   re-asserted every 30s): the routed FORWARD trio, the per-team-gateway C2 DNAT
   to `10.200.0.10:<port>`, and the nat ACCEPT that exempts the C2 flow from team
   SNAT. The FORWARD trio is installed even with `red.c2 off` (red01 must reach
   the teams); only the DNAT is skipped, because there is no callback port,
5. **freeze payloads** (`tlaloc c2-prep`): build the payload matrix against the
   competition's Mythic operation and freeze it into
   `competitions/<id>/red/payloads/` with a `uuid → box` manifest. Builds never
   run mid-event,
6. **stage red01**: push the tlaloc package to `/opt/tlaloc` (staged, never
   pip-installed — the systemd unit is what puts it on the import path) and the
   competition's nakon intel to `/var/lib/tlaloc/intel`,
7. **push the config** to `/etc/tlaloc/config.yaml` (0600), push the C2/LLM env
   files (0600) if the Mythic step or the LLM key produced one, and **verify the
   per-field contract**: the pushed file is fetched back and diffed against
   `tlaloc verify-config --print-contract` — a field the deploy meant to set but
   the push dropped is a feature that is dead on the VM while every local test
   stays green,
8. **install `tlaloc.service`** (enabled, not started — the seed runs explicitly),
9. **seed** (`tlaloc seed --comp /var/lib/tlaloc/intel`): access → escalation →
   C2 channel → persistence + one evasion touch per box, at the depth the config
   declares, writing `world.meta["seed"]` and the seed manifest,
10. **pull the seed manifest** to `competitions/<id>/red/manifest.json`.

Every failure here aborts the deploy: a range with no red team is not a range.

## red01 layout

```
/opt/tlaloc/            the staged package (+ systemd/ + config.example.yaml)
/etc/tlaloc/config.yaml 0600, pushed by the deploy
/etc/tlaloc/env         optional 0600 KEY=VALUE (TLALOC_LLM_API_KEY)
/etc/tlaloc/c2.env      0600 Mythic password for the agent (TLALOC_MYTHIC_PASSWORD)
/var/lib/tlaloc/        state + audit + reports (per competition: fresh dir)
  intel/                the pushed nakon facts (`seed --comp` reads this)
  world.json            belief state (0600, schema+comp stamped)
  events.jsonl          the audit log: every decision with its bundle
  KILL                  the kill switch (create it to stop red mid-cycle)
  report.md             secrets-free after-action
  report-secrets.md     organizer-facing 0600 (rotations, keys)
  restore-table.json    the undo table (verdict + organizer)
```

Manual control on red01: `systemctl stop tlaloc` (clean stop) or
`touch /var/lib/tlaloc/KILL` (the gate refuses every command from the next one
on, logged with its reason).

## Operating a run

```bash
python3 -m tlaloc --config /etc/tlaloc/config.yaml status     # beliefs, ladder tier, C2 callbacks
python3 -m tlaloc --config /etc/tlaloc/config.yaml candidates # the bounded option list
python3 -m tlaloc --config /etc/tlaloc/config.yaml decide     # one decision, no action
python3 -m tlaloc --config /etc/tlaloc/config.yaml report     # write the artifacts now
python3 -m tlaloc --config /etc/tlaloc/config.yaml restore-table --commands
python3 -m tlaloc --config /etc/tlaloc/config.yaml verify-config --pushed /etc/tlaloc/config.yaml
```

## Verdict and teardown

- **Evidence first.** Teardown runs `tlaloc report` on red01, then pulls
  `/var/lib/tlaloc/` into `competitions/<id>/red/`: `report.md`,
  `report-secrets.md` (0600), `restore-table.json`, `world.json`, `events.jsonl`
  and the seed `manifest.json`. `tlaloc_ops.write_verdict()` derives
  `red/verdict.json`: the red-side facts (planted, taken down, undo recipes) that
  the dashboard and the after-action read.
- **Coverage** (per scored pin, DOWN→UP proof) is the ENGINE's job: it reads the
  scoreboard, never the agent's own report. tlaloc's verdict deliberately does
  not claim coverage.
- **Teardown**: `tlaloc_ops.destroy_tlaloc_red()` stops `tlaloc.service` on
  red01 (ending the event clock and flushing the audit log), removes the engine's
  red rules, and destroys the VM through the run's ownership tags. A vmid that is
  not this run's is refused, not deleted; skipping this step leaves the VM, its
  config, its key and its DNAT rules standing.

## Scrim checks

One scrim on a copy of a working competition proves the live path. Grade:

- pressure + adaptivity (the decision log's `source` mix and the impact ledger are
  the numbers; `restore-table.json`'s summary has them),
- zero guardrail incidents (`guardrail_halt` absent from `events.jsonl`),
- verdict + restore table complete: every impact row has a restore row,
- the re-plant loop actually fired (dead artifacts observed, `replant` candidates
  in the log),
- the C2 DNAT survives a Docker restart on the engine (the 30s timer re-asserts
  it).
