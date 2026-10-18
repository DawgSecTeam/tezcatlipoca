"""lockout: the endgame denial, and the window that is the only thing unlocking it.

The ROE point: a locked account is an organizer restore, so this effect must be
refused unless the lockout window is open — outside it a lockout is indistinguishable
from vandalism. The tests also pin the two access-safety rules (red's own accounts
are never locked; on Windows the SSH path is only disabled when a live C2 channel
covers the box) and the read-back that makes `ok=True` mean the accounts are
actually locked.
"""

import shlex

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc.c2 import FakeMythic
from tlaloc.effects import execute, lockout as lk
from tlaloc.policy import Policy

IP = "192.168.1.10"        # web01, Blue1, linux
WIN_IP = "192.168.1.20"    # dc01, Blue1, windows
TARGETS = ["svc-backup", "analyst"]


def late_ctx(tmp_path, ssh, *, windows=False, c2=None, dry_run=False,
             remaining_sec=120, cfg_over=None):
    """A rooted box with the lockout window open (`red.windows.lockout`)."""
    ctx = make_ctx(tmp_path, cfg=base_cfg(**(cfg_over or {})), ssh=ssh,
                   dry_run=dry_run, c2=c2)
    if windows:
        ctx.world.add_foothold(WIN_IP, "Administrator", "boxpw-1", "root",
                               windows=True)
    else:
        ctx.world.add_foothold(IP, "ubuntu", "boxpw-1", "root", escalation="sudo")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = (
        ctx.now() - ctx.cfg["red"]["duration_min"] * 60 + remaining_sec)
    ctx.world.save()
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors,
                        rng=ctx.rng)
    return ctx


def lock_script(users):
    add = "; ".join(f"usermod -L {shlex.quote(u)} 2>/dev/null" for u in users)
    read = ("for u in " + " ".join(shlex.quote(u) for u in users) + "; do "
            "h=$(grep \"^$u:\" /etc/shadow 2>/dev/null | cut -d: -f2); "
            "if [ -z \"$h\" ]; then echo \"N $u\"; "
            "else case \"$h\" in \\!*|\\**) echo \"L $u\";; *) echo \"U $u\";; "
            "esac; fi; done")
    return add + "; " + read


def windows_script(users):
    add = "; ".join(f"net user {u} /active:no" for u in users)
    listing = ", ".join("'" + u + "'" for u in users)
    read = (f"foreach ($u in @({listing})) {{ "
            "$x = Get-LocalUser -Name $u -ErrorAction SilentlyContinue; "
            "if (-not $x) { 'N ' + $u } elseif ($x.Enabled) { 'U ' + $u } "
            "else { 'L ' + $u } }")
    return f"{add}; {read}"


# --- dry run ------------------------------------------------------------------

def test_dry_run_plans_without_touching_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = late_ctx(tmp_path, ssh, dry_run=True)
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.impacts() == [] and ctx.world.restore_table() == []


# --- the happy path -----------------------------------------------------------

def test_locking_blue_out_is_an_impact_with_the_unlock_recipe(tmp_path):
    ssh = FakeSSH().on("usermod -L", rc=0,
                       out="L svc-backup\nL analyst\n")
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)

    assert result.ok, result.detail
    assert "locked 2 login account(s)" in result.detail
    assert "svc-backup" in result.detail and "analyst" in result.detail
    assert result.data["unrecoverable"] is True

    # the exact command: one root shell, one usermod per account, read back
    assert ssh.calls[0]["cmd"] == "sudo -n sh -c " + shlex.quote(
        lock_script(TARGETS))
    assert ssh.calls[0]["user"] == "ubuntu"

    # the impact is recorded, and the undo is the organizer's unlock recipe
    impacts = ctx.world.impacts()
    assert len(impacts) == 1
    assert impacts[0]["service"] == "logins" and impacts[0]["mode"] == "lockout"
    assert result.restore["secrets"] is None
    assert result.restore["recipe"][1:] == ["usermod -U svc-backup",
                                            "usermod -U analyst"]
    assert ctx.world.restore_table()[0]["recipe"] == result.restore["recipe"]
    artifact = ctx.world.artifacts()[0]
    assert artifact["kind"] == "impact-lockout"
    assert "svc-backup" in artifact["detail"]
    assert "lockout: locked" in " ".join(ctx.world.target(IP)["notes"])


def test_the_any_password_caveat_is_stated(tmp_path):
    """web01 carries a pam_permit plant: a locked password is not the whole login
    path there, and the result has to say so rather than imply blue is out."""
    ssh = FakeSSH().on("usermod -L", rc=0, out="L svc-backup\nL analyst\n")
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok
    assert "accepts any/empty password" in result.detail


def test_accounts_absent_from_the_box_are_reported_not_claimed(tmp_path):
    ssh = FakeSSH().on("usermod -L", rc=0,
                       out="L svc-backup\nN analyst\n")
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok, result.detail
    assert "do not exist on this box" in result.detail and "analyst" in result.detail


# --- partial and unverifiable lockouts ----------------------------------------

def test_a_partial_lockout_is_not_an_impact_but_is_recorded(tmp_path):
    ssh = FakeSSH().on("usermod -L", rc=0, out="L svc-backup\nU analyst\n")
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)

    assert result.ok is False
    assert "only part of the lockout held" in result.detail
    assert ctx.world.impacts() == [], "a half lockout is not the objective"
    # the mutation is real, so the organizer record still lands
    assert len(ctx.world.artifacts()) == 1
    assert result.restore["recipe"][1:] == ["usermod -U svc-backup"]


