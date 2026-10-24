### Student portal: unproven paths

A single-node two-team, four-box acceptance run (`portal-smoke`) proved health/login/scoping, the closed gate (`423`), cross-team refusal (`403`), four truly concurrent relays (each `101` + `RFB 003.008`), ACL removal/reapply across a real box rebuild, and full teardown revocation (the exact `tezcon-<comp>-<engine-run-id>@pve` user and its ACLs gone). A second one-box run (`tiny-portal-smoke`) proved the **gate-open** path: `open: True`, team gate `HTTP 200`, and the console relay. A third one-box run proved the **public Cloudflare path** with a quick tunnel (`trycloudflare.com`): tunnel healthz `200`, tunnel team login `200`, and a tunnel console relay `wss 101` + `RFB 003.008`. Still unproven: the deploy-side **named-tunnel start** (`TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`, including the ownership decision) because the token is absent, and multi-node (the field node was down). **Next action**: run with the event's tunnel token/hostname and a two-node range.

### CLEF action-noul confidence is not yet translating into red impact

The first 4-box run with action-noul (`run-6b0389a7-run3`) had 11/29 `next_effect`
decisions from clef, with high-value `p_yes` 0.73-0.83, but red recorded only
one counted takedown and two stalls; blue completed two cycles and restored four
services. The action descriptions and/or the around-the-cycle candidate supply
still need tuning against range data. This is not a reason to lower theta: the
probe showed low theta mostly buys holds.
**Next action**: inspect the chosen candidate ids/descriptions on the high-p_yes
cycles, then tune descriptions/priority and rerun a 4-box scrim; judge by
takedowns and blue restores, not p_yes alone.
