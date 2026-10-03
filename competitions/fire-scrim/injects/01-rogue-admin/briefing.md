# Inject 1 — Rogue administrator discovered

**Priority: HIGH — due in 30 minutes**

Hive Interactive's identity audit just flagged a service account on the domain controller that
was granted administrator group membership outside the change window. The account was created
with password-never-expires and a weak, guessable password.

Your tasks:

1. Identify the rogue administrator account on `dc01` and remove it from the Administrators
   group (do not delete the account — IR needs it for evidence).
2. Force a password reset on the account and enable expiry.
3. Document the account's creation time and any logon activity you can recover.

Scoring: service uptime continues throughout; inject score is awarded on reviewer evaluation
after close of business.
