### Student portal: unproven paths

A single-node two-team, four-box acceptance run (`portal-smoke`) proved health/login/scoping, the closed gate (`423`), cross-team refusal (`403`), four truly concurrent relays (each `101` + `RFB 003.008`), ACL removal/reapply across a real box rebuild, and full teardown revocation (the exact `tezcon-<comp>-<engine-run-id>@pve` user and its ACLs gone). A second one-box run (`tiny-portal-smoke`) proved the **gate-open** path: `open: True`, team gate `HTTP 200`, and the console relay. A third one-box run proved the **public Cloudflare path** with a quick tunnel (`trycloudflare.com`): tunnel healthz `200`, tunnel team login `200`, and a tunnel console relay `wss 101` + `RFB 003.008`. Still unproven: the deploy-side **named-tunnel start** (`TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`, including the ownership decision) because the token is absent, and multi-node (the field node was down). **Next action**: run with the event's tunnel token/hostname and a two-node range.

### A failed deploy's reclaim throws away the templates it built

The dashboard reclaims a failed deploy automatically (2026-11-08 directive), using a full
`destroy-competition.py`. On a practice run that means the goldens and engine template the
failed attempt already converted are destroyed too, so the retry rebuilds ~20 minutes of
templates — and it is what makes the new `resume_deploy` action useful only while the workspace
still exists (a failure with no VMs recorded, a cancelled run, a refused reclaim). A
`--teams-only` reclaim would clear exactly the poisoned vmids/bridges the directive exists to
clear (the team boxes, engine and bridges go; templates stay, and the next deploy re-adopts them
by their hash stamps). Not changed unilaterally: it alters a directive, not a defect.

### gcp provider: the v1 matrix gaps (the Linux range is the cut)

The gcp backend deploys the Linux stack end to end (engine, goldens, boxes, red01, teardown).
Refused at preflight by NAME until each lands — never a silent half-deploy:

- **In-path firewalls** — pfSense cannot run on GCP at all; VyOS needs an imported appliance
  image tier, not the PVE clone path. Refusal names the box.
- **unmanaged appliance boxes** — same tier, no gcp path.

Windows boxes ride the implemented boot-time channel (`windows-startup-script-ps1` metadata
carrying the full proxmox bootstrap contract; unbooted DC goldens cut straight from the base
image so each team's clone specializes its own SID). It is unit-covered against the in-memory
API but has NOT survived a live bake yet — the first Windows gcp deploy is the proof.

Known-open beyond the matrix (these greet you with a clear refusal at use time):

- **redeploy's rollback modes** — a machine image restores by RECREATING the instance; the redeploy
  engine still assumes in-place rollback. `--mode reconfigure` is the only mode wired for gcp.
- **portal consoles on gcp** — there is no VNC on GCP, so the portal's vncproxy tier has no gcp
  path. A browser-SSH console gateway is the pending design; `portal 1` ranges deploy on proxmox
  only for now.
- **restore points reboot** — a machine image requires a stopped instance, so taking `tz-base`/
  `tz-ready` reboots the box (disk-equivalent restore state, which is what redeploy restores
  anyway; documented in the architecture map).
- **the golden-hash path still dials Proxmox** — `deploy_lib/configs.py` resolves base-template
  vmids through `_template_vmid_map` → `proxmox_api`, whose `config.require("proxmox.endpoint")`
  refuses a gcp-only profile. A gcp deploy today needs a reachable Proxmox endpoint in the
  profile purely so the golden hash inputs can carry a base-template id (a meaningless number
  on gcp). The engine template's sha256-of-image-URL base key is the pattern to extend here.

Also pending: the dashboard's web UI has no platform toggle yet — `platform` rides the MCP tool
and the HTTP API's `params` dict.

### gcp: instances.insert refuses the static regional-address attach on this project

Every reference shape of a real, RESERVED, PREMIUM external IPv4 address — the
www/compute-host full URLs the API itself returns, the relative form (which `subnetwork`
accepts), and the bare name — is refused by `instances.insert` with "The specified external IP
address … was not found in region 'us-central1'", against addresses the same project's list
confirms exist. Probed live 2026-11-06 with both a range reservation and a fresh short-named
one, full-URL subnets alongside (probe instances deleted; the reservations cleaned). Ephemeral
external IPv4 works on every instance unaided. The deploy therefore attaches the engine's
mgmt address as EPHEMERAL at create and records the realized IP in the state document — a
range never outlives its own instances, and a resume reuses the same engine. Revisit a static
pin only if the API starts accepting its own reservations.
