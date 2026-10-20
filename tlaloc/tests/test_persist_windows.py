"""persist_windows — the Windows families behind `persist`."""

import base64
import re

import pytest

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc.effects import artifactnames
from tlaloc.effects import execute
from tlaloc.effects import persist
from tlaloc.effects import persist_windows as pw
from tlaloc.exec import winexec

WIN_IP = "192.168.1.20"
LINUX_IP = "192.168.1.10"
PASSWORD = "boxpw-1"

# pinned naming draw: what makes the exact command strings assertable
NAMES = {
    "stem": "hvdiag",
    "mode": "full",
    "run_value": "HvdiagRefresh",
    "task": "CsreportRenew",
    "log_task": "WmitraceWatchdog",
    "user": "svc-netmeter",
    "service": "PwrshiftGuard",
    "script": "C:\\Windows\\System32\\hvdiag-refresh.cmd",
    "wmi": "RegpollMonitor",
    "decoy_service": "ShdwcopyAgent",
    "decoy_script": "C:\\Windows\\System32\\shdwcopy.cmd",
}

SCRIPT = NAMES["script"]
TASK = NAMES["task"]
LOG_TASK = NAMES["log_task"]
SERVICE = NAMES["service"]
RUN_VALUE = NAMES["run_value"]
WMI = NAMES["wmi"]
DECOY = NAMES["decoy_service"]
HELPER = f"C:\\Windows\\System32\\{NAMES['stem']}-maint.cmd"
WATCH_DIR = f"C:\\ProgramData\\Microsoft\\{SERVICE}"
WATCH_HELPER = f"{WATCH_DIR}\\{NAMES['stem']}-watch.ps1"

VERIFY = "if ($?) { exit 0 } else { exit 1 }"
HIDDEN_OUT = f"HIDDEN|{SCRIPT}\n"
TICKS = "637000000000000000"
TS_OUT = f"TS|{SCRIPT}|{TICKS}|{TICKS}|{TICKS}\n"


@pytest.fixture
def pinned_names(monkeypatch):
    monkeypatch.setattr(artifactnames, "windows_names",
                        lambda *args, **kwargs: dict(NAMES))
    return dict(NAMES)


def decode(cmd):
    marker = "-EncodedCommand "
    assert cmd.startswith("powershell -NoProfile -NonInteractive " + marker), cmd
    return base64.b64decode(cmd.split(marker, 1)[1]).decode("utf-16-le")


def scripted(ssh, rc=0, out="", err=""):
    """The fake's first-match rule wins."""
    ssh.on(lambda call: True, rc=rc, out=out, err=err)
    return ssh


def win_ctx(tmp_path, ssh, *, admin=True, seeded=False, **over):
    ctx = make_ctx(tmp_path, ssh=ssh,
                   cfg=base_cfg(red={"assume_breach": "off"},
                                run={"host_guard": False}), **over)
    if admin:
        ctx.world.add_foothold(WIN_IP, "Administrator", PASSWORD, "root",
                               windows=True)
    else:
        ctx.world.add_foothold(WIN_IP, "analyst", PASSWORD, "user", windows=True)
    if seeded:
        ctx.world.add_artifact(WIN_IP, "persist-seed",
                               "helper .cmd dropped by an earlier family id=seed",
                               check_cmd="if ($?) { exit 0 } else { exit 1 }",
                               paths=[SCRIPT])
    return ctx


def assert_planted(ssh, script, *, password=PASSWORD):
    assert len(ssh.calls) == 1, [decode(c["cmd"]) for c in ssh.calls]
    call = ssh.calls[0]
    assert call["ip"] == WIN_IP
    assert call["password"] == password
    assert call["windows"] is False, ("EncodedCommand is one argument — the "
                                     "handler composes the wrap itself, once")
    assert call["cmd"] == winexec.ps(script)
    assert decode(call["cmd"]) == script
    return call


def assert_checkable(artifact):
    check = artifact["check_cmd"]
    assert check and "\n" not in check and '"' not in check
    winexec.assert_shippable(check)     # run.py re-runs check_cmd through the legacy wrap


