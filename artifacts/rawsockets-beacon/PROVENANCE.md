# Provenance

`beacon.c` / `Makefile` / `README.md` vendored 2026-09-17 from
`~/Downloads/rawsockets-beacon/rawsockets-beacon-iptablesavoider-deepseek/`
(a defensive-security test beacon: forged-SYN raw-socket beacon with a `BEA1`
payload marker, built for validating kernel firewall detection/blocking).

Used by tezcatlipoca as a **scenario artifact**: when a Compfile sets
`team_beacons 1`, deploy plants compiled copies on team Linux boxes as
huntable adversary persistence (see `beacon_ops.py`). Blue's job is to find
the process/unit/binary and its periodic egress and shut it down properly.
