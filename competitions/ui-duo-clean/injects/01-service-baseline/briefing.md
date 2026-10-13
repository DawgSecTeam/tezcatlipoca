# Service Baseline Report

From: Meridian Components — IT Director
To: Infrastructure Response Team

Both of our public-facing systems are now yours to look after. Before anything else, we need a
baseline on record.

## Your tasks

1. Confirm both scored services are reachable and healthy:
   - **lnx01** — nginx on port 80 (`http://192.168.140.2/`)
   - **win01** — IIS on port 80 (`http://192.168.140.3/`)
2. Log in to each host with the credentials you have been issued and record: OS version,
   service version, and where the service logs live.
3. Submit a short baseline report (one page is fine) covering the above and anything that
   already looks off to you.

## Constraints

- Do not take either service down. The scoreboard is watching both continuously.
- There is no known compromise. This is a hygiene exercise, not an incident response.
