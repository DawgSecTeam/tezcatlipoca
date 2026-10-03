# Inject 3 — Persistence sweep directive

**Priority: CRITICAL — due in 60 minutes**

The incident response vendor's verdict: the adversary left persistence on **every machine** —
run keys and scheduled tasks on Windows; hidden cron jobs, autostart listeners, disguised
systemd services (some carrying a hidden exfiltration URL), raw-socket beacons, and backdoor
firewall rules on Linux.

Your tasks:

1. Sweep every box in your range for persistence artifacts: unknown services (read them with
   `systemctl cat` — names are disguised), cron/at jobs, autostart entries, run keys,
   scheduled tasks, and firewall rules that accept traffic from 198.51.100.77.
2. Disable and remove every artifact you find — killing a respawning service is not enough;
   it must be disabled.
3. Produce a short artifact inventory (host, name, type, action taken).

Scoring: service uptime continues throughout; inject score is awarded on reviewer evaluation
after close of business.
