# Windows pin live-verification round — lab vmid 131 `tz-vulnlab-w1` (2026-10-02)

Box: Windows Server 2022 (`WIN-H7GN8JE8SN4`, PowerShell 5.1.20348.558), Workgroup member,
cloned from `base-windows-server`. Executed over the QEMU guest-agent channel as SYSTEM
(`pmx exec 131 -- powershell -NoProfile -ExecutionPolicy Bypass -File ...`).

## Method (and why it matters for the exit codes below)

nakon runs each step as its own `powershell.exe` launched with
`-NoProfile -NonInteractive -ExecutionPolicy Bypass -File` and computes the step rc as
`$LASTEXITCODE` if an external command set one, else `1` when the process's `$Error` collection is
non-empty, else `0` (`vendor/nakon/nakon/gen/powershell.py:render_step_ps1`). A local harness
reproduced that contract exactly and additionally captured the pre/post state:

- body vars are injected as **strings**, the way `render_step_ps1` bakes them in
  (`$KEY = '<value>'`), not as typed values;
- `$Error` is *not* cleared before the body, so `error_count` is what nakon would score;
- every run is followed by explicit probes (`net user`, `reg query`, `Get-WebApplication`,
  `Get-NetTCPConnection`, `Invoke-WebRequest`) because the finding — not the rc — is the deliverable.

A local harness reproduced that contract exactly; its transcripts were scratch and are not
retained — the rc traces are inline in the tables below, and the bodies it ran are the `.sh` files in
this directory (independently diffed against the live catalog: 10/10 exact matches).

## `local-user-win` — FAILS (silent no-op), fixed

Typed `-eq 1` on the left is a string (`'1'`), so PowerShell coerces the **right** operand:
`'1' -eq 1` → `False`. `ADMIN_ADD` was therefore never the bug; `PASSWORD_NEVER_EXPIRES` silently
no-opped on every run, on both the create and the existing-account path.

| run | body | rc (nakon contract) | end state |
|---|---|---|---|
| 1 | current, `PASSWORD_NEVER_EXPIRES=1` | 0 | account created, in Administrators, FullName/Description set — but `net user svc-kiosk` → `Password expires 11/13/2026` |
| 2 | current, same vars | 0 | same; still a finite expiry |
| A | fixed, after `Set-LocalUser -PasswordNeverExpires $false` | 0 | `net user svc-kiosk` → `Password expires Never` |
| B | fixed, consecutive re-run | 0 | idempotent; `Account expires Never`, `enabled=True`, `admin=True` |

Account creation was never the problem: the body already does `Get-LocalUser` → `New-LocalUser`
and the second consecutive run is rc=0. Measured side-fact worth keeping: `New-LocalUser -FullName`
rejects a value longer than the SAM limit (20 chars) with a *misleading* "does not meet the length,
complexity, or history requirements" error (`'Imaging Sync Service'` = 21 chars fails,
`'Short Name'` succeeds) — not a defect in this row, but a trap for whoever adds a full name.

## `powershell-execution-unrestricted` — FAILS (rc=1), fixed

| run | body | rc | state |
|---|---|---|---|
| 1 | current | **1** | `EnableScripts` never written; `HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell` absent |
| 2 | current | **1** | same |
| — | fixed from `ExecutionPolicy` **Undefined** | **0** | `exec_policy_LM=Unrestricted`, `EnableScripts=1`, `ShellIds.ExecutionPolicy=Unrestricted` |
| — | fixed, consecutive | **0** | idempotent |

Root cause: nakon's Process-scope `-ExecutionPolicy Bypass` outranks LocalMachine, so
`Set-ExecutionPolicy -Scope LocalMachine` raises a **terminating** `SecurityException`
("overridden by a policy defined at a more specific scope"). Measured on 5.1.20348:
`-ErrorAction Ignore`, `-ErrorAction SilentlyContinue` **and** `try/catch` all leave
`$Error.Count=1` → rc=1, and the throw aborted the three registry writes below it. A `reg export`
diff of `HKLM\SOFTWARE\Microsoft\PowerShell` before/after `Set-ExecutionPolicy -Scope LocalMachine`
is empty except the `ExecutionPolicy` value, so the fixed body writes that value directly and calls
no cmdlet that can throw.

## `rpc-proxy-on-dc-web-win` — FAILS (dirty plant), fixed

Prerequisite installed first (that is what `depends_on "IIS HTTP"` means in practice):
`Install-WindowsFeature Web-Server,RPC-over-HTTP-Proxy -IncludeManagementTools` → `Success=True
RestartNeeded=No`, `W3SVC=Running`, `%windir%\System32\RpcProxy` present with `RpcProxy.dll` +
`LBService.dll`, `WebAdministration` 1.0.0.0 available.

