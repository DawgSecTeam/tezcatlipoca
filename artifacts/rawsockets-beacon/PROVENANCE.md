# Provenance

`beacon.c` / `Makefile` / `README.md` are vendored from an external defensive-security
test beacon project (a forged-SYN raw-socket beacon with a `BEA1` payload marker,
built for validating kernel firewall detection/blocking); not written in this repo.

Used by tezcatlipoca as a **scenario artifact**: when a Compfile sets
`team_beacons 1`, deploy plants compiled copies on team Linux boxes as
huntable adversary persistence (see `beacon_ops.py`). Blue's job is to find
the process/unit/binary and its periodic egress and shut it down properly.
