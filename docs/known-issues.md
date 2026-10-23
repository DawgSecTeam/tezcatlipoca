### Red's first in-event impact can lag 16–19 minutes

Two 30-minute scrims on `sliver-practice` (runs `run-b0b1ae1c`, `run-c0044f51`) passed every
blue gate and the red takedown/restore/foothold/tactics gates, but failed only `stalls`: the
first in-window impact landed at T+16 and T+19. The channel probe is benign by design (a
pre-T0 takedown makes blue start against an existing outage), so the gap is red cadence: the
deterministic policy spends early cycles on recon/persistence before its first pressure
action. The red tactics are otherwise present (4 distinct, 9 takedowns in the better run).
**Next action**: make the opening cycle carry a scored takedown (or lower the pressure
threshold for the first two cycles) and re-run a 30-minute scrim; do not count the seed's
benign C2 probe as an impact.

### Sliver's Windows DNS implant exits when its resolver is unreachable

The Windows DNS client does not survive the seed's callback window: it exits
(nonzero) when the DNS listener or gateway resolver is unavailable, and the
Windows persistence surfaces do not revive it reliably. The payload is still
compiled and listed in the manifest (`plant: false`); DNS is planted and proven
on one Linux box. `tlaloc c2-prep` builds every transport for every box, but the
full-spread seed must not require Windows DNS until the client is fixed.
**Next action**: rebuild the Windows DNS payload with a forced resolver, run it
under the service guard, and require it in the full matrix once it survives a
listener restart.

### Student portal: unproven paths

A single-node two-team, four-box acceptance run (`portal-smoke`) proved health/login/scoping, the closed gate (`423`), cross-team refusal (`403`), four truly concurrent relays (each `101` + `RFB 003.008`), ACL removal/reapply across a real box rebuild, and full teardown revocation (the exact `tezcon-<comp>-<engine-run-id>@pve` user and its ACLs gone). A second one-box run (`tiny-portal-smoke`) proved the **gate-open** path: `open: True`, team gate `HTTP 200`, and the console relay. A third one-box run proved the **public Cloudflare path** with a quick tunnel (`trycloudflare.com`): tunnel healthz `200`, tunnel team login `200`, and a tunnel console relay `wss 101` + `RFB 003.008`. Still unproven: the deploy-side **named-tunnel start** (`TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`, including the ownership decision) because the token is absent, and multi-node (the field node was down). **Next action**: run with the event's tunnel token/hostname and a two-node range.
