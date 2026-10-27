### Student portal: unproven paths

A single-node two-team, four-box acceptance run (`portal-smoke`) proved health/login/scoping, the closed gate (`423`), cross-team refusal (`403`), four truly concurrent relays (each `101` + `RFB 003.008`), ACL removal/reapply across a real box rebuild, and full teardown revocation (the exact `tezcon-<comp>-<engine-run-id>@pve` user and its ACLs gone). A second one-box run (`tiny-portal-smoke`) proved the **gate-open** path: `open: True`, team gate `HTTP 200`, and the console relay. A third one-box run proved the **public Cloudflare path** with a quick tunnel (`trycloudflare.com`): tunnel healthz `200`, tunnel team login `200`, and a tunnel console relay `wss 101` + `RFB 003.008`. Still unproven: the deploy-side **named-tunnel start** (`TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`, including the ownership decision) because the token is absent, and multi-node (the field node was down). **Next action**: run with the event's tunnel token/hostname and a two-node range.

### Placement probe over-scopes the vmid collision check

`multinode/placement_planner.probe_node` refuses a satellite whose existing VMs occupy ANY slot
the placement *might* use (`engine_vmid`, `+140`, `+150..`), even when that node will not host the
engine in the chosen layout. Consequence: a satellite running an unrelated competition's engine
(the same derived vmid band, from a previous worktree) is disqualified from hosting a satellite,
with the collision named as if a real clash existed. The workaround is choosing another node, and
`--scoring-vmid` does not help. **Fix to design**: probe per-role — only require the engine's own
slots on the node the engine will actually run on, and for satellites require only the jump vmid and
their assigned golden band, both derivable once the layout is fixed. Not changed yet because the
probe runs before the layout is chosen and every caller would need the layout passed in.

### `verifier/packet.py` scores only the first team's boxes

The packet-integrity check probes one box per competition (the first Linux box of the first team),
so a box that failed to boot or a service that did not start on any other team passes. Every team's
first Linux box already has a recorded target; widening it is a loop over `team_keys()` instead of a
single pick, but the check's runtime grows with the team count and it currently runs inside the same
timeout as the scoreboard gates.