def test_the_handler_covers_every_family_persist_names():
    assert set(persist.AUTO_ORDER_WINDOWS) <= set(pw.FAMILIES)
    order = persist.auto_order(windows=True)
    assert order[:len(persist.AUTO_ORDER_WINDOWS)] == list(persist.AUTO_ORDER_WINDOWS)
    assert set(order) == set(pw.FAMILIES)
    assert all(callable(handler) for handler in pw.FAMILIES.values())
    assert persist.families(windows=True) == dict(pw.FAMILIES)


def test_dry_run_plans_without_touching_the_box(tmp_path, pinned_names):
    ssh = FakeSSH()
    ctx = win_ctx(tmp_path, ssh, dry_run=True)
    result = execute("persist", {"ip": WIN_IP, "family": "auto"}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.artifacts() == [] and ctx.world.restore_table() == []


def test_scheduled_task_plants_a_system_task(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "scheduled_task"}, ctx)
    assert result.ok, result.detail
    script = (f"Set-Content -Path '{SCRIPT}' -Value 'ping -n 600 127.0.0.1 >nul' "
              "-Encoding ascii; "
              f"schtasks /Create /TN '{TASK}' /TR '{SCRIPT}' "
              "/SC MINUTE /MO 15 /RU SYSTEM /F; "
              f"schtasks /Query /TN '{TASK}' >$null; {VERIFY}")
    assert_planted(ssh, script)

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-scheduled-task"
    assert TASK in artifact["detail"]
    assert artifact["paths"] == [SCRIPT]
    assert_checkable(artifact)
    assert artifact["check_cmd"] == f"schtasks /Query /TN '{TASK}' >$null; {VERIFY}"
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"schtasks /Delete /TN '{TASK}' /F" in recipe
    # ROE: move, not delete
    assert (f"Move-Item -Path '{SCRIPT}' -Destination "
            f"'{SCRIPT}.tlaloc-bak' -Force") in recipe


def test_runkey_plants_an_hklm_run_value(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "runkey"}, ctx)
    assert result.ok, result.detail
    key = "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Run"
    script = (f"New-ItemProperty -Path '{key}' -Name '{RUN_VALUE}' "
              f"-Value '{pw.SLEEP_CMDLINE}' -PropertyType String -Force | Out-Null; "
              f"$v = (Get-ItemProperty -Path '{key}' "
              f"-ErrorAction SilentlyContinue).{RUN_VALUE}; "
              "if ($v) { exit 0 } else { exit 1 }")
    assert_planted(ssh, script)

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-runkey"
    assert f"{RUN_VALUE}" in artifact["detail"]
    assert_checkable(artifact)
    assert artifact["check_cmd"] == (f"if ((Get-ItemProperty '{key}').{RUN_VALUE}) "
                                     "{ exit 0 } else { exit 1 }")
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"Remove-ItemProperty -Path '{key}' -Name '{RUN_VALUE}' -Force" in recipe
    # the payload is winexec's own EncodedCommand: no hand-rolled command line survives
    assert pw.SLEEP_CMDLINE in decode(ssh.calls[0]["cmd"])
    assert pw.SLEEP_CMDLINE == winexec.ps("Start-Sleep 600")


def test_defrun_plants_the_default_profile_run_value(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "defrun"}, ctx)
    assert result.ok, result.detail
    key = ("Registry::HKEY_USERS\\.DEFAULT\\Software\\Microsoft\\Windows"
           "\\CurrentVersion\\Run")
    script = (f"$p = '{key}'; New-Item -Path $p -Force | Out-Null; "
              f"New-ItemProperty -Path $p -Name '{RUN_VALUE}' "
              f"-Value '{pw.SLEEP_CMDLINE}' -PropertyType String -Force | Out-Null; "
              f"$v = (Get-ItemProperty -Path $p -ErrorAction SilentlyContinue)."
              f"{RUN_VALUE}; if ($v) {{ exit 0 }} else {{ exit 1 }}")
    assert_planted(ssh, script)

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-defrun"
    assert "new profiles inherit it" in artifact["detail"]
    assert_checkable(artifact)
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"Remove-ItemProperty -Path '{key}' -Name '{RUN_VALUE}' -Force" in recipe


