# Student portal: Quotient login → Proxmox web console

Blue teams log in with their **Quotient team credentials** and get a **browser console** (the
VM's own screen, through Proxmox) on each of their team's boxes. Opt-in per competition, deployed
on the scoring engine, non-fatal to the deploy: a portal that can't start is recorded as a
degradation and the range carries on, but verify's `degradations` gate reports it as a failure.
Unproven paths: [known-issues.md](known-issues.md).

## Turning it on

Compfile keys (all optional except `portal`):

| Key | Default | Effect |
|---|---|---|
| `portal 1` | `0` | Deploy the portal at the end of phase 8 |
| `portal_firewall_console 0` | `1` | Hide the in-path firewall's console from teams (they own their pfSense by default) |
| `portal_scoreboard_url <url>` | `http://<engine>` | The scoreboard link the portal shows |
| `portal_hostname <host>` | — | Printed in the competitor packet's *Web console* section (packet text only) |

Env vars (the tunnel pair goes in the event's own env file, never a practice worktree's):

| Variable | Effect |
|---|---|
| `TEZ_PORTAL_TUNNEL_TOKEN` | Cloudflare tunnel token. Without it the portal is reachable only through `ssh -L` |
| `TEZ_PORTAL_HOSTNAME` | The tunnel's stable public hostname, used for the ownership check |
| `TEZ_PORTAL_OPEN=1` | Start with the gate open (practice runs). Default closed |

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
| UI | `portal/frontend/` (see [Frontend](#frontend)) |
| Deploy side | `portal/portal_ops.py` |
| Per-comp console token | `infra/console.py` |
| Phase hook | end of `deploy_lib/phases/seed.py` (phase 8) |
| Verify gate | `verifier/portal.py` (gate name `portal`) |
| Preflight (advisory) | `infra/gates.py` |
| Operator CLI | `portal-access.py` |

### Logins never touch Quotient

The portal adds no Quotient sessions and doesn't need Quotient up to let a team in — it reads the
same `event.conf` Quotient reads (bind-mounted read-only) and compares against its `[[team]]`/
`[[admin]]` entries, re-reading on mtime change so a rotated password takes effect without a
restart. Roles: **team** (its own boxes only), **admin**/white team (every team's boxes, plus the
gate). `scoring` and `inject` are refused.

Students may type a display name at login. Accounts are shared per team, so that name is the only
per-student attribution, recorded in the access log.

### The dashboard's VPN panel

When remote access is on ([remote-access.md](remote-access.md)), phase 8 folds the enrollment
into `portal.json` (`vpn`: the login server plus each team's per-person keys, read from
`remote-access/keys.json` and `TEZ_HEADSCALE_URL`), and `/api/me` hands a team session only its
own people — a team never sees another team's keys, like boxes. The team view shows a **VPN
setup** panel (install Tailscale, then each person's `tailscale up --login-server … --authkey …`
command) above the **VM access** console grid, and the top bar links both the Quotient
**Scoreboard** and its **Injects** page. With remote access off (or no `people.json` roster)
there is no `vpn` field and no VPN section.

### Isolation

The console token can open a console on every team VM of this competition, so isolation is
enforced server-side:

- Box lookup comes from the session: `POST /api/console` resolves the box from the session's
  team. A team naming another team gets 403, same as a nonexistent box, so a team learns nothing
  about other lineups.
- Relay ids are single-use and bound to the minting session; a leaked id is burned on first use.
- The browser never holds the token — only the relay id and a VNC ticket useless without the
  relay.
- The portal listens on `127.0.0.1` only; nothing is reachable from team subnets. cloudflared
  dials out.

Pinned by `tests/portal/test_portal_app.py`.

### The console token

`infra.console` mints one set per node the teams live on:

| Object | Value |
|---|---|
| User | `tezcon-<comp>-<run_id>@pve` |
| Token | `<user>!portal`, `privsep=0` |
| Role | `TezConsole` = `VM.Console` only. Shared across competitions, never deleted |
| ACLs | `/vms/<vmid>` → that user, one per team VM of this competition |

The token can only open a console on this comp's team VMs — no power-cycle, no config read, no
other VM.

- **Storage**: the secret lives in the state document (`run.portal_console_tokens`); PVE only reveals
  it at creation, so a vanished token is re-created.
- **Permissions**: minting needs `User.Modify`, `Permissions.Modify`, `Realm.AllocateUser`, and
  (for the first `TezConsole` creation on a node) `Sys.Modify`. Preflight warns when a node's
  token lacks them; the portal then runs with logins and box lists but no consoles there.
- **Teardown**: `destroy-competition.py` deletes exactly `tezcon-<comp>-<run_id>@pve` on every
  node before the VMs go — exact name match, never a prefix. An absent user counts as clean, so
  re-running destroy converges.

### The gate

Team consoles answer 423 until an admin opens access; logins and box lists work throughout.
Admins always bypass it.

```bash
python3 portal-access.py <comp> status | open | close
```

`state/access.json` survives a re-push (phase 8 re-runs, engine-recovery re-seed). A re-push
restarts the portal container (it reads `portal.json` once at startup), so open consoles drop and
need a reconnect.

### The stable hostname has one owner

Two cloudflared connectors on one tunnel token would split students between two ranges at random.
Before starting cloudflared, phase 8 fetches `https://$TEZ_PORTAL_HOSTNAME/healthz`
(`{comp, run_id, open}`):

| Answer | Action |
|---|---|
| Nobody: offline, error page, or not our JSON | Start cloudflared |
| This run | Start it again |
| Another comp or run | Skip the tunnel, record a degradation, stop any cloudflared this comp left behind. The portal stays reachable through `ssh -L` only |

**One-time Cloudflare setup** (by hand; the repo only consumes the token): create a remotely
managed tunnel in Zero Trust; add a public hostname → service `http://portal:8000` (cloudflared
runs in the portal's compose network, so the service name resolves); put the token and hostname
in the event env file as `TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`.

## Operating it

```bash
ssh -i proxmox -L 8443:127.0.0.1:8443 <vm_user>@<engine_ip>    # then http://127.0.0.1:8443
```

- `credentials.txt` gets a `Portal:` line once it's up.
- `/opt/tez-portal/state/access.log` records logins, console opens/closes, gate flips. Teardown
  copies it to `competitions/<id>/portal-access.log`, and artifact collection carries it into
  `.automated-tests/<run-id>/`.

**After a box rebuild**: Proxmox's destroy path removes that VM's ACL entries, so re-apply:

```bash
python3 portal-access.py <comp> resync
```

PUTs are idempotent. If it reports a token was re-created, re-run
`create-competition.py --competition <comp> --from-phase 8 --yes` so the engine's `portal.json`
picks up the new secret.

**After engine recovery**: `redeploy_engine_ops` clears `portal_up`; the advertised `--from-phase
7` re-seed re-runs phase 8, which re-ships the portal with the recorded tokens.

**No console token on a node**: re-run phase 8 with a privileged token, e.g.
`TEZ_PROXMOX_API_TOKEN=<privileged token> python3 create-competition.py --competition <comp>
--from-phase 8 --yes`. Phase 8's other steps are flag-guarded and skip.

## Limits

- **No clipboard.** QEMU's VGA console has no guest clipboard; the console page's *Type text…*
  button sends paced ASCII keystrokes instead.
- **RFB 3.3 cap.** PVE's `vncwebsocket` only completes an RFB 3.3 handshake; noVNC has no public
  setting for its version cap, so `Console.jsx` lowers the internal `_rfbMaxVersion` to 3.3 right
  after constructing the client. Re-check that field on a noVNC upgrade.
- **The engine healthcheck does not watch the portal** — `range-healthcheck.sh` is baked into the
  engine template, and changing it would rebuild every competition's engine template for an
  opt-in feature. The container has its own Docker healthcheck and `restart: unless-stopped`.

## Frontend

`portal/frontend/` shares the management UI's stack (React 19, Vite, Tailwind 4) and design.
`src/components/ui.jsx`/`kit.jsx` and `src/index.css` are verbatim copies of
`tezcatlipoca-dashboard/web/src/components/` (the shared kit's original home; plus portal-only rules) rather than cross-tree imports, because
the image builds on the engine from `portal/` alone. `tests/portal/test_portal_frontend.py` pins the
noVNC release tarball's integrity hash; it doesn't check the copies for drift.

- **Views**: `/` is sign-in, then the team hub (VPN setup panel when remote access is on, the
  VM access console grid) or the white-team view (gate, every team, access log);
  `/console/<team>/<box>` is one console per tab. The FastAPI app serves the build as
  an SPA, same as the dashboard service.
- **noVNC** comes from the v1.6.0 GitHub release tarball (pinned integrity hash), not the npm
  package: its `lib/` build is CommonJS with a top-level `await`, which Vite rejects; the release
  source's ES modules build cleanly.
- **Build**: the image builds the UI itself (Dockerfile stage 1: `npm ci` + `vite build`), so a
  deploy host needs no Node; only the frontend source ships to the engine.
- **Local dev**: `cd portal/frontend && npm install && npm run dev`. Vite proxies `/api`,
  `/healthz`, and the console websocket to `127.0.0.1:8443` (a local uvicorn portal, or a real
  engine's portal over `ssh -L`).

## Verification

`verify-competition.py` runs the `portal` gate when the Compfile enables it, probing the engine's
`127.0.0.1` listener the way cloudflared reaches it: health; a team login sees exactly its own
boxes; the gate (423 while closed); a cross-team console request refused (403); an admin login;
per node, one console relayed far enough to read the `RFB 003.` greeting; that relay id refused on
reuse. Offline coverage: `tests/portal/test_portal_*.py`, `tests/portal/test_pve_console_ops.py`.
