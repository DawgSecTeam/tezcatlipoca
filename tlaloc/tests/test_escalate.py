"""escalate — one test per mechanism/label family (sudo, sudo_pw, escape:*,
capexec:*, system), plus the no-op, the give-up paths and the precondition ones.

The fixtures' comp-min plants `sudo-nopasswd` on 192.168.1.10 and a Windows DC
on 192.168.1.20, so the intel hint is deterministic in these tests.
"""

import json

from tests.helpers import FakeSSH, make_ctx
from tlaloc.effects import execute
from tlaloc.effects.base import can_root, root_cmd

IP = "192.168.1.10"
WIN = "192.168.1.20"

ROOT_ID = "uid=0(root) gid=0(root) groups=0(root)"
SUID_ID = "uid=1000(analyst) gid=1000(analyst) euid=0(root) groups=0(root)"


def _ctx(tmp_path, ssh, ip=IP, user="analyst", password="boxpw-1", kind="user",
         escalation=None, windows=False):
    ctx = make_ctx(tmp_path, ssh=ssh)
    ctx.world.add_foothold(ip, user, password, kind, windows=windows,
                           escalation=escalation)
    return ctx


def _notes(ctx, ip=IP):
    return ctx.world.target_record(ip)["notes"]


# --- dry-run ------------------------------------------------------------------

def test_dry_run_plans_without_touching_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, ssh=ssh, dry_run=True)
    ctx.world.add_foothold(IP, "analyst", "boxpw-1", "user")
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []


# --- label: sudo --------------------------------------------------------------

def test_passwordless_sudo_is_discovered_proved_and_recorded(tmp_path):
    ssh = FakeSSH().on("sudo -n id", out=ROOT_ID)
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok, result.detail
    # the proving command IS the routing command: exactly one, and reproducible
    assert ssh.calls[0]["cmd"] == "sudo -n id"
    assert ssh.sent("sudo -n id") == ["sudo -n id"]
    fh = ctx.world.foothold(IP)
    assert fh["escalation"] == "sudo" and fh["kind"] == "root"
    assert can_root(fh)
    assert root_cmd("id", fh) == ssh.calls[0]["cmd"]
    assert "sudo" in result.detail and result.data["label"] == "sudo"
    # the ledger says why, and the undo is recorded
    assert any("escalate: intel hint: escalation label 'sudo'" in n for n in _notes(ctx))
    assert any("escalate: sudo proved root" in n or "escalate: passwordless sudo" in n
               for n in _notes(ctx))
    table = ctx.world.restore_table()
    assert table and table[0]["effect"] == "escalate" and table[0]["recipe"]
    assert ctx.world.impacts() == []          # an escalation is not an impact


# --- label: sudo_pw -----------------------------------------------------------

def test_password_sudo_uses_the_foothold_credential_and_never_leaks_it(tmp_path):
    ssh = FakeSSH().on("| sudo -S", out=ROOT_ID)
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok, result.detail
    assert ssh.sent("sudo -S") == ["echo 'boxpw-1' | sudo -S -p '' id"]
    assert ctx.world.foothold(IP)["escalation"] == "sudo_pw"
    assert result.data["label"] == "sudo_pw"
    blob = json.dumps({"detail": result.detail, "data": result.data})
    assert "boxpw-1" not in blob, "the password must not enter the audit log"

    assert "boxpw-1" not in json.dumps(ctx.world.restore_table())


def test_sudo_pw_is_skipped_without_a_password(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh, password="")
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert "sudo_pw" in result.detail
    assert not ssh.sent("sudo -S")


# --- label: escape:* (SUID escape binary) -------------------------------------

def test_suid_shell_escape_becomes_a_routable_label(tmp_path):
    ssh = FakeSSH()
    ssh.on("find / -xdev -perm -4000", out="/usr/bin/bash\n/usr/bin/find\n")
    ssh.on("/usr/bin/bash -p -c id", out=SUID_ID)
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok, result.detail
    label = "escape:/usr/bin/bash -p -c"
    assert ctx.world.foothold(IP)["escalation"] == label
    assert ssh.sent("/usr/bin/bash -p -c id") == ["/usr/bin/bash -p -c id"]
    assert root_cmd("id", ctx.world.foothold(IP)) == "/usr/bin/bash -p -c id"
    assert "escape:" in label and result.data["family"] == "suid"
    # the non-routable ones are still reported for improvise
    assert any("/usr/bin/find" in n for n in result.data["notes"])
    assert any("one-shot" in n for n in result.data["notes"])


