**PRIORITY: CRITICAL**

The actor is known to seed persistence: cron entries, systemd units/timers, profile hooks, registry run keys, scheduled tasks, and services on both Linux and Windows.

**Tasking:**
1. Sweep every box for persistence mechanisms: cron/cron.d/anacron, systemd units and timers, rc scripts, shell profiles, authorized_keys, on Windows: Run keys, scheduled tasks, services, WMI subscriptions, startup folders.
2. Remove what you can prove is attacker-planted. When in doubt, snapshot the artifact and remove it -- availability of scored services takes precedence.
3. Re-check 15 minutes after cleanup: anything that comes back means you missed the load mechanism.
4. Submit a per-box table of artifacts found, action taken, and verification result.
