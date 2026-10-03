# Scale8 remediation — action plan v2 (2026-10-03)

Supersedes the working plan at
`~/dev/dawgsec/scrim-runs/scale8-scrim-2026-10-01/REMEDIATION-PLAN.md`, which is a faithful copy of
the plan as handed over. Source of truth for the failures remains
[REPORT.md](../../../scrim-runs/scale8-scrim-2026-10-01/REPORT.md) (soak, 2026-10-02).

This version exists because the handed-over plan was verified line-by-line against `main` and the run
artifacts. It keeps the workstream skeleton (A–F), **drops three items that are already done**,
**corrects two root causes**, and **adds four defects the original missed**. Two hard constraints
discovered during verification reorder the whole schedule.

---

## 0. Verified corrections to the handed-over plan

Everything below was checked against the code and the run's own artifacts — not taken from the
report. "Correction" means the original plan would have spent time, or shipped a change, that the
evidence does not support. Each row names the artifact that settles it, so any row can be re-checked
in one command.

| # | Handed-over claim | Verified reality | Consequence |
|---|---|---|---|
| 1 | "commit 6b55d76 merged the six §9 fixes" | **6b55d76 adds one file** (`competitions/scale8-scrim-2026-10-01/packet.md`). The fixes landed in `0791b3a` (jump debian, multinode headroom, `enumerate_targets`), `fcca1ec` (fire-test gate), `4957e35` (observe-soak login) — all contained in `main` | Keep "landed" status; fix the provenance note so the next reader doesn't chase 6b55d76 |
| 2 | jump `configure_jump()` must persist `ip route add 10.200.0.0/24 via <engine>` | **Already true by construction.** bad-auto's routed mode puts the segment gateway on the engine (`ensure_segment_addr`, `badauto/deploy/engine_nat.py:165-193`) and installs `FORWARD -s <red_subnet> -d 192.168.0.0/16 -j ACCEPT` + a return accept + a DROP-toward-red (`firewall_script`, `engine_nat.py:95-140`). red01 already routes the teams via that gateway | **Delete the item.** The engine needs no change |
| 3 | same, for the jump: "persist a route on the jump" | **Unnecessary.** Return traffic is de-SNAT'd by conntrack to red's real source, and the jump already accepts `RELATED,ESTABLISHED` (`jump_ops.py:55`) | **Delete the item** |
| 4 | "`proxmox_api` passes no timeout" / "add default timeouts so no wait-loop deadline can be bypassed by a blocking call" | **`proxmox_api_for` already passes `timeout=60`** (`range_ops.py:120`); `guest_agent_exec_*` additionally bound each poll (`range_ops.py:214-257`) | **Rewrite.** The real defect is different — see #5 |
| 5 | "a phase-6 resume hung ~40 min in `wait_for_windows_sshd` because the deadline is only checked between unbounded agent calls" | The unbounded call is **`wait_for_guest_agent` inside `_wait_for_windows_setup_complete`**, which is passed the *entire remaining* deadline (`windows_ops.py:35`: `timeout=max(1, int(deadline - time.time()))`) against a 900 s default. `wait_for_windows_sshd` itself is bounded at 180 s (`windows_ops.py:151`) | **Re-target:** cap the nested agent wait at the enclosing budget, and give the four domain-chain helpers (`wait_for_windows_sshd`, `wait_for_dc_dns`, `wait_for_adws`, `_dc_promoted`) one shared per-call cap + shared deadline |
| 6 | "blue threads outlived DONE — `stage_run` must join with a timeout, `stop.set()` in a `finally`" | **Already implemented** in `61e7ed1` (2026-10-01 23:43), i.e. *before* the soak teardown: `supervise_workers` sets `stop` and joins against one deadline (`run-agent-scrim.py:1744-1766`), `join_workers` returns the stragglers, `blue_feed_loop` honours `stop` | **Downgrade** from "fix" to "verify on the next live run + surface the join-exceeded stragglers"; D's real content is #7 |
| 7 | — (not in the plan) | **New defect.** `stage_teardown` runs `badauto destroy` with `check=False` and never passes `--red-vmid` (`run-agent-scrim.py:1941-1955`), so the red VM teardown silently no-ops and cannot fail the harness | **This is D's actual fix** |
| 8 | — | **New defect.** Injects were written where the scorer does not look: teams produced `sub.md`, `sub7.md`…`sub12.md` at the team root; the scorer only counts `sub-*.md` or files under `submissions/` (`scrim-report.py:372-375`). Result: `INTERACTION.md` says "injects submitted: **0**" and FAILs the gate, while REPORT §8 claims 88 | **Add to E.** The blue gate is being failed by a path mismatch, not by agent behaviour |
| 9 | "red ... cred_sprays + 2 db_attacks vs 225–228 failed ... engine-node teams were never targeted at all" | Attacks targeted `192.168.225-227` only (teams 5–7) — **no engine-node team was ever attacked**, yet `world.json` enumerated **all 40 boxes** (teams 1–8) and the tactic's own service map covers team1–8, with `focus_team: team3` at capture | **Promote A2 from "investigate" to a first-class defect** with a reproduced target list to work from |
| 10 | acceptance gate: "blue availability ≥ 95% on every team" | Under the comp's own scoring model a legitimately-broken T0 service costs 12.5 pp for the whole run; the soak's best team scored 94.8%. The canonical gates are `docs/rehearsal-gates.md` (blue: restorations ≥ 2, injects ≥ 2, cycles rc=0, notebook ≥ 10) | **Replace** the 95% figure with the rehearsal gates + a "no phantom down-window" check, and say what the scoreboard is allowed to read at T0 |