def test_service_creates_and_starts_an_auto_service(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "service"}, ctx)
    assert result.ok, result.detail
    script = (f"Set-Content -Path '{SCRIPT}' -Value 'ping -n 600 127.0.0.1 >nul' "
              "-Encoding ascii; "
              f"if (Get-Service -Name '{SERVICE}' -ErrorAction SilentlyContinue) {{ "
              f"sc.exe config '{SERVICE}' binPath= '{SCRIPT}' start= auto | Out-Null }} "
              f"else {{ sc.exe create '{SERVICE}' binPath= '{SCRIPT}' start= auto "
              "| Out-Null }; "
              f"sc.exe start '{SERVICE}' | Out-Null; "
              f"sc.exe query '{SERVICE}' >$null; {VERIFY}")
    assert_planted(ssh, script)
    # the re-assert branch is what makes a second plant idempotent on the box
    assert "sc.exe config" in decode(ssh.calls[0]["cmd"])

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-service"
    assert artifact["paths"] == [SCRIPT]
    assert_checkable(artifact)
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"sc.exe delete '{SERVICE}'" in recipe
    assert any("tlaloc-bak" in step for step in recipe), \
        "the dropped helper is moved aside, never deleted"


def test_wmisub_plants_a_permanent_wmi_subscription(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "wmisub"}, ctx)
    assert result.ok, result.detail
    script = (f"Set-Content -Path '{SCRIPT}' -Value 'ping -n 600 127.0.0.1 >nul' "
              "-Encoding ascii; "
              "$f = Set-WmiInstance -Namespace root\\subscription -Class __EventFilter "
              f"-Arguments @{{Name='{WMI}';EventNamespace='root\\cimv2';"
              "QueryLanguage='WQL';Query='SELECT * FROM __InstanceModificationEvent "
              "WITHIN 300 WHERE TargetInstance ISA Win32_LocalTime'}; "
              "$c = Set-WmiInstance -Namespace root\\subscription "
              "-Class CommandLineEventConsumer -Arguments "
              f"@{{Name='{WMI}';CommandLineTemplate='{SCRIPT}'}}; "
              "Set-WmiInstance -Namespace root\\subscription "
              "-Class __FilterToConsumerBinding -Arguments @{Filter=$f;Consumer=$c} "
              "| Out-Null; "
              "if (Get-WmiObject -Namespace root\\subscription -Class __EventFilter "
              f"| Where-Object {{ $_.Name -eq '{WMI}' }}) {{ exit 0 }} else {{ exit 1 }}")
    assert_planted(ssh, script)

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-wmisub"
    assert WMI in artifact["detail"] and SCRIPT in artifact["detail"]
    assert_checkable(artifact)
    recipe = " | ".join(ctx.world.restore_table()[0]["recipe"])
    for klass in ("__FilterToConsumerBinding", "CommandLineEventConsumer",
                  "__EventFilter"):
        assert klass in recipe
    assert "Remove-WmiObject" in recipe


def test_watchpair_plants_a_mutual_watchdog_pair(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "watchpair"}, ctx)
    assert result.ok, result.detail
    calls = ssh.calls
    assert len(calls) == 1
    script = decode(calls[0]["cmd"])
    assert calls[0]["cmd"] == winexec.ps(script)
    assert f"-TaskName '{TASK}'" in script and f"-TaskName '{LOG_TASK}'" in script
    assert f"Set-Content -Path '{WATCH_HELPER}'" in script
    assert f"Copy-Item -Path $env:ComSpec -Destination '{WATCH_DIR}\\{TASK}.exe'" in script
    assert f"Copy-Item -Path $env:ComSpec -Destination '{WATCH_DIR}\\{LOG_TASK}.exe'" in script
    # the helper re-registers whichever task is missing — that is the mechanism
    assert "Register-ScheduledTask -TaskName $t" in script
    # doubled quotes: the helper ships as an array of single-quoted lines
    assert f"-ArgumentList ''/c {pw.SLEEP_CMDLINE}''" in script

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-watchpair"
    assert artifact["paths"] == [WATCH_HELPER, f"{WATCH_DIR}\\{TASK}.exe",
                                 f"{WATCH_DIR}\\{LOG_TASK}.exe"]
    assert_checkable(artifact)
    recipe = ctx.world.restore_table()[0]["recipe"]
    # blue must undo BOTH tasks in one pass or the pair re-creates itself
    assert any(TASK in step and LOG_TASK in step for step in recipe)
    assert any(("tlaloc-bak" in step and WATCH_DIR in step) for step in recipe)
    note = " ".join(ctx.world.target(WIN_IP)["notes"])
    assert "BOTH" in note and WATCH_DIR in note