def test_an_unreadable_state_records_the_undo_and_says_so(tmp_path):
    ssh = FakeSSH().on("usermod -L", rc=1, out="")   # the read-back told us nothing
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)

    assert result.ok is False
    assert "could not read the lock state back" in result.detail
    assert ctx.world.impacts() == []
    # the lock commands may have landed, so the undo (a no-op if they did not)
    # and the artifact are still recorded
    assert result.restore["recipe"][1:] == ["usermod -U svc-backup",
                                            "usermod -U analyst"]
    assert ctx.world.artifacts()[0]["kind"] == "impact-lockout"


def test_nothing_locked_is_a_failure(tmp_path):
    ssh = FakeSSH().on("usermod -L", rc=0, out="U svc-backup\nU analyst\n")
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok is False
    assert "no account could be locked" in result.detail
    assert ctx.world.impacts() == []


def test_transport_failure_is_ok_false(tmp_path):
    ssh = FakeSSH().on("usermod -L", rc="raise")
    ctx = late_ctx(tmp_path, ssh)
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok is False
    assert "TransportError" in result.detail
    assert ctx.world.impacts() == []


# --- the window is the gate ---------------------------------------------------

def test_the_window_closed_rejects_the_candidate(tmp_path):
    """The whole ROE point: an out-of-window lockout is a precondition failure, and
    the rejection is remembered (it is an organizer restore, not a free move)."""
    ssh = FakeSSH()
    ctx = late_ctx(tmp_path, ssh, remaining_sec=30 * 60)      # 30 min left
    assert ctx.policy.window_open("lockout") is False
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "window is not open" in result.detail
    assert ssh.calls == []
    assert ctx.world.reject_memory()


def test_without_the_roe_clock_the_lockout_is_refused(tmp_path):
    ssh = FakeSSH()
    ctx = late_ctx(tmp_path, ssh)
    ctx.policy = None
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "ROE clock" in result.detail
    assert ssh.calls == []


def test_needs_a_root_path(tmp_path):
    ssh = FakeSSH()
    ctx = late_ctx(tmp_path, ssh)
    ctx.world.add_foothold(IP, "analyst", "pw", "user")     # re-dropped unprivileged
    result = execute("lockout", {"ip": IP}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "root path" in result.detail
    assert ssh.calls == []


def test_root_and_reds_own_accounts_are_never_locked(tmp_path):
    ssh = FakeSSH()
    ctx = late_ctx(tmp_path, ssh)
    fh = ctx.world.foothold(IP)
    assert lk.off_limits("root", fh) is True
    assert lk.off_limits("ubuntu", fh) is True          # our foothold
    assert lk.off_limits("svc-backup", fh) is False
    result = execute("lockout", {"ip": IP, "users": ["root"]}, ctx)
    assert result.ok is False and "precondition" in result.detail


# --- windows ------------------------------------------------------------------

def test_windows_locks_administrator_only_with_a_live_channel(tmp_path):
    ssh = FakeSSH().on("net user Administrator /active:no", rc=0,
                       out="L Administrator\n")
    ctx = late_ctx(tmp_path, ssh, windows=True)
    # the channel that outlives the login: a fresh callback for this box
    ctx.c2 = FakeMythic({"dc01-1": {"seen_at": ctx.now(), "agent": "poseidon"}})
    result = execute("lockout", {"ip": WIN_IP, "users": ["Administrator"]}, ctx)

    assert result.ok, result.detail
    assert ssh.calls[0]["cmd"] == windows_script(["Administrator"])
    assert ssh.calls[0]["windows"] is True
    assert result.impact["service"] == "logins"
    assert result.restore["recipe"][1] == "net user Administrator /active:yes"
    assert ctx.world.restore_table()[0]["recipe"] == result.restore["recipe"]


def test_windows_refuses_to_disable_our_own_ssh_path_without_a_channel(tmp_path):
    ssh = FakeSSH()
    ctx = late_ctx(tmp_path, ssh, windows=True)
    result = execute("lockout", {"ip": WIN_IP, "users": ["Administrator"]}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail
    assert "live C2 channel" in result.detail
    assert ssh.calls == []


def test_windows_leaves_the_ssh_account_alone_when_other_logins_exist(tmp_path):
    """Without a channel the Windows box account is dropped from the plan, and the
    rest of the credlist is still locked (the earlier agent's keep_foothold_user
    case). `ubuntu`
    comes from users.json's credlist and is not on a Windows box at all, so the
    read-back reports it absent rather than pretending it was locked."""
    ssh = FakeSSH().on("net user ubuntu /active:no", rc=0,
                       out="N ubuntu\nL svc-backup\nL analyst\n")
    ctx = late_ctx(tmp_path, ssh, windows=True)
    result = execute("lockout", {"ip": WIN_IP}, ctx)
    assert result.ok, result.detail
    script = windows_script(["ubuntu", "svc-backup", "analyst"])
    assert ssh.calls[0]["cmd"] == script
    assert "Administrator" not in ssh.calls[0]["cmd"]
    assert "do not exist on this box" in result.detail