**Also worth knowing before scheduling:** `jump_rules` is pinned by an exact-string golden at
`tests/test_multinode.py:282-307`, and the golden covers only the 2-team/1-jump topology. Any A change
must re-capture that golden *and* add a red-segment case, or the change is untested.

---

## 1. Constraints that reorder everything

1. **`.193` is down.** Verified 2026-10-03 01:3x: `10.0.0.193:8006` → no answer, ICMP 100% loss.
   `.150` answers (`:8006` → 200). Half the soak topology (engine node, slot-0 goldens 2050–2054,
   engine template 2040, ids 221–224) is therefore **unavailable**, and every two-node validation in
   the handed-over plan is blocked. This is why the plan is split into an offline wave and a
   live wave instead of one sequence.
2. **The main tree is being actively edited by another session.** `artifacts_ops.py`,
   `tests/test_artifacts_ops.py`, `tests/test_guest_file_read.py` are untracked and grew while this
   plan was written (mtimes 01:32–01:34); `range_ops.py`, `config_ops.py`, `packet_ops.py`, `utils.py`
   and `terraform/*.tf` are modified (an in-path-firewall feature). Full suite currently reads
   **7 failed / 810 passed**, all 7 in the untracked `tests/test_artifacts_ops.py`; the tracked
   baseline is **763 passed / 92 subtests / exit 0** and that is the number to compare against.
   → **All hardening work happens in a new worktree cut off `main`.** Never this tree.
3. **Live validation is a practice run.** Per AGENTS.md it must run from its own worktree, with its
   own run id, and be torn down with `destroy-competition.py`.

---

## 2. The plan I will act on

Effort estimates stay in the original's units (focused engineer-days). "Wave" is the execution order
given the constraints above, not a priority ordering.

**Wave 1 is complete** — landed on branch `scale8-hardening` (worktree
`.worktrees/scale8-hardening/`, cut off `main`) as eight commits, with the offline suite going from
**763 passed / 92 subtests** at the base to **847 passed / 92 subtests**:

| Commit | Item | What it does |
|---|---|---|
| `6eb4c39` | A1 | `TEZ_RED_SEGMENT`: jump accepts red→local teams, accepts replies, SNATs red to the team gateway ahead of the egress MASQUERADE; MSS clamped on the routed hop |
| `4765916` | A3 | `verify --red-teams all` + a pre-T0 gate in `stage_red` that fails the run when red cannot dial every team |
| `f20d584` | C1 | `guard_resume_existence` (engine > boxes > sweep+SSH) and a `checkpoint()` that will not stamp a claim the range cannot back |
| `6416b4d` | C2 | `strict=False` still raises when no machine ran a single step |
| `12d2fd5` | C3 | every nested agent wait bounded by the poll cap and the enclosing deadline; expiry says which probe failed |
| `4ed2daa` | D1 | teardown fails on a non-zero destroy rc and on a red01 that survived it |
| `2c2fc61` | E3+E4 | `services_to_rows` uses the newest round that has checks; inject counting matches `sub*.md` and the prompt writes where the counter looks |
| `c764265` | F1+F2 | duplicate `token_env` rejected; routed-red, phase-budget, endpoint-count and .150-RAM docs |

Two things changed during implementation, both recorded rather than smoothed over:

- **A1's golden is re-captured, not preserved.** The original plan said an empty `red_segment` would
  be byte-identical to today's rules. The MSS clamp is deliberately unconditional (a routed hop is
  what it exists for), so every topology's golden changed. The structural assertions —
  team↔team isolation, `:FORWARD DROP`, and the red-rule ordering — are pinned alongside it.