def test_a_suid_binary_that_does_not_return_root_is_not_a_label(tmp_path):
    ssh = FakeSSH()
    ssh.on("find / -xdev -perm -4000", out="/usr/bin/bash\n")
    ssh.on("/usr/bin/bash -p -c id", out="uid=1000(analyst) gid=1000(analyst)")
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert ctx.world.foothold(IP)["escalation"] is None
    assert any("did not return root" in n for n in result.data["notes"])


def test_one_shot_only_escape_is_reported_but_never_labelled(tmp_path):
    """v1 recorded escape:find; the router appends the command, so find cannot
    round-trip. The box is rooted and the ledger says so — without a lie."""
    ssh = FakeSSH()
    ssh.on("find / -xdev -perm -4000", out="/usr/bin/find\n")
    ssh.on("-exec /bin/sh -p -c", out=ROOT_ID)
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert "one-shot" in result.detail and "/usr/bin/find" in result.detail
    assert ctx.world.foothold(IP)["escalation"] is None
    assert not can_root(ctx.world.foothold(IP))
    assert any("PROVES root" in n for n in result.data["notes"])
    assert any("not routable" in n for n in _notes(ctx))


# --- label: capexec:* (cap_setuid binary) -------------------------------------

def test_cap_setuid_binary_becomes_a_capexec_label(tmp_path):
    ssh = FakeSSH()
    ssh.on("getcap -r /", out="/usr/lib/.an1d cap_setuid=ep\n")
    ssh.on("/usr/lib/.an1d id", out=ROOT_ID)
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok, result.detail
    assert ctx.world.foothold(IP)["escalation"] == "capexec:/usr/lib/.an1d"
    assert result.data["family"] == "capexec"
    assert ssh.sent("/usr/lib/.an1d id") == ["/usr/lib/.an1d id"]


def test_cap_setuid_without_a_known_invocation_shape_is_not_labelled(tmp_path):
    ssh = FakeSSH()
    ssh.on("getcap -r /", out="/usr/local/bin/helper cap_setuid=ep\n")
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert ctx.world.foothold(IP)["escalation"] is None
    assert any("not routable" in n for n in result.data["notes"])


# --- label: system (Windows) --------------------------------------------------

def test_windows_administrator_takes_over_system_and_records_the_cleanup(tmp_path):
    ssh = FakeSSH().on("schtasks /Create", out="SYSTEM-OK")
    ctx = _ctx(tmp_path, ssh, ip=WIN, user="Administrator", password="boxpw-1",
               kind="user", windows=True)
    result = execute("escalate", {"ip": WIN}, ctx)
    assert result.ok, result.detail
    assert ctx.world.foothold(WIN)["escalation"] == "system"
    assert result.data["label"] == "system"
    script = ssh.calls[0]["cmd"]
    assert ssh.calls[0]["windows"] is True
    for needle in ("schtasks /Create", "/RU SYSTEM", "/SC ONCE", "whoami",
                   "C:\\Windows\\Temp\\", "schtasks /Delete", "Remove-Item"):
        assert needle in script, needle
    assert '"' not in script, "the cmd.exe wrap forbids double quotes"
    table = ctx.world.restore_table()
    assert table and "schtasks /Delete" in " ".join(table[0]["recipe"])
    assert ctx.world.artifacts() == []       # the one-shot is not a standing plant
    assert any("SYSTEM" in n for n in _notes(ctx, WIN))


def test_windows_system_label_is_a_no_op_when_already_stood(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh, ip=WIN, user="Administrator", password="boxpw-1",
               kind="root", escalation="system", windows=True)
    result = execute("escalate", {"ip": WIN}, ctx)
    assert result.ok and "already SYSTEM" in result.detail
    assert ssh.calls == []