def test_hide_marks_the_planted_files_hidden(tmp_path, pinned_names):
    ssh = scripted(FakeSSH(), out=HIDDEN_OUT)
    ctx = win_ctx(tmp_path, ssh, seeded=True)
    result = execute("persist", {"ip": WIN_IP, "family": "hide"}, ctx)
    assert result.ok, result.detail
    script = (f"$ok = 0; foreach ($p in @('{SCRIPT}')) {{ if (Test-Path $p) {{ "
              "attrib +h +s $p; if ((Get-Item $p -Force).Attributes.ToString()"
              ".Contains('Hidden')) { Write-Output ('HIDDEN|' + $p); $ok++ } } }; "
              "if ($ok -gt 0) { exit 0 } else { exit 1 }")
    assert_planted(ssh, script)

    mine = [a for a in ctx.world.artifacts(WIN_IP) if a["kind"] == "persist-hide"]
    assert len(mine) == 1
    assert SCRIPT in mine[0]["detail"]
    assert mine[0]["paths"] == [SCRIPT]
    assert_checkable(mine[0])
    assert ctx.world.restore_table()[0]["recipe"][0] == f"attrib -h -s {SCRIPT}"


def test_hide_with_nothing_planted_fails_honestly(tmp_path, pinned_names):
    ssh = FakeSSH()
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "hide"}, ctx)
    assert result.ok is False
    assert "nothing planted to hide" in result.detail
    assert ssh.calls == []
    assert ctx.world.artifacts(WIN_IP) == []


def test_logwipe_clears_the_logs_and_leaves_a_reclear_task(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "logwipe"}, ctx)
    assert result.ok, result.detail
    body = "wevtutil cl Security & wevtutil cl System & wevtutil cl Application"
    script = (f"wevtutil cl Security; wevtutil cl System; wevtutil cl Application; "
              f"Set-Content -Path '{HELPER}' -Value '{body}' -Encoding ascii; "
              f"schtasks /Create /TN '{LOG_TASK}' /TR '{HELPER}' "
              "/SC MINUTE /MO 30 /RU SYSTEM /F; "
              f"schtasks /Query /TN '{LOG_TASK}' >$null; {VERIFY}")
    assert_planted(ssh, script)

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-logwipe"
    assert artifact["paths"] == [HELPER]
    assert_checkable(artifact)
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"schtasks /Delete /TN '{LOG_TASK}' /F" in recipe
    # the wiped records are honestly declared unrecoverable rather than faked
    assert any("unrecoverable" in step for step in recipe)


def test_timestomp_stamps_the_planted_files_and_records_the_originals(tmp_path,
                                                                    pinned_names):
    ssh = scripted(FakeSSH(), out=TS_OUT)
    ctx = win_ctx(tmp_path, ssh, seeded=True)
    result = execute("persist", {"ip": WIN_IP, "family": "timestomp"}, ctx)
    assert result.ok, result.detail
    script = ("$r = (Get-Item $env:SystemRoot).CreationTime; $n = 0; "
              f"foreach ($p in @('{SCRIPT}')) {{ "
              "$i = Get-Item -Force $p -ErrorAction SilentlyContinue; "
              "if ($i) { Write-Output ('TS|' + $p + '|' + $i.CreationTime.Ticks "
              "+ '|' + $i.LastWriteTime.Ticks + '|' + $i.LastAccessTime.Ticks); "
              "$i.CreationTime = $r; $i.LastWriteTime = $r; $i.LastAccessTime = $r; "
              "$n++ } }; "
              "if ($n -gt 0) { exit 0 } else { exit 1 }")
    assert_planted(ssh, script)

    mine = [a for a in ctx.world.artifacts(WIN_IP)
            if a["kind"] == "persist-timestomp"]
    assert len(mine) == 1 and mine[0]["paths"] == [SCRIPT]
    assert_checkable(mine[0])
    # the pre-stomp ticks came back in the same round trip: the undo is exact
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"(Get-Item '{SCRIPT}').LastWriteTime = [datetime]::new({TICKS})" in recipe
    assert f"(Get-Item '{SCRIPT}').CreationTime = [datetime]::new({TICKS})" in recipe
    assert f"(Get-Item '{SCRIPT}').LastAccessTime = [datetime]::new({TICKS})" in recipe


