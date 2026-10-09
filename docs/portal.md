# Student portal: Quotient login → Proxmox web console

Blue teams log in with their **Quotient team credentials** and get a **browser console** (the VM's
own screen, through Proxmox) on each of their team's boxes. It is opt-in per competition, deployed
on the scoring engine, and non-fatal: a portal that cannot start is recorded as a degradation and
the range carries on.

> **Status (2026-10-08): first live run passed.** `portal-live-2026-10-08` on cyberfield .193
> (2 teams × Ubuntu 20.04 + Windows Server 2019): real consoles on every box in a real browser,
> typed input reaching the guest, and a full `verify-competition.py` PASS. Not yet exercised: the
> Cloudflare tunnel, event-size concurrency, rebuild ACLs, multi-node, and teardown revocation
> ([known-issues.md](known-issues.md#student-portal-what-the-first-live-run-did-not-cover)).

## Turning it on

Compfile keys (all optional except `portal`):

| Key | Default | Effect |
|---|---|---|
| `portal 1` | `0` | Deploy the portal at the end of phase 8 |
| `portal_firewall_console 0` | `1` | Hide the in-path firewall's console from teams (they own their pfSense by default) |
| `portal_scoreboard_url <url>` | `http://<engine>` | The scoreboard link the portal shows (set it to the public scoreboard URL) |
| `portal_hostname <host>` | — | Printed in the competitor packet's *Web console* section (packet text only) |

Env vars (put the tunnel pair **only in the event's env file**, never in a practice worktree's):

| Variable | Effect |
|---|---|
| `TEZ_PORTAL_TUNNEL_TOKEN` | Cloudflare tunnel token. Without it the portal is reachable only through `ssh -L` |
| `TEZ_PORTAL_HOSTNAME` | The tunnel's stable public hostname (e.g. `range.dawgsec.com`), used for the ownership check |
| `TEZ_PORTAL_OPEN=1` | Start with the gate **open** (practice runs). The default is closed |

## How it works

```
student ─HTTPS─► Cloudflare (stable hostname) ─tunnel─► cloudflared ─┐      engine VM
                                                                     ▼      /opt/tez-portal
     portal (FastAPI/uvicorn; published 127.0.0.1:8443 only)
      ├─ reads /opt/quotient/config/event.conf (ro) — the logins
      ├─ reads portal.json (ro)                    — boxes, nodes, console tokens
      ├─ state/access.json, state/access.log       — the gate and the audit trail
      └─ WS /console/ws/<one-shot id> ── relay ──► wss://<node>:8006/…/vncwebsocket
```

| Piece | Where |
|---|---|
| Service (auth, broker, routes) | `portal/` (`app.py`, `auth.py`, `console.py`, `Dockerfile`, `compose.yaml`) |
| UI | `portal/frontend/`: React + Vite + Tailwind, the management UI's stack and design (see [Frontend](#frontend)) |
| Deploy side (config, push, compose, probe, teardown) | `portal_ops.py` |
| Per-comp console token | `pve_console_ops.py` |
| Phase hook | end of `deploy_lib/phases/seed.py` (phase 8) |
| Verify gate | `verifier/portal.py` (gate name `portal`) |
| Preflight (advisory) | `preflight/portal.py` |
| Operator CLI | `portal-access.py` |

### Logins never touch Quotient

The repo's working rule is that Quotient allows **one session per account** (`quotient/setup.py`
`_admin_accounts`). Under that rule, a portal that validated by logging into Quotient as `team1`
would log team1 out of the scoreboard. The 2026-10-08 live run could not reproduce the eviction
([known-issues.md](known-issues.md#quotient-did-not-enforce-one-session-per-account-on-2026-10-08)),
but the design holds either way: the portal adds no Quotient sessions and doesn't need Quotient
up to let a team in. Instead the portal reads the same `event.conf` Quotient reads, which is bind-mounted read-only, and compares
against its `[[team]]` and `[[admin]]` entries. It re-reads the file when its mtime changes, so a
team password rotated in Quotient's config takes effect without a restart. The bind mount follows
`push_event_conf`'s in-place `tee` writes.

Roles:
- **team**: its own boxes only.
- **admin** (white team): every team's boxes, plus the gate.
- `scoring` (automation) and `inject` are refused.

Students may type a display name at login. Accounts are shared per team, so that name is the only
per-student attribution, and it appears in the access log.

### The isolation argument

The console token can open a console on **every team VM of this competition**, so isolation is
enforced by the portal, server-side:

- **Box lookup comes from the session.** `POST /api/console` resolves the box from the session's
  team. A team session that names another team gets 403. A box name that only exists on another
  team gets the same 403 as one that doesn't exist, so a team learns nothing about other lineups.
- **Relay ids are single-use and bound to the minting session.** A leaked id is burned by its first
  use, whoever makes it.
- **The browser never holds the token.** It gets the relay id and the VNC ticket, which noVNC uses
  as the RFB password. The ticket is useless without the relay.
- **Nothing is reachable from team subnets.** The portal listens on `127.0.0.1` only. Team boxes
  can reach the engine at `192.168.<id>.1`, which is why nothing is bound wider. cloudflared dials
  out.

Tests pin all of this, including the cross-team refusals (`tests/test_portal_app.py`).

### The console token (per competition, console-only)

`pve_console_ops` mints one set per node the teams live on:

| Object | Value |
|---|---|
| User | `tezcon-<comp>-<run_id>@pve` |
| Token | `<user>!portal`, `privsep=0` (the token is exactly the user's rights) |
| Role | `TezConsole` = `VM.Console` only. Shared across competitions and never deleted |
| ACLs | `/vms/<vmid>` → that user, one per team VM of **this** competition |

What the token can do is what students already get: open a console on this comp's team VMs. It
cannot power-cycle, read configs, or reach any other VM.

Lifecycle:
- **Storage.** The secret is carried in `.deploy_state.json` (`portal_console_tokens`), so a resume
  reuses it. PVE only reveals a secret at creation, so a vanished token is re-created.
- **Permissions.** Minting needs `User.Modify`, `Permissions.Modify`, `Realm.AllocateUser` and
  `Sys.Modify` (the last only for the first `TezConsole` creation on a node). The cyberrange .150
  deploy token (`terraform@pam!comp-automation`) held all four on 2026-10-08. The preflight warns
  when a node's token lacks them, and the portal then runs with logins and box lists but no
  consoles there.
- **Teardown.** `destroy-competition.py` deletes exactly `tezcon-<comp>-<run_id>@pve` on every
  node, before the VMs go. It matches the exact name, never a prefix
  ([AGENTS.md](../AGENTS.md): an over-broad sweep once destroyed other comps). An absent user
  counts as clean, so re-running the destroy converges.

### The gate

Team consoles answer **423** until an admin opens access. Team logins and box lists work
throughout, so teams can sign in during the briefing. Admins always bypass the gate. Flip it from
the white-team view, or:

```bash
python3 portal-access.py <comp> status | open | close
```

`state/access.json` survives a re-push (phase 8 re-runs, engine-recovery re-seed). A re-push restarts the portal container, because it reads portal.json once at startup, so open consoles drop and need a reconnect.

### The stable hostname has one owner

Two cloudflared connectors on one tunnel token would split students between two ranges at random.
Before starting cloudflared, phase 8 fetches `https://$TEZ_PORTAL_HOSTNAME/healthz` (which returns
`{comp, run_id, open}`):

| Answer | Action |
|---|---|
| Nobody: offline, error page, or not our JSON | Start cloudflared |
| This run | Start it again |
| Another comp or run | Skip the tunnel, record a degradation, and stop any cloudflared this comp left behind. The portal stays reachable through `ssh -L` only |

**One-time Cloudflare setup** (by hand; the repo only consumes the token):
1. Create a remotely-managed tunnel in Zero Trust.
2. Add a public hostname → service **`http://portal:8000`**. cloudflared runs in the portal's
   compose network, so the service name resolves.
3. Put the tunnel token and hostname in the event env file as `TEZ_PORTAL_TUNNEL_TOKEN` and
   `TEZ_PORTAL_HOSTNAME`.

## Operating it

**Reaching the portal as an operator**, with or without a tunnel:

```bash
ssh -i proxmox -L 8443:127.0.0.1:8443 <vm_user>@<engine_ip>    # then http://127.0.0.1:8443
```

**Output and logs**
- `credentials.txt` gets a `Portal:` line once it is up.
- `/opt/tez-portal/state/access.log` records logins, console opens/closes and gate flips.
- Teardown copies it to `competitions/<id>/portal-access.log`, and artifact collection carries it
  into `.automated-tests/<run-id>/`.

**After a box rebuild** (`redeploy-competition.py` re-clones a VM): Proxmox's destroy path removes
that VM's ACL entries (not yet observed here), so re-apply them:

```bash
python3 portal-access.py <comp> resync
```

The PUTs are idempotent. If it says a token was re-created, re-run
`create-competition.py --competition <comp> --from-phase 8 --yes` so the engine's portal.json picks
up the new secret.

**After engine recovery**: `redeploy_engine_ops` clears `portal_up`. Its advertised re-seed
(`create-competition.py … --from-phase 7`) re-runs phase 8, which re-ships the portal with the
recorded tokens.

**No console token on a node** (the preflight or phase 8 says the deploy token lacks privileges):
re-run phase 8 with a token that has them, e.g. a one-off
`TF_VAR_proxmox_api_token=<privileged token> python3 create-competition.py --competition <comp>
--from-phase 8 --yes`. Phase 8's other steps are flag-guarded, so they skip, and only the portal
work runs again.

## Limits

- **No clipboard.** QEMU's VGA console has no guest clipboard. The console page's *Type text…*
  button sends text as keystrokes (ASCII, paced), which suits commands but not large files.
- **RFB 3.3 cap.** PVE's `vncwebsocket` only completed an RFB 3.3 handshake when the pfSense tool
  was built (2026-10-04). noVNC 1.6.0 has no public setting for its version cap, so
  `portal/frontend/src/pages/Console.jsx` lowers the internal `_rfbMaxVersion` to 3.3 right after
  constructing the client. A noVNC upgrade must re-check that field still exists.
- **The engine healthcheck does not watch the portal.** `range-healthcheck.sh` is baked into the
  engine template, and changing it would rebuild every competition's engine template for an opt-in
  feature. The container has its own Docker healthcheck and `restart: unless-stopped` instead.

## Frontend

`portal/frontend/` is the management UI's stack (React 19, Vite, Tailwind 4) and design. It is
not a look-alike: `src/components/ui.jsx` and `kit.jsx` are verbatim copies of
`webui/frontend/src/components/`, and `src/index.css` is webui's theme file verbatim (minus the
typography plugin) followed by portal-only rules. They are copies rather than cross-tree imports
because the image is built on the engine from `portal/` alone. `tests/test_portal_frontend.py`
fails when either side drifts, and also when the shared dependency versions drift.

- **Views.** `/` is sign-in, then either the team's boxes or the white-team view (gate, every
  team, access log). `/console/<team>/<box>` is one console per tab. The FastAPI app serves the
  build as an SPA, the same way `webui/server.py` does, and its errors carry `detail` like
  webui's.
- **noVNC** comes from the v1.6.0 **GitHub release tarball**, pinned with an integrity hash in
  `package-lock.json`. The npm package was not usable: its `lib/` build is CommonJS with a
  top-level `await` (`lib/util/browser.js`), which Vite rejects. The release source's ES modules
  (`core/`) build cleanly.
- **Build.** The image builds the UI itself (`portal/Dockerfile` stage 1: `npm ci` +
  `vite build`), so a deploy host needs no Node. Only the frontend source ships to the engine,
  never `node_modules/` or `dist/`.
- **Local development.** Run `cd portal/frontend && npm install && npm run dev`. Vite proxies
  `/api`, `/healthz` and the console websocket to `127.0.0.1:8443`, which is either a local
  `uvicorn` portal or a real engine's portal over `ssh -L 8443:127.0.0.1:8443`.

## Verification

**Live, 2026-10-08** (`portal-live-2026-10-08`, cyberfield .193, run `run-13c2ef80`):

- **Deploy.** Phase 8 shipped and built the portal in 70 s. The first attempt hit a stale
  in-process module after a mid-deploy code change and was recorded as a degradation, which
  proved the non-fatal path: the range still came up live. A `--from-phase 8` re-run fixed it.
- **Token scope on PVE.** User `tezcon-portal-live-2026-10-08-run-13c2ef80@pve` with exactly four
  ACLs (`/vms/1500,1501,1510,1511` → `TezConsole`), and `TezConsole` = `VM.Console` only.
- **Verify.** The `portal` gate passed every row, including a relayed `RFB 003.008` greeting from
  real PVE. Full `verify-competition.py`: **PASS**.
- **Real browser** (headless Chromium, real noVNC, through `ssh -L`), 12/12:
  - white team opened all four consoles; Windows showed its lock screen;
  - team1 saw the gate closed (423) and was refused team2's box (403);
  - the open page unlocked on its own when admin opened access;
  - on `web01`, *Type text…* logged in and ran `echo PORTAL-CONSOLE-OK $(hostname)`, and the
    guest printed `team1-web01`.
- **Quotient session survives a portal login.** A team1 Quotient session stayed valid (200 on
  `/api/teams`) after a team1 portal login. See the note above on why the control did not show
  an eviction either.


- `verify-competition.py` runs the `portal` gate when the Compfile enables it. It probes the
  engine's `127.0.0.1` listener in the same way cloudflared reaches it:
  - health;
  - a team login that sees exactly its own boxes;
  - the gate (423 while closed);
  - a cross-team console request refused (403);
  - an admin login;
  - per node, one console relayed far enough to read the `RFB 003.` greeting;
  - that relay id refused on reuse.
- Offline: `tests/test_portal_*.py` and `tests/test_pve_console_ops.py`.
- On 2026-10-08 the probe script was also run against a real uvicorn portal (with a fake PVE
  upstream). The image was built from the exact shipped bundle and smoke-run under podman, and
  every view was rendered in headless Chromium in both themes. None of that touched a Proxmox
  host.
