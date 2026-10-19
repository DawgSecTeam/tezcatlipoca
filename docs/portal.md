# Student portal: Quotient login → Proxmox web console

Blue teams log in with their **Quotient team credentials** and get a **browser console** (the
VM's own screen, through Proxmox) on each of their team's boxes. Opt-in per competition, deployed
on the scoring engine, non-fatal to the deploy: a portal that can't start is recorded as a
degradation and the range carries on — verify's `degradations` gate reports it as a failure.
Remaining unproven paths (named tunnel, multi-node): [known-issues.md](known-issues.md).

## Turning it on

Compfile keys (`portal/portal_ops.py` owns flags, defaults and semantics): `portal 1` deploys the
portal at the end of phase 8; `portal_firewall_console 0` hides the in-path firewall's console
from teams (they own their pfSense by default); `portal_scoreboard_url <url>` overrides the
scoreboard link; `portal_hostname <host>` is printed in the competitor packet's *Web console*
section (packet text only).

Env: `TEZ_PORTAL_TUNNEL_TOKEN` (Cloudflare token — without it the portal is reachable only
through `ssh -L`), `TEZ_PORTAL_HOSTNAME` (the tunnel's stable public name, used for the ownership
check), `TEZ_PORTAL_OPEN=1` (start with the gate open, for practice runs). These are `_SPEC` keys
`portal.tunnel_token` / `portal.hostname` / `portal.open` ([usage-people.md](usage-people.md#configure-the-event)).
**The tunnel pair goes in the event's own env file, never a practice worktree's** — starting the
tunnel claims the public hostname (below).

## How it works

The portal (FastAPI/uvicorn, `portal/`) is published on `127.0.0.1:8443` on the engine only —
team subnets reach nothing; cloudflared dials out. It reads `/opt/quotient/config/event.conf`
(read-only bind mount) and `portal.json` (boxes, nodes, console tokens), keeps gate state and the
audit trail in `state/access.json` / `state/access.log`, and relays
`WS /console/ws/<one-shot id>` to `wss://<node>:8006/…/vncwebsocket`. Deploy side:
`portal/portal_ops.py`, hooked at the end of phase 8 (`deploy_lib/phases/seed.py`); verify gate
`portal` (`verifier/portal.py`); operator CLI `portal-access.py`.

### Logins never touch Quotient

The portal adds no Quotient sessions and doesn't need Quotient up to let a team in — it reads the
same `event.conf` Quotient reads and compares against its `[[team]]`/`[[admin]]` entries,
re-reading on mtime change so a rotated password takes effect without a restart. Roles: **team**
(its own boxes only), **admin**/white team (every team's boxes, plus the gate); `scoring` and
`[[inject]]` accounts are refused. Accounts are shared per team, so the display name typed at
login is the only per-student attribution — recorded in the access log.

### The dashboard's VPN panel

With remote access on ([remote-access.md](remote-access.md)), phase 8 folds enrollment into
`portal.json` (`vpn`: login server plus each team's per-person keys from
`remote-access/keys.json`), and `/api/me` hands a team session only its own people — a team never
sees another team's keys, like boxes. The team view shows a **VPN setup** panel above the console
grid; no remote access (or no roster) means no panel.

### Isolation

The console token can open a console on every team VM of this competition, so scoping is enforced
server-side (pinned by `tests/portal/test_portal_app.py`):

- `POST /api/console` resolves the box from the session's team; another team gets 403, same as a
  nonexistent box, so a team learns nothing about other lineups.
- Relay ids are single-use and bound to the minting session; a leaked id is burned on first use.
- The browser holds only the relay id and a VNC ticket useless without it — never the token.
- The portal listens on `127.0.0.1` only.

### The console token

`infra/console.py` mints one per-run user per node the teams live on: `tezcon-<comp>-<run_id>@pve`
(`console_user()`), role `TezConsole` = `VM.Console` only (shared across competitions, never
deleted), one ACL per team VM of this competition. The token can open no other VM, power-cycle
nothing, read no config.

- The secret lives only in the state document (`run.portal_console_tokens`); PVE reveals it at
  creation only, so a vanished token is re-created.
- Minting needs `User.Modify`, `Permissions.Modify`, `Realm.AllocateUser`, `Sys.Modify`
  (`console.REQUIRED_PRIVS`). A node whose deploy token lacks them is skipped with a recorded
  degradation (`mint_console_tokens` probes per node) and its portal runs with logins and box
  lists but no consoles.
- `destroy-competition.py` deletes exactly `tezcon-<comp>-<run_id>@pve` on every node before the
  VMs go — exact name match, never a prefix. An absent user counts as clean, so re-running
  destroy converges.

### The gate

Team consoles answer 423 until an admin opens access; logins and box lists work throughout;
admins always bypass it.

```bash
python3 portal-access.py <comp> status | open | close
```

`state/access.json` survives a re-push and is re-read per request. A re-push does restart the
portal container (`portal.json` is read once at startup), so open consoles drop and need a
reconnect.

### The stable hostname has one owner

Two cloudflared connectors on one tunnel token would split students between two ranges at random,
so before starting cloudflared phase 8 asks `https://$TEZ_PORTAL_HOSTNAME/healthz` who owns the
name (`portal_ops.tunnel_decision`): free or this run → start; another comp or run → skip the
tunnel, record a degradation, and stop any cloudflared this comp left behind — the portal stays
reachable through `ssh -L` only.

**One-time Cloudflare setup** (by hand; the repo only consumes the token): create a remotely
managed tunnel in Zero Trust; add a public hostname → service `http://portal:8000` (cloudflared
runs in the portal's compose network, so the service name resolves); put the token and hostname
in the event env file.

## Operating it

```bash
ssh -i proxmox -L 8443:127.0.0.1:8443 <vm_user>@<engine_ip>    # then http://127.0.0.1:8443
```

- `credentials.txt` gets a `Portal:` line once it's up.
- `/opt/tez-portal/state/access.log` records logins, console opens/closes, gate flips; teardown
  copies it to `competitions/<id>/portal-access.log`, and artifact collection carries it into
  `.automated-tests/<run-id>/`.

**After a box rebuild**: Proxmox's VM-destroy path removes that VM's ACL entries (the rest stay),
so re-apply:

```bash
python3 portal-access.py <comp> resync
```

`resync` reuses the recorded token (PUTs are idempotent). If it reports a token was re-created,
re-run `python3 create-competition.py --competition <comp> --from-phase 8 --yes` so the engine's
`portal.json` picks up the new secret.

**After engine recovery**: `redeploy_engine_ops` clears `portal_up`; the advertised `--from-phase
7` re-seed re-runs phase 8, which re-ships the portal with the recorded tokens.

**No console token on a node**: re-run phase 8 with a privileged token —
`TEZ_PROXMOX_API_TOKEN=<privileged token> python3 create-competition.py --competition <comp>
--from-phase 8 --yes` (phase 8's other steps are flag-guarded and skip).

## Limits

- **No clipboard.** QEMU's VGA console has no guest clipboard; the console page's *Type text…*
  button sends paced ASCII keystrokes instead.
- **RFB 3.3 cap.** PVE's `vncwebsocket` only completes an RFB 3.3 handshake and noVNC has no
  public setting for its version cap — `Console.jsx` lowers the internal `_rfbMaxVersion`;
  re-check that field on a noVNC upgrade.
- **The engine healthcheck does not watch the portal** — `range-healthcheck.sh` is baked into the
  engine template, and changing it would rebuild every competition's engine template for an
  opt-in feature. The container has its own Docker healthcheck and `restart: unless-stopped`.

## Frontend

`portal/frontend/` shares the dashboard's stack and UI kit by in-tree copy (the image builds on
`portal/` alone, so no cross-tree imports); nothing drift-checks the copies. noVNC is pinned to
its GitHub release tarball with an integrity hash (`tests/portal/test_portal_frontend.py`), not
the npm package: the `lib/` build is CommonJS with a top-level `await`, which Vite rejects, while
the release source's ES modules build cleanly. The image builds the UI itself (`npm ci` +
`vite build`, Dockerfile stage 1), so a deploy host needs no Node. Local dev:
`cd portal/frontend && npm install && npm run dev` (Vite proxies `/api`, `/healthz` and the
console websocket to `127.0.0.1:8443` — a local uvicorn portal, or a real engine's portal over
`ssh -L`).

## Verification

`verify-competition.py` runs the `portal` gate when the Compfile enables it, probing the engine's
`127.0.0.1` listener the way cloudflared reaches it — health, a team login sees exactly its own
boxes, the gate (423 while closed), cross-team console refused (403), an admin login, one console
relayed per node in `portal.json` far enough to read the `RFB 003.` greeting (a node with no box
sample is a named FAIL), and a relay id refused on reuse; with `public_host` set it repeats the
probe through `https://<public_host>` (a broken tunnel is a FAIL row, not a crash). Probe list:
`portal_ops.check_portal`. `--portal-concurrency N` opens N relays at once across team boxes so
`pveproxy` is exercised at event size instead of one at a time (`verifier/cli.py`). Offline
coverage: `tests/portal/` (`test_portal_*.py`, `test_pve_console_ops.py`).
