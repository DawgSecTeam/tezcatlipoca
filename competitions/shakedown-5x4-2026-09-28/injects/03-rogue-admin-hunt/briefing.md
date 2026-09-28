**PRIORITY: CRITICAL**

Directory auditing flagged account and group changes on dc01 that no change ticket covers: new local and domain accounts, unexpected group memberships, and delegation oddities.

**Tasking:**
1. Enumerate local accounts on every Windows box and domain accounts in AD; compare against the baseline you captured at kickoff.
2. Remove attacker-created accounts and group memberships (Domain Admins, Administrators, RDP groups). Document everything you remove.
3. Check delegation (constrained/unconstrained) and any GPO-level persistence you can see.
4. Report the account inventory before/after and your assessment of how the actor got the privileges.

Keep dc01's scored services up throughout -- do not disable AD or WinRM in the process.