def test_the_timestomp_check_stays_under_the_command_cap(tmp_path, pinned_names):
    """Enumerating every stamped path pushed the check past the gate's 200-char cap:
    artifact health rejected it and the whole cycle died (live: 6 cycles lost)."""
    long1 = ("C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\"
             "DevmanSweep.cmd")
    long2 = "C:\\ProgramData\\netshield\\NetshieldStore-run.cmd"
    out = "".join(f"TS|{p}|{TICKS}|{TICKS}|{TICKS}\n" for p in (SCRIPT, long1, long2))
    ctx = win_ctx(tmp_path, scripted(FakeSSH(), out=out), seeded=True)
    ctx.world.add_artifact(WIN_IP, "persist-seed", "helper id=long1",
                           check_cmd="if ($?) { exit 0 } else { exit 1 }",
                           paths=[long1])
    ctx.world.add_artifact(WIN_IP, "persist-seed", "helper id=long2",
                           check_cmd="if ($?) { exit 0 } else { exit 1 }",
                           paths=[long2])
    result = execute("persist", {"ip": WIN_IP, "family": "timestomp"}, ctx)
    assert result.ok, result.detail
    mine = [a for a in ctx.world.artifacts(WIN_IP)
            if a["kind"] == "persist-timestomp"]
    assert len(mine) == 1
    assert len(mine[0]["check_cmd"]) <= 200, mine[0]["check_cmd"]
    assert_checkable(mine[0])


def test_decoy_creates_the_bait_service(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "decoy"}, ctx)
    assert result.ok, result.detail
    script = (f"if (Get-Service -Name '{DECOY}' -ErrorAction SilentlyContinue) {{ "
              f"sc.exe config '{DECOY}' binPath= C:\\Windows\\System32\\cmd.exe "
              "start= auto | Out-Null } "
              f"else {{ sc.exe create '{DECOY}' binPath= C:\\Windows\\System32\\cmd.exe "
              "type= own start= auto | Out-Null }; "
              f"sc.exe description '{DECOY}' Backdoor agent | Out-Null; "
              f"sc.exe start '{DECOY}' | Out-Null; "
              f"sc.exe query '{DECOY}' >$null; {VERIFY}")
    assert_planted(ssh, script)

    artifact = ctx.world.artifacts(WIN_IP)[0]
    assert artifact["kind"] == "persist-decoy"
    assert "bait" in artifact["detail"]
    assert_checkable(artifact)
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"sc.exe delete '{DECOY}'" in recipe


def test_hide_covers_every_planted_path(tmp_path, pinned_names):
    other = f"{WATCH_DIR}\\{TASK}.exe"
    ssh = scripted(FakeSSH(), out=f"HIDDEN|{SCRIPT}\nHIDDEN|{other}\n")
    ctx = win_ctx(tmp_path, ssh, seeded=True)
    ctx.world.add_artifact(WIN_IP, "persist-seed-two",
                           "watchdog payload id=seed2", check_cmd="exit 0",
                           paths=[other])
    result = execute("persist", {"ip": WIN_IP, "family": "hide"}, ctx)
    assert result.ok, result.detail
    mine = [a for a in ctx.world.artifacts(WIN_IP) if a["kind"] == "persist-hide"]
    assert mine[0]["paths"] == [SCRIPT, other]
    assert check_hides(mine[0]["check_cmd"], SCRIPT)
    assert check_hides(mine[0]["check_cmd"], other)
    recipe = ctx.world.restore_table()[0]["recipe"]
    assert f"attrib -h -s {SCRIPT}" in recipe and f"attrib -h -s {other}" in recipe


def check_hides(check_cmd, path):
    return f"(Get-Item '{path}' -Force).Attributes.ToString().Contains('Hidden')" \
        in check_cmd


def test_hide_reports_failure_when_nothing_ends_up_hidden(tmp_path, pinned_names):
    ssh = scripted(FakeSSH(), rc=0, out="")
    ctx = win_ctx(tmp_path, ssh, seeded=True)
    result = execute("persist", {"ip": WIN_IP, "family": "hide"}, ctx)
    assert result.ok is False and "hide on" in result.detail
    assert ctx.world.artifacts(WIN_IP)[0]["kind"] == "persist-seed"


