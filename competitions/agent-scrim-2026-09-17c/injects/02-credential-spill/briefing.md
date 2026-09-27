**PRIORITY: HIGH**

A payroll service account spreadsheet was found world-readable in a share on **dc01** (`C:\ProgramData\Meridian\hr-credentials.txt`).

**Tasking:**
1. Locate the file and inventory every credential it exposes.
2. Determine where each credential is valid (local accounts? services? scheduled tasks?).
3. Rotate or neutralize what you safely can WITHOUT breaking scored services or the shared scoring-check accounts, and document what remains exposed.

Submit your findings and remediation decisions.

---
*Meridian Health Incident Response -- inject delivered via scoreboard. Submit your response through the scoreboard before close.*