| run | condition | rc | notes |
|---|---|---|---|
| baseline | no IIS | 1 | `New-WebApplication : ... is not recognized` (a raw command-not-found) |
| 1 | IIS + role | 0 | apps created, but stdout carries the `FeatureOperationResult` object and `Target configuration object '...applications[@name="/Rpc"]' is not found at path 'MACHINE/WEBROOT/APPHOST'` — `poolManagementMode` is not an IIS application property |
| 2 | IIS + role | 0 | identical noise on the re-run |
| fixed 1 | apps deleted first | 0 | no output, no errors |
| fixed 2 | consecutive | 0 | no output, no errors |

Fixed end state: `/Rpc` + `/RpcWithCert` → `%windir%\System32\RpcProxy` on `DefaultAppPool`,
`RPCPROXY` handler mapped (`IsapiModule`), `HKLM:\SOFTWARE\Microsoft\Rpc\RpcProxy`
`Enabled=1` and `ValidPorts=WINDOWS-UOSF89N:593;WINDOWS-UOSF89N:49152-65535`, `W3SVC` running.
`Install-WindowsFeature RSAT-Rpc-Proxy` (the old body's feature) does not exist on any Server SKU,
so the role was never installed by the old body; `ValidPorts` also needs re-asserting because the
role only fills it during install and an empty value leaves a shell that cannot proxy anything.

## `unauth-kiosk-app-startup-win` — WORKS AS INTENDED, prerequisite gap made explicit

Prerequisite built for the test: `C:\KaminoAI\app` with a real CPython 3.11 venv
(`.venv\Scripts\python.exe`), `uvicorn` 0.54.0 + `fastapi` + `main.py`.

| run | condition | rc | state |
|---|---|---|---|
| baseline | no app tree, no `python` on PATH | **1** | `Start-Process : Cannot validate argument on parameter 'FilePath'. The argument is null or empty` — and the how-to file *is* still written first |
| A | venv python present, `uvicorn` missing | 0 | **silent no-op**: the interpreter starts, dies on `ModuleNotFoundError`, nothing listens |
| C1/C2 | full app tree, `:80` free | 0 | `0.0.0.0:80` pid, `HTTP 200 {"kiosk":"unauthenticated"}`, consecutive re-run 0 |
| earlier | IIS owning `:80` | 0 | **silent no-op**: http.sys holds the port, uvicorn dies, rc still 0 |
| fixed 3 | `:80` free | 0 | `0.0.0.0:80`, `HTTP 200` |
| fixed 4 | consecutive | 0 | idempotent, same pid serving |
| fixed D | app tree absent | **1** | `no python interpreter for the kiosk app: neither 'C:\KaminoAI\app\.venv\Scripts\python.exe' nor 'python' on PATH exists. Plant the kiosk app tree (or set APP_DIR) before this configuration.` |

So: in the met case the row already works and the finding is real (unauthenticated kiosk on
`0.0.0.0`, no service wrapper, no restart policy, dies on logout/reboot). The two silent no-ops are
parts of the planted fragility and were deliberately **not** made fatal — the fix only replaces the
misleading `Start-Process` argument error with an explicit prerequisite message and adds a
non-fatal liveness warning.

## `mailenable-cleartext-mail-win` — FAILS, NOT FIXED (cannot verify in budget)

| step | result |
|---|---|
| `https://community.chocolatey.org/api/v2/` | `200`; TLS 1.2 bootstrapper installs Chocolatey 2.7.4 — **the network and TLS path is fine** |
| `choco search mailenable --exact --limit-output` | empty; `choco search mail` lists ~25 mail packages, none of them MailEnable |
| current body | rc=1; `Program Files (x86)\Mail Enable` absent, no `ME*` service, no listeners; only the `MailEnable-Cleartext` firewall rule exists |
| `https://www.mailenable.com/MESetup.exe` | 29,526,424 bytes in 30 s, Authenticode `Valid`, `FileVersion 3.00` |
| `MESetup.exe /S` | exits 0, installs only the MAPI connector: `Bin\` = `MEINSTALLER.DLL`, `MEMAPIClean.exe`; no mail-server services, no listeners |

The documented cleartext POP3/IMAP weakness therefore never lands on a box. That is a
**wrong-package-ID script bug**, not the environmental network/TLS failure the handoff predicted.
The body also `-EA SilentlyContinue`s `Set-Service`/`Start-Service` on service names that are not
the installed ones (`MESMTPC`/`MEIA`/`MEPOP3S`; the real set is `MEMTAS`/`MEPOPS`/`MEIMAPS`), so
even a successful install would not have been started. Not applied: the acquisition path is fixed in
`mailenable-cleartext-mail-win.candidate`, but `/S` does not complete a full mail-server install —
that needs the interactive Wise installer / a response file, which was out of budget. The row stays
in `KNOWN_BROKEN_CONFIGS`.

## What was NOT touched

- No `restore-backup`, no `delete-backup`, no `delete`.
- Every planted weakness left in place; each fixed body was run twice in a row and re-read from the
  live catalog afterwards (`script_match=True` for all four, with `id`/`type`/`run_as`/`depends_on`
  unchanged).
- The kiosk app tree, IIS + `RPC-over-HTTP-Proxy` and a local `svc-kiosk` account were planted on
  the disposable lab box only, to give these rows their declared prerequisites.