def test_timestomp_reports_failure_without_its_own_evidence(tmp_path, pinned_names):
    ssh = scripted(FakeSSH(), rc=0, out="")
    ctx = win_ctx(tmp_path, ssh, seeded=True)
    result = execute("persist", {"ip": WIN_IP, "family": "timestomp"}, ctx)
    assert result.ok is False and "timestomp on" in result.detail
    assert ctx.world.restore_table() == []


def test_a_second_run_reasserts_without_a_duplicate_artifact(tmp_path,
                                                             pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    first = execute("persist", {"ip": WIN_IP, "family": "scheduled_task"}, ctx)
    assert first.ok and first.data["family"] == "scheduled_task"
    assert len(ctx.world.artifacts(WIN_IP)) == 1

    # persist.py refuses to re-plant a standing family without force=true
    refused = execute("persist", {"ip": WIN_IP, "family": "scheduled_task"}, ctx)
    assert refused.ok is False and "already standing" in refused.detail

    again = execute("persist", {"ip": WIN_IP, "family": "scheduled_task",
                                "force": "true"}, ctx)
    assert again.ok, again.detail
    assert "re-asserted" in again.detail
    assert len(ctx.world.artifacts(WIN_IP)) == 1, "re-plant duplicated the artifact"
    assert len(ssh.calls) == 2
    assert ssh.calls[1]["cmd"] == ssh.calls[0]["cmd"], \
        "the re-assert must be the same idempotent install script"
    decoded = decode(ssh.calls[0]["cmd"])
    assert "/F" in decoded and f"schtasks /Query /TN '{TASK}'" in decoded


def test_a_plant_that_does_not_hold_is_ok_false(tmp_path, pinned_names):
    ssh = scripted(FakeSSH(), rc=1, err="Access is denied.")
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "service"}, ctx)
    assert result.ok is False
    assert "rc=1" in result.detail and "Access is denied" in result.detail
    assert ctx.world.artifacts(WIN_IP) == []
    assert ctx.world.restore_table() == [], "nothing changed -> nothing to undo"


def test_a_transport_failure_is_a_failed_result_not_a_crash(tmp_path, pinned_names):
    ssh = FakeSSH().on(lambda call: True, rc="raise")
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "wmisub"}, ctx)
    assert result.ok is False
    assert "TransportError" in result.detail and "wmisub" in result.detail
    assert ctx.world.artifacts(WIN_IP) == []


def test_a_family_failure_does_not_poison_the_next_family(tmp_path, pinned_names):
    # first-match rule: the SERVICE rule must precede the catchall
    ssh = FakeSSH().on(lambda call: SERVICE in decode(call["cmd"]), rc=1,
                       err="denied")
    ssh.on(lambda call: True, rc=0)
    ctx = win_ctx(tmp_path, ssh)
    assert execute("persist", {"ip": WIN_IP, "family": "service"}, ctx).ok is False
    later = execute("persist", {"ip": WIN_IP, "family": "decoy"}, ctx)
    assert later.ok, later.detail
    assert ctx.world.artifacts(WIN_IP)[0]["kind"] == "persist-decoy"


