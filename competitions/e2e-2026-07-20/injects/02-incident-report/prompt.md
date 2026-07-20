# INJECT 02 — Incident Report: Suspicious SUID Binary

**From:** SOC Manager
**To:** Blue Team
**Priority:** Critical
**Time to complete:** 45 minutes

## Background

Monitoring flagged that a standard system utility on one of your hosts now carries the
**SUID bit** and is owned by root — a common privilege-escalation foothold. Management needs
a formal incident report suitable for handing to the (fictional) client.

## Tasks

1. **Identify** every unexpected SUID/SGID binary on the `webmail` and `core` hosts. Include
   the exact command you used to enumerate them.
2. **Assess impact**: explain how an attacker could abuse the specific binary you found to
   escalate privileges (reference GTFOBins-style technique).
3. **Remediate**: show the command(s) to safely remove the unnecessary SUID bit, and state
   how you confirmed the fix.
4. Provide a short **timeline** (detection → containment → remediation) with timestamps.

## Deliverable

Submit an incident report (PDF or Markdown) covering all four tasks. Screenshots of the
before/after `find` output strengthen your submission.
