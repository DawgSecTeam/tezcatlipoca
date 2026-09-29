**PRIORITY: HIGH**

A developer reports that a build credential file was readable by anonymous logins on the Fedora build server (app01), and similar secrets may be sitting in web roots on web01 and win01.

**Tasking:**
1. Find any world-readable credential material on web01, app01, and win01 (web roots, deploy keys, config files).
2. Rotate every exposed password, including service accounts, and verify the services that depend on them still work afterwards.
3. Report what you found, what you rotated, and what you verified.

Service availability is still scoring -- a rotation that breaks a scored service costs more than the leak.
