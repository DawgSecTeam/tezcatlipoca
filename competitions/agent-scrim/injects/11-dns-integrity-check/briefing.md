**PRIORITY: HIGH**

Ward clinicians report being redirected to look-alike portals.

**Tasking:**
1. Verify the internal DNS service on app01 answers authoritatively for ward records and log stale/duplicate zones.
2. Sweep ALL hosts files (Linux and Windows) for entries hijacking `*.meridian.local` names.
3. Verify each domain-joined box still resolves the domain (breakage here breaks authentication). Fix what you find.

Submit your integrity results per host.

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
