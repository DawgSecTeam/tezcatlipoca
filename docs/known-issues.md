### A pre-slug kept template cannot be adopted by a fresh deploy

Templates created before the comp-tag slug fix carry the display name split at its space
(`comp-CDE` + `2026` instead of `comp-cde-2026`). The collision preflight compares the
slugged comp tag, does not recognise them as this competition's own kept artifacts, and
refuses the slot ("collides with VMs ... golden vmid"), so a later deploy cannot reuse
them and only a full destroy reclaims the vmids. Either re-tag such templates once
(`comp-<slug>`) or destroy them; the reuse path itself is correct for post-fix templates.

### The dashboard's range page still hides teardown on a retired range with kept templates

`--teams-only` retires the range and keeps this competition's golden + engine templates for
its next run, which is the documented practice-run loop. The dashboard now resolves a `destroy`
on a retired range whose document records only kept templates and no live VMs
(`dashboard/ranges.py:reclaimable_templates`, wired into `runner.py`), and the engine's
ownership guard still scopes what is destroyed — so the API and
`destroy-competition.py --teams-only`/full paths can reclaim them. What remains is the web:
`Range.jsx` computes `blocked = retired || …`, so the page offers no action menu and an
operator cannot reach that resolver from the range view. Caught live before the backend fix:
a teams-only teardown left the engine template and its goldens in place and the follow-up
full teardown failed.
**Next action**: in `tezcatlipoca-dashboard`'s `web/src/pages/Range.jsx`, show the action menu
when `range.reclaimable_templates` is true (destroy only; verify/reset stay disabled) and
update `Range.test.jsx`'s retired-range case.

### A two-team scrim on one LLM endpoint loses every other blue cycle to the 600 s cap

`run-6b0732dc-run3` (scrim-one, 2 teams, 45 min, both blue teams on one deepseek-v4-pro
endpoint): 8 cycle starts, 4 completed rc=0 and 4 hit the per-cycle cap (2 per team), so the
`timeouts <= 2` gate failed and the verdict read NOT READY despite 38 interaction points
(27 takedowns, 17 restore-reactions, 7 simultaneous down on each team, 16 restorations). The
two teams serialise on the one LLM lock and a real incident cycle needs more than 600 s, so a
team's next cycle starts already behind. The gate's own expectation is one cut per team, but
each team here lost half its cycles.
**Next action**: decide the multi-team recipe deliberately — raise `TEZ_CYCLE_TIMEOUT` (e.g.
1200 s) for multi-team events and scale the `timeouts` gate with the team count, or accept that
one endpoint serves one team per window and run multi-team ranges with per-team endpoints.

### The boxes' DNS is a hardcoded public resolver, and this uplink filters it

`estate/apply.py` gives every box `nameserver: 8.8.8.8`; `common/utils.DNS_FIX_CMD` writes the
same into `resolv.conf` and `resolved.conf.d`. On this estate's uplink both 8.8.8.8 and 1.1.1.1
now time out from the host while the gateway resolver 10.0.0.1 answers, so a fresh engine cannot
resolve `download.docker.com`/`security.ubuntu.com` and the deploy dies at
`install docker compose v2 plugin` (`run-fee03a45`), and boxes lose the Ubuntu archive the same
way. The pair `1.1.1.1 8.8.8.8` covers a single filtered resolver, not a filtered *public* path.
**Next action**: make the box DNS a placement value (the range's own gateway/upstream resolver
first, public resolvers only as fallback), then deploy cde-2026 to prove resolution end to end.

### The template builders reach their scratch VM through node shell

