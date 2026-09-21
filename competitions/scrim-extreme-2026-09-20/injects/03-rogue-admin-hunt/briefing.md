**PRIORITY: CRITICAL**

Forensics indicates the Wardline crew created at least one **administrative backdoor account** on **dc01**, disguised as a service account.

**Tasking:**
1. Enumerate local/domain accounts with administrative privileges on dc01.
2. Identify the impostor(s): look for service-named accounts with suspicious group memberships, never-expiring passwords, or logon patterns.
3. Decide a removal plan. Consider order-of-operations: disabling vs deleting, and what breaks when a service-like account disappears.

Document the rogue account(s), evidence, and your action (or deliberate deferral).

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
