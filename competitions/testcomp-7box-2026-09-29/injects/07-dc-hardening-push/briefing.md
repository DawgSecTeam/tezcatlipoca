**PRIORITY: HIGH**

The CISO issued an emergency hardening directive for **dc01**:

1. Raise the minimum password length above 4 and re-enable account lockout (current policy invites spraying).
2. SMB1 must not remain enabled; verify actual protocol status, not just the registry stub.
3. Require NLA for RDP and disable the legacy LM authentication level.
4. Restore at least basic logon auditing.

**Constraints:** do not break WinRM (scored), the domain-joined members' authentication, or your own remote access. Test after each change. Submit what you changed, evidence, and anything you rolled back.

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