`tools/build-{pfsense,vyos}-provision-template.py` alias `192.168.1.2` on the node's bridge and `ssh -W` through `root@<node>` to drive the scratch VM's bootstrap LAN (`192.168.1.0/24`); their ISO fetch (`download-url`) and template rename (`PUT .../config`) are API-only, leaving this hop and template sync ([multi-node.md](multi-node.md#template-copies)) as the repo's only node-shell uses. The pfSense agent retrofit cannot simply switch to the agent channel instead: installing that agent is what the hop is for. **Next action**: on a host whose shell is closed, either alias `192.168.1.2` on the *operator's* NIC where the operator shares the scratch VM's bridge L2, or console-type an operator-routable bootstrap address (`/monitor sendkey`, which these tools already drive the console with).

### A per-run workspace keeps disappearing, and no code in this repo deletes it

The original entry claimed the dashboard removes each run's workspace when the run ends, including
a run that FAILED and a destroy that ended `teardown_failed`, stranding the range: the journal,
the run id and the ownership set all live in that workspace, so deleting it removes the sanctioned
way to finish. **That premise does not reproduce.** There is no code in the dashboard that deletes
`<run_dir>/comp` — the only per-run deletion is the engine worktree — and on the ops host most
stored runs still have their workspace, including a `teardown_failed` range that still
carries its eight recorded resources. Of the four `teardown_failed` ids the entry named, two have
no store row at all and two have no run dir. So *something outside both repos* is removing run
directories, and until it is identified this cannot be fixed here.
**Fixed in passing**: the deletion site that does exist (`workspaces.release`) now holds the
workspace of any run whose range is not retired, reclaiming only bare debris.
**Next action**: identify what removes run dirs on the ops host (a janitor, a retention job, a
systemd timer) and make it retire the range through `POST /api/ranges/retire` before it deletes.

### The engine management IP is resolved but never selected

The placement redesign's second step selects one value the operator used to guess — red01's
address — but the engine's management address stops at *resolution*, not selection:
`deploy_lib/prepare._resolve_engine_mgmt_ip` takes the flag, then the node record, then the
profile, then the marked default (`DEFAULT_ENGINE_MGMT_IP`, documented as contested and poisoned
on a flat shared L2). On an estate where the default is unusable and every candidate is
unverifiable — co-tenant guests with no guest agent — selecting unattended would
trade a wrong answer for a guess, which is why it is still an explicit per-run input. Live runs
show the intended behaviour in the meantime: an explicit address proceeds with a named warning
and an unverifiable one refuses.
**Next action**: decide the rule deliberately (refuse and require a pin, or select from the
declared band with the agent-less guests named, as red01's address now does) and record the
decision next to the band constants.

### Planted Linux boxes deny all SSH at the PAM account stage after a restart

Root cause unsolved. A stop/start on a planted box can kill SSH preauth (`Access denied ... by PAM account configuration`) while services still listen. `redeploy --mode reset` recognizes the signature and escalates past it. The diagnostic kit now exists: `tools/pam-bisect-lab.py` + `common/pam_bundle.py`; `grab` captured all 13 sections from the live planted `web01` over the guest agent, and `redeploy --mode reset` now auto-captures the same bundle into the run's `evidence/pam-trap/` before escalating. Twenty live stop/start cycles (2026-10-18, lab-local known_hosts) did not reproduce the trap. The bundle now also captures the box clock and per-user aging (`chage -l`, `passwd -S`, and the shadow aging fields without the hash), since a boot-time clock/password-aging disagreement is the remaining plausible account-stage cause. **Not the `bad-perms-userConfig` misconfig**: a recent pass checked Linux-PAM master and `pam_unix`'s auth and account paths carry no `stat`/mode check on `/etc/shadow`, so a world-readable shadow cannot fail `pam_acct_mgmt` — do not reconnect the two without new evidence. **Next action**: analyze the next auto-captured bundle, or run `pam-bisect-lab.py cycle`/`bisect` against a planted lab until it reproduces. Root cause still open.

### Student portal: unproven paths

A single-node two-team, four-box acceptance run (`portal-smoke`) proved health/login/scoping, the closed gate (`423`), cross-team refusal (`403`), four truly concurrent relays (each `101` + `RFB 003.008`), ACL removal/reapply across a real box rebuild, and full teardown revocation (the exact `tezcon-<comp>-<engine-run-id>@pve` user and its ACLs gone). A second one-box run (`tiny-portal-smoke`) proved the **gate-open** path: `open: True`, team gate `HTTP 200`, and the console relay. A third one-box run proved the **public Cloudflare path** with a quick tunnel (`trycloudflare.com`): tunnel healthz `200`, tunnel team login `200`, and a tunnel console relay `wss 101` + `RFB 003.008`. Still unproven: the deploy-side **named-tunnel start** (`TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`, including the ownership decision) because the token is absent, and multi-node (the field node was down). **Next action**: run with the event's tunnel token/hostname and a two-node range.