- **E3 has no supporting evidence in the soak's own data.** Every `final-services-team*.json` in the
  run directory carries full check arrays in all ten rounds, so the fix is not proven against that
  run; it is the class the report described, pinned by a test that reproduces the mid-round payload
  shape Quotient emits. Wave 3's monotonicity check is what would confirm it live.

### Wave 1 — offline, no node required (start immediately, in a new worktree)

| # | Item | Files | Test (offline) |
|---|---|---|---|
| **A1** | `jump_rules(..., red_segment="")`: when non-empty, add per-team `FORWARD -s <red> -d 192.168.<t>.0/24 -j ACCEPT` and `POSTROUTING -s <red> -d 192.168.<t>.0/24 -j SNAT --to-source 192.168.<t>.1`. Empty ⇒ byte-identical output to today (single-node and red-less comps unchanged). Also add `TCPMSS --clamp-mss-to-pmtu` for the engine↔team and red↔team paths — cheap defence for a routed hop that failed on exactly the protocols that open fresh TCP | `jump_ops.py`, `deploy_phases.py` (wire the segment), `nodes_ops.py` if the segment is config | Re-capture the `test_multinode.py` golden for the empty case; **add** a red-segment golden + a structural assertion that team→team stays unreachable |
| **A3** | `verify-competition.py --red-teams all`: for every team, red01 reaches one box. Fail closed (SKIP ≠ PASS, matching the existing gate discipline), one TCP/SSH probe per team, reporting per team | `verify-competition.py` | New gate test in the `test_verify_gates.py` idiom (`FakeSession`), plus a "one team unreachable ⇒ FAIL" case |
| **C1** | Resume existence gates in the resume path: `from_phase > 4` requires every `team_box` in terraform state to resolve to a live VM; `from_phase > 5` additionally requires `.postclone-swept` + ≥1 box SSH-reachable. Missing ⇒ hard error naming the exact remedy (`--from-phase 4`). `checkpoint(N)` refuses to stamp when its own gate would fail | `deploy.py`, `deploy_phases.py` | New `tests/test_resume_existence.py`. Also close the repo's biggest offline gap: `read_terraform_ctx` (`ssh_ops.py:76-88`) has **no test at all** — fake `terraform` on `PATH`, following `test_run_terraform.py` |
| **C2** | `strict=False` floor: `run_nakon(strict=False)` still raises when the pattern is "nothing answered" (0 reachable machines, or 100% connect-phase failures) | `nakon_ops.py` | New test: 2-flaky-of-41 ⇒ tolerated; 32-of-32 connect failures ⇒ raises, and asserts the message distinguishes the two |
| **C3** | Timeout honesty (revised from the original): cap the agent wait inside `_wait_for_windows_setup_complete` at `min(remaining, CAP)` instead of the whole deadline; give the four domain-chain waits one shared per-call cap and one shared deadline; on expiry, say which call was in flight | `windows_ops.py`, `range_ops.py` | New behavioural tests for `wait_for_windows_sshd` / `_wait_for_windows_setup_complete` (currently patch-only seams): a hung agent must terminate at the cap, and the diagnostics must name the call |
| **D1** | `stage_teardown`: pass `--red-vmid`, check the destroy's rc, then assert the vmid is gone (`vm_status`) and raise if not; keep red-destroy and range-destroy serialized and surface both rc's | `run-agent-scrim.py` | Extend `tests/test_scrim_defects.py` with a fake destroy that leaves the VM behind ⇒ harness must exit non-zero |
| **E3** | `services_to_rows` (`run-agent-scrim.py:769-779`) falls back to the newest round **that actually has checks** instead of `Last10Rounds[0]` blindly | `run-agent-scrim.py` | New test: in-flight round with empty `Checks` ⇒ row reflects the last completed round, not a phantom DOWN |
| **E4** | Inject counting: count submissions where the teams actually write them (`sub*.md` at the team root), and/or have the prompt write `sub-<injectId>.md`. Prefer both — the scorer must not depend on an agent's filename choice | `scrim-report.py`, `run-agent-scrim.py` (prompt) | New test: team-root `sub7.md` counts; `submissions/` still counts |
| **F1** | `load_nodes_config` rejects duplicate `token_env` (today it only rejects duplicate names — `nodes_ops.py:125-127`); document the contract in `docs/multi-node.md` | `nodes_ops.py`, docs | Extend `LoadNodesConfigTest` |
| **F2** | Docs: phase-budget table (REPORT §2.2) into `docs/multi-node.md`; ≥6-team blue-endpoint spreading into `docs/scrim-harness.md`; `.150` RAM ceiling and the 4-team guidance into `docs/environment-facts.md` | docs | `test_secret_hygiene.py` only if a new env var appears |

### Wave 2 — live on `.150` alone (allowed now; no `.193` dependency)

