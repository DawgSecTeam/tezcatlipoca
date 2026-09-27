**PRIORITY: HIGH**

An external audit found **attacker-added allow rules** in Linux firewall state and at least one removed/absent firewall on a ward system.

**Tasking:**
1. On web01, app01, and db01: inspect firewall rules (ufw/iptables) for entries allowing a known-bad host (`198.51.100.77`).
2. Identify which system is running with its firewall removed or ineffective.
3. Remove attacker rules and restore a minimal, availability-safe policy. Do NOT lock out your own management access or the scoring checks.

Document before/after rules per host.

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