def test_windows_without_an_admin_session_has_no_label(tmp_path):
    ctx = _ctx(tmp_path, FakeSSH(), ip=WIN, user="analyst", kind="user", windows=True)
    result = execute("escalate", {"ip": WIN}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert "Administrator" in result.detail


# --- already root / stale label ----------------------------------------------

def test_a_standing_label_is_re_verified_and_nothing_is_touched(tmp_path):
    ssh = FakeSSH().on("sudo -n id", out=ROOT_ID)
    ctx = _ctx(tmp_path, ssh, user="ubuntu", kind="root", escalation="sudo")
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok and "re-verified" in result.detail
    assert [c["cmd"] for c in ssh.calls] == ["sudo -n id"]
    assert ctx.world.foothold(IP)["escalation"] == "sudo"
    assert ctx.world.restore_table() == []      # nothing changed


def test_a_root_foothold_with_no_label_is_a_no_op_success(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh, user="root", kind="root")
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok and "already root" in result.detail
    assert ssh.calls == [] and ctx.world.restore_table() == []


def test_a_stale_label_is_withdrawn_so_routing_cannot_ride_it(tmp_path):
    ssh = FakeSSH()          # nothing on this box answers with root any more
    ctx = _ctx(tmp_path, ssh, user="ubuntu", kind="root", escalation="sudo")
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    fh = ctx.world.foothold(IP)
    assert fh["escalation"] == "" and not can_root(fh)
    assert any("withdrew stale label" in n for n in result.data["notes"])
    assert any("withdrew stale label" in n for n in _notes(ctx))


# --- no label applies ---------------------------------------------------------

def test_no_label_applies_is_ok_false_with_the_discovery_in_the_notes(tmp_path):
    ssh = FakeSSH()          # every probe answers, none of them with root
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert "no escalation label applies" in result.detail
    assert result.data["notes"] and result.data["label"] is None
    assert ctx.world.foothold(IP)["escalation"] is None
    assert ctx.world.failure_count("escalate", IP) == 1
    assert ctx.world.restore_table() == []
    assert any("escalate:" in n for n in _notes(ctx))
    for family in ("sudo", "suid", "capexec"):
        assert family in result.detail, f"{family} must appear in the give-up detail"


def test_a_nonzero_return_code_is_never_a_label(tmp_path):
    ssh = FakeSSH().on("sudo -n id", rc=1, out="sudo: a password is required")
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert ctx.world.foothold(IP)["escalation"] is None
    assert any("rc=1" in n for n in result.data["notes"])


def test_the_family_order_follows_the_intel_hint(tmp_path, monkeypatch):
    ssh = FakeSSH()
    ssh.on("getcap -r /", out="/usr/lib/.x2d cap_setuid=ep\n")
    ssh.on("/usr/lib/.x2d id", out=ROOT_ID)
    ctx = _ctx(tmp_path, ssh, ip="192.168.1.11")     # db01: no sudo-nopasswd hint
    monkeypatch.setattr(ctx.intel, "hints_for",
                        lambda ip: {"escalation": "capexec:pending-discovery"})
    result = execute("escalate", {"ip": "192.168.1.11"}, ctx)
    assert result.ok, result.detail
    assert result.data["label"] == "capexec:/usr/lib/.x2d"
    assert "find / -xdev" in ssh.calls[0]["cmd"]      # binary families promoted
    assert "getcap" in ssh.calls[1]["cmd"]
    assert not ssh.sent("sudo -n id"), "the hinted family won before sudo was tried"


def test_mechanism_param_pins_one_family(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP, "mechanism": "sudo"}, ctx)
    assert result.ok is False
    assert ssh.sent("sudo -n id") == ["sudo -n id"]
    assert not ssh.sent("find /") and not ssh.sent("getcap")
    assert "no escalation label applies" in result.detail


def test_a_transport_blowup_is_ok_false_not_a_crash(tmp_path):
    ssh = FakeSSH().on("", rc="raise")
    ctx = _ctx(tmp_path, ssh)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False
    assert any("transport error" in n for n in result.data["notes"])
    assert ctx.world.foothold(IP)["escalation"] is None


# --- preconditions ------------------------------------------------------------

def test_no_foothold_is_a_precondition_rejection(tmp_path):
    ctx = make_ctx(tmp_path, ssh=FakeSSH())
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert ctx.world.reject_memory()


def test_escalate_stops_re_probing_a_box_with_no_root_path(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    for _ in range(3):
        ctx.world.tally_failure("escalate", IP)
    result = execute("escalate", {"ip": IP}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert ssh.calls == []


# --- the vocabulary itself -----------------------------------------------------

def test_emitted_labels_stay_inside_the_shared_routing_vocabulary():
    """The label vocabulary is v1's, and `can_root`/`root_cmd` must route it.

    `system` is the one exception by design: on Windows the routing is the
    admin-equivalent SESSION (as in takedown.windows_admin), not a wrapper.
    """
    from tlaloc.effects.base import ESCALATIONS
    for label in ("sudo", "sudo_pw", "escape:/usr/bin/bash -p -c",
                  "capexec:/usr/lib/.an1d"):
        assert label.startswith(ESCALATIONS), label
        assert can_root({"user": "analyst", "kind": "user", "escalation": label})
    assert not can_root({"user": "Administrator", "kind": "user",
                         "escalation": "system"})
    assert "system" not in ESCALATIONS
