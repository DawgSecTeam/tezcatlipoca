**PRIORITY: CRITICAL**

The intrusion crew maintains presence through **persistence mechanisms** on both Windows and Linux systems.

**Tasking:**
1. Windows: sweep scheduled tasks, Run/RunOnce registry keys, and non-standard services on dc01 and win01. Flag anything that launches PowerShell from `C:\ProgramData\Wardline\` or similar paths.
2. Linux: sweep systemd units (including suspicious 'optimizer'/'health' daemons), cron directories (including hidden entries), and user crontabs on web01, app01, db01.
3. Neutralize persistence without killing scored services; list anything you leave in place and why.

Submit a persistence inventory table (host, mechanism, artifact, action taken).

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
