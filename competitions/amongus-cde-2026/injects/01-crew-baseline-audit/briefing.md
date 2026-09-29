# Crew Tasking — Baseline Security Audit

TO: All crewmates, The Skeld
FROM: Command (mira)

An anonymous tip says someone walked into the ship's systems and left the doors
wide open. Before we jump, Command needs a written baseline of what is actually
exposed on your segment.

## Deliverable

A short report (PDF or text uploaded to the skeld FTP drop) covering:

1. Every account that can reach `mira` over the domain, and which groups hold it.
2. Every share reachable without a crew credential.
3. Which services on your box answer from outside your segment, and why each one
   is (or is not) supposed to.

## Constraints

- Keep every scored service ONLINE. A "fixed" service that stopped answering
  scores as a dead service.
- Do not touch the engine, the other crew segments, or accounts the scoring
  checks authenticate with.

The task board on the skeld FTP drop (`TASKS.txt`) lists today's maintenance
windows. Upload your report to the same drop.