def test_an_unknown_family_is_a_precondition_rejection(tmp_path, pinned_names):
    ssh = FakeSSH()
    ctx = win_ctx(tmp_path, ssh)
    result = execute("persist", {"ip": WIN_IP, "family": "cron"}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert "unknown persistence family" in result.detail
    assert ssh.calls == []


def test_a_user_foothold_is_a_precondition_rejection(tmp_path, pinned_names):
    ssh = FakeSSH()
    ctx = win_ctx(tmp_path, ssh, admin=False)
    result = execute("persist", {"ip": WIN_IP, "family": "runkey"}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert ssh.calls == []


def test_the_handler_itself_refuses_a_non_admin_foothold(tmp_path, pinned_names):
    """The seed pipeline calls the handler, not the effect."""
    ssh = FakeSSH()
    ctx = win_ctx(tmp_path, ssh, admin=False)
    result = pw.apply(ctx, WIN_IP, "runkey", dict(NAMES))
    assert result.ok is False and "Administrator" in result.detail
    assert ssh.calls == []


def test_the_handler_answers_an_unknown_family_without_raising(tmp_path,
                                                               pinned_names):
    ctx = win_ctx(tmp_path, FakeSSH())
    result = pw.apply(ctx, WIN_IP, "no_such_family", dict(NAMES))
    assert result.ok is False and "no such family" in result.detail


def test_a_linux_family_is_not_offered_on_a_windows_box(tmp_path, pinned_names):
    ssh = FakeSSH()
    ctx = win_ctx(tmp_path, ssh)
    for family in ("systemd_unit", "sshd_dropin", "preload"):
        result = execute("persist", {"ip": WIN_IP, "family": family}, ctx)
        assert result.ok is False and "unknown persistence family" in result.detail
    assert ssh.calls == []


def test_auto_walks_the_order_and_skips_standing_families(tmp_path, pinned_names):
    ssh = scripted(FakeSSH())
    ctx = win_ctx(tmp_path, ssh)
    first = execute("persist", {"ip": WIN_IP, "family": "auto"}, ctx)
    assert first.ok and first.data["family"] == "scheduled_task"
    second = execute("persist", {"ip": WIN_IP, "family": "auto"}, ctx)
    assert second.ok and second.data["family"] == "defrun", second.detail
    standing = {a["family"] for a in ctx.world.artifacts(WIN_IP)}
    assert {"scheduled_task", "defrun"} <= standing


@pytest.mark.parametrize("mode", ["full", "stem", "suffix"])
def test_every_family_plants_with_the_real_naming_draw(tmp_path, mode):
    """Drift guard: works with whatever artifactnames draws, in every naming mode."""
    for family in sorted(pw.FAMILIES):
        seeded = family in ("hide", "timestomp")
        out = (f"HIDDEN|{SCRIPT}\n" if family == "hide"
               else (TS_OUT if family == "timestomp" else ""))
        ssh = scripted(FakeSSH(), out=out)
        ctx = make_ctx(tmp_path / f"{mode}-{family}", ssh=ssh,
                       cfg=base_cfg(naming={"mode": mode},
                                    red={"assume_breach": "off"},
                                    run={"host_guard": False}))
        ctx.world.add_foothold(WIN_IP, "Administrator", PASSWORD, "root",
                               windows=True)
        if seeded:
            ctx.world.add_artifact(WIN_IP, "persist-seed", "seed id=seed",
                                   check_cmd="exit 0", paths=[SCRIPT])
        result = execute("persist", {"ip": WIN_IP, "family": family}, ctx)
        assert result.ok, f"{family} in {mode} mode: {result.detail}"
        mine = [a for a in ctx.world.artifacts(WIN_IP)
                if a["kind"] != "persist-seed"]
        assert len(mine) == 1, f"{family}/{mode}: {mine}"
        assert mine[0]["family"] == family
        assert_checkable(mine[0])
        assert ctx.world.restore_table()[0]["recipe"][-1] == \
            f"# planted by persist family={family} mode={mode}"


@pytest.mark.parametrize("family", sorted(pw.FAMILIES))
def test_every_family_records_a_checkable_artifact_with_a_restore(tmp_path, family,
                                                                 pinned_names):
    """The restore table is each family's record projected; a missing check_cmd or undo is a blind spot."""
    seeded = family in ("hide", "timestomp")
    out = HIDDEN_OUT if family == "hide" else (TS_OUT if family == "timestomp" else "")
    ssh = scripted(FakeSSH(), out=out)
    ctx = win_ctx(tmp_path, ssh, seeded=seeded)
    result = execute("persist", {"ip": WIN_IP, "family": family}, ctx)
    assert result.ok, result.detail

    mine = [a for a in ctx.world.artifacts(WIN_IP) if a["kind"] != "persist-seed"]
    assert len(mine) == 1, mine
    artifact = mine[0]
    assert artifact["family"] == family
    assert_checkable(artifact)

    recipes = ctx.world.restore_table()
    assert len(recipes) == 1
    recipe = recipes[0]
    assert recipe["ip"] == WIN_IP and recipe["effect"] == "persist"
    assert recipe["recipe"], "every mutation must carry its undo"
    assert recipe["recipe"][-1] == (f"# planted by persist family={family} "
                                    "mode=full")
    if family not in ("hide", "timestomp"):
        name = re.search(r"id=(\S+)", artifact["detail"]).group(1)
        assert name in " ".join(recipe["recipe"]), \
            "the undo recipe must name the artifact it takes back"
