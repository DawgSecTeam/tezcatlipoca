# INJECT 03 — DNS & Database Service Hardening

**From:** Infrastructure Manager
**To:** Blue Team
**Priority:** Medium
**Time to complete:** 55 minutes

## Background

The `core` host runs the organization's **DNS (BIND)** and **MariaDB** services. Both must
stay reachable for scoring, but both are currently configured more permissively than our
security baseline allows. Harden them **without** taking them offline.

## Tasks

1. **BIND**: our baseline forbids open recursion to arbitrary clients. Describe how you would
   restrict recursion to only the trusted range while still answering the scored `localhost`
   A-record query. Provide the `named.conf.options` snippet.
2. **MariaDB**: enumerate all accounts with `ALL PRIVILEGES` and `GRANT OPTION`. Explain which
   are unnecessary and how you would apply least privilege — being careful to leave the scored
   service-check account able to authenticate.
3. Document a **verification step** for each service proving it is still up after your changes.

## Deliverable

Submit a hardening change record (PDF or Markdown). Note any change that could plausibly
break the scored check, and how you avoided it.

> Reminder: availability is scored continuously. If a service check goes red while you work,
> you are losing points — plan changes to minimize downtime.
