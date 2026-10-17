# Known issues

Temporary writepad, not a ledger: every entry has a concrete next action and is drained each run cycle.

## Open

### A retired range's kept templates cannot be reclaimed until the competition deploys again

`--teams-only` retires the range and keeps this competition's golden + engine templates for
its next run, which is the documented practice-run loop. But the retired range then blocks
the only path that can destroy them: `_range_workspace` answers a follow-up destroy with
`the range in <run> was torn down by <destroy run> — nothing to destroy`, and a full teardown
run resolves to that retired workspace and dies. Caught live: a teams-only teardown left the
engine template and its goldens in place, the follow-up full teardown failed, and the
templates were only reclaimed by deploying the competition again (which correctly adopted all
four) and then tearing *that* range down fully.
The templates are the whole point of teams-only, so this is the normal path, not an edge case.
**Next action**: let a full teardown act on a retired range's recorded template inventory when
that range has no live VMs left, or have the teams-only path record the kept templates
somewhere a later full teardown can find without a deploy.

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

Root cause unsolved. A stop/start on a planted box can kill SSH preauth (`Access denied ... by PAM account configuration`) while services still listen. `redeploy --mode reset` recognizes the signature and escalates past it. Unreproduced since it was confirmed (four stop/starts came back clean); the standing repro lab was destroyed. **Not the `bad-perms-userConfig` misconfig**: a recent pass checked Linux-PAM master and `pam_unix`'s auth and account paths carry no `stat`/mode check on `/etc/shadow`, so a world-readable shadow cannot fail `pam_acct_mgmt` — do not reconnect the two without new evidence. **Next action**: commit the diagnostic kit (`tools/pam-bisect-lab.py`) so the next occurrence can be bisected; it does not exist in the tree, so this is a from-scratch build.

### Scoring round loop does not auto-resume after an engine reboot

No unattended healing. The shared classifier now distinguishes a stale post-reboot pause from a deliberate one; `verify-competition.py --fix-round-loop` is the repair. **Next action**: a live reboot trial to validate the code path on a real engine.

### Fedora goldens cannot be built on SELinux-enforcing nodes

Mitigated, not closed. The qemu-guest-agent domain is confined on Fedora 44 (dnf/rpm/systemctl denied, `setenforce 0` denied); SSH is the escape hatch (cloud user lands `unconfined_t` with passwordless sudo). The template recipe now sets `SELINUX=permissive` before sealing, but only on rebuild — current node templates still enforce. **Next action**: rebuild the Fedora templates. Keep Fedora out of agent-only lineups until then.

### Student portal: unproven paths from the first live run

A first live run proved console access, the gate, and verify PASS. Still unproven: the Cloudflare tunnel path (no token used), `pveproxy` under event-size concurrency (only one console was open), box rebuilds (console ACL survival), multi-node, teardown revocation (range was left up). **Next action**: a run exercising each.

### Quotient "one session per account" not reproduced

Several modules assume a second login evicts the first cookie (the `scoring` second-admin account, a comment in `deploy_lib/context.py`, `usage-people.md`). On the portal's live engine, two logins as `team1` and separately as `admin` both left the first cookie valid. **Next action**: re-test before relying on the rule either way; the `scoring` account and the student portal are harmless regardless.

