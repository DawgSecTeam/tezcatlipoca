**PRIORITY: HIGH**

Netflow analysis flags periodic **outbound beacons** from your Linux wards toward attacker infrastructure, and possible DNS-based exfiltration.

**Tasking:**
1. Identify the beaconing process(es) on web01/app01/db01 (look for 'telemetry', 'health', or raw-socket daemons; check systemd unit Environment lines for exfil URLs).
2. Check hosts files on ALL systems (Linux `/etc/hosts` and Windows `C:\Windows\System32\drivers\etc\hosts`) for redirected medical hostnames pointing at `198.51.100.77`.
3. Stop the beacons and remove the redirects; preserve evidence (paths, URLs, timing).

Submit the beacon/redirect kill sheet.

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