| # | Item | Why it can run on one node |
|---|---|---|
| **B0** | **Agent-wedge diagnosis.** Single-team lab comp on `.150` (goldens exist), promote the DC, capture at each stage: `agent/ping` vs `agent/exec`, console via `vncproxy`, qemu-ga service state, Windows event log; then freeze the wedge and test whether *other* agent RPCs still answer | Needs one Windows DC and one host — does not need the two-node topology |
| **B0+** | **Add the hypothesis the original ranked last and the evidence favours:** after promotion the box moves to the **Domain** network profile, and qemu-ga's firewall rules are normally scoped *per profile at install time* (`netsh advfirewall firewall add rule ... profile=domain,private`). A profile switch silently makes them inapplicable — exec (4701–4703) dies while `agent/ping` (virtio-serial) still answers, and a reboot does not help because the profile persists | Explains all three observations (ping ok, exec 500, survives power-cycle), and is checkable in minutes with `Get-NetFirewallProfile` + `Get-NetFirewallRule | ? DisplayName -match qemu` |
| **B1** | **Domain chain off the agent's critical path.** `wait_for_windows_sshd`, `wait_for_dc_dns`, `wait_for_adws`, `_dc_promoted` all funnel through `guest_agent_exec_windows` — one channel, so one wedge stalls the chain (`windows_ops.py:129-192`, `domain_ops.py:41-55`). Give each an SSH-via-engine equivalent and keep the agent as fallback | Pure code + one live comp to prove the SSH path works on a promoted box |
| **E1** | Quotient DNS checker parser: engine-node `app01-dns` answered `localhost → [127.0.0.1]` yet the checker reported `no records received`. Get `/opt/quotient` from any live engine, fix the matcher, PR upstream, record in `docs/upstream-defects-handoff.md` | Any live engine |
| **E2** | nakon catalog: grant `'user'@'%'` (plus `skip_name_resolve`) so satellite db checks arriving SNAT'd as `_gateway` match. Upstream PR — affects every satellite db pin | Catalog-side, one comp to confirm |
| **A2** | Red targeting blind spot: instrument per-team reachability at focus rotation, then explain why 40 targets were enumerated but only 225–227 were ever attacked (focus rotation and `credlist_gate_min`/`credlist_max_per_team` are the first suspects — `run-agent-scrim.py:1655-1660`) | Runs on `.150`; the instrumentation is the deliverable |

### Wave 3 — two-node validation (blocked on `.193` returning)

| # | Item |
|---|---|
| **A-valid** | `verify --red-identity --red-teams all` PASS on a two-node comp; a live cred_spray lands ≥1 foothold on a satellite box with `10.200.0.10` visible in the box's auth log, and in red's `events.jsonl` |
| **B-valid** | 8-team rerun: teams 5–8 complete the full domain chain, `verify`'s `domains` gate passes 8/8 with unique DomainSIDs |
| **C-valid** | Live exercise: deploy 2 teams, `terraform destroy` the team resources, attempt `--from-phase 5` ⇒ must refuse with the `--from-phase 4` remedy |
| **D-valid** | 2-team scrim: teardown exits clean, zero leftover VMs via the tag sweep, rc≠0 surfaces if any destroy fails |
| **E-valid** | 8×8 board reads UP at T0, monitor snapshots monotonic (no phantom down-windows), `INTERACTION.md` injections counted |

### Wave 4 — milestone

Full 8-team rerun from a fresh worktree with every gate armed, deploy budget 5 h + run 2 h.

---

## 3. Acceptance gates (revised)

Replace the two unsupported figures from the handed-over plan and keep the rest:

1. `verify` passes `domains` for 8/8 teams (unique DomainSIDs ×8).
2. `verify --red-identity --red-teams all` PASS.
3. Red lands ≥1 foothold on **each node's** teams; `docs/rehearsal-gates.md` red gates met
   (takedowns ≥ 6, restore-reactions ≥ 3, distinct tactics ≥ 4, Windows footholds ≥ 1).
4. Blue meets `docs/rehearsal-gates.md` (restorations ≥ 2, injects ≥ 2), **and** no down-window in
   `scoreboard-state.jsonl` is contradicted by the T0 baseline (the phantom-down check).
5. Teardown: zero leftover VMs, driver exits clean, both destroys rc=0, red01 gone.
6. The deploy from a fresh worktree completes with no manual intervention (run 1 needed 6 attempts).

---

## 4. What I need decided before Wave 2

Wave 1 has no dependency on anything below. Waves 2–4 assume the `.150` co-tenancy and the sibling
in-path-firewall work can proceed in parallel; if the other session is about to deploy against `.150`,
the B0 lab comp and the A2/E validation runs must be scheduled around it.
