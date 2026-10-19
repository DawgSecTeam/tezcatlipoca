"""rotate_credlist honesty: ok=True only when the new credential authenticates and the old one does not; red's own accounts are never chosen and secret values never reach the blue-visible recipe or notes."""

import re
import shlex

from tests.helpers import FakeSSH, base_cfg, make_ctx
from tlaloc.effects import artifactnames as names
from tlaloc.effects import execute, rotate_credlist as rc
from tlaloc.policy import Policy

IP = "192.168.1.10"        # web01, Blue1, linux — credlist: ubuntu, svc-backup, analyst
WIN_IP = "192.168.1.20"    # dc01, Blue1, windows
USER = "svc-backup"        # the account intel knows ground truth for
ORIGINAL = "Backup1!"
PARAMS = {"ip": IP}


def rooted_ctx(tmp_path, ssh, clock=0.6, windows=False, cfg_over=None,
               dry_run=False):
    ctx = make_ctx(tmp_path, cfg=base_cfg(**(cfg_over or {})), ssh=ssh,
                   dry_run=dry_run)
    if windows:
        ctx.world.add_foothold(WIN_IP, "Administrator", "boxpw-1", "root",
                               windows=True)
    else:
        ctx.world.add_foothold(IP, "ubuntu", "boxpw-1", "root", escalation="sudo")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = (ctx.now()
                                     - clock * ctx.cfg["red"]["duration_min"] * 60)
    ctx.world.save()
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors,
                        rng=ctx.rng)
    return ctx


def old_credential_rejected(ssh):
    return ssh.on(lambda call: call["user"] == USER
                  and call["password"] == ORIGINAL,
                  rc=1, out="", err="Permission denied (password).")


def test_dry_run_plans_without_touching_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = rooted_ctx(tmp_path, ssh, dry_run=True)
    result = execute("rotate_credlist", dict(PARAMS), ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.sabotage() == [] and ctx.world.impacts() == []


def test_reds_own_accounts_are_never_eligible(tmp_path):
    from tests.helpers import FakeSSH
    ctx = rooted_ctx(tmp_path, FakeSSH())
    fh = ctx.world.foothold(IP)
    assert rc.eligible_users(ctx, IP, fh) == ["svc-backup", "analyst"]
    for off_limits in ("root", "ubuntu", "Administrator", "admin"):
        assert rc.protected(off_limits, ctx, fh), off_limits
    # a planted persistence account is ours too, and must survive
    planted = f"svc-{names.LINUX_STEMS[0]}"
    assert names.is_planted_account(planted)
    assert rc.protected(planted, ctx, fh)


def test_refuses_to_rotate_the_footholds_own_account(tmp_path):
    ssh = FakeSSH()
    ctx = rooted_ctx(tmp_path, ssh)
    result = execute("rotate_credlist", {"ip": IP, "user": "ubuntu"}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "refusing to rotate" in result.detail
    assert ssh.calls == []
    assert ctx.world.sabotage() == []


def test_rotation_breaks_the_old_credential_and_records_the_restore(tmp_path):
    ssh = FakeSSH()
    old_credential_rejected(ssh)          # everything else authenticates
    ctx = rooted_ctx(tmp_path, ssh)
    result = execute("rotate_credlist", dict(PARAMS), ctx)

    assert result.ok, result.detail
    assert "no longer authenticates" in result.detail

    sabotage = ctx.world.sabotage()
    assert len(sabotage) == 1
    recorded = sabotage[0]
    assert recorded["user"] == USER and recorded["ip"] == IP
    assert recorded["old_password"] == ORIGINAL
    new = recorded["new_password"]
    assert new and new != ORIGINAL

    rotate = [c for c in ssh.calls if "chpasswd" in (c["cmd"] or "")]
    assert len(rotate) == 1
    assert rotate[0]["user"] == "ubuntu"          # root path, not the target
    assert rotate[0]["cmd"] == "sudo -n sh -c " + shlex.quote(
        f"echo '{USER}:{new}' | chpasswd")

    assert any(c["user"] == USER and c["password"] == ORIGINAL for c in ssh.calls)
    assert any(c["user"] == USER and c["password"] == new for c in ssh.calls)

    # red keeps working the account
    creds = [c for c in ctx.world.valid_creds(IP) if c["user"] == USER]
    assert any(c["password"] == new and c["source"] == "rotate_credlist"
               for c in creds)

    # the impact names the account whose auth check now fails
    impacts = ctx.world.impacts()
    assert len(impacts) == 1
    assert impacts[0]["service"] == USER and impacts[0]["mode"] == "rotate_credlist"

    # the restore record carries both values, organizer-side only
    assert result.restore["secrets"] and ORIGINAL in result.restore["secrets"]
    assert new in result.restore["secrets"]
    assert ctx.world.restore_table()[0]["secrets"] == result.restore["secrets"]
    recipe = " ; ".join(result.restore["recipe"])
    assert USER in recipe
    assert ORIGINAL not in recipe and new not in recipe, \
        "the recipe is rendered in the blue-visible report — values are secrets"

    notes = " ".join(ctx.world.target(IP)["notes"])
    assert ORIGINAL not in notes and new not in notes

    artifact = ctx.world.artifacts()[0]
    assert artifact["kind"] == "impact-rotate_credlist"
    assert USER in artifact["detail"]


def test_an_original_that_still_authenticates_is_not_an_impact(tmp_path):
    """The change did not hold: ok=False with no impact, but the real mutation's record still lands."""
    ssh = FakeSSH()                        # unmatched calls return rc=0
    ctx = rooted_ctx(tmp_path, ssh)
    result = execute("rotate_credlist", dict(PARAMS), ctx)

    assert result.ok is False
    assert "did not hold" in result.detail
    assert ctx.world.impacts() == []
    assert len(ctx.world.sabotage()) == 1
    assert ctx.world.restore_table()[0]["effect"] == "rotate_credlist"
    assert ctx.world.artifacts()[0]["kind"] == "impact-rotate_credlist"


def test_a_new_credential_that_does_not_authenticate_is_not_an_impact(tmp_path):
    ssh = FakeSSH().on(lambda call: call["user"] == USER and call["cmd"] == "id",
                       rc=1, out="")
    ctx = rooted_ctx(tmp_path, ssh)
    result = execute("rotate_credlist", dict(PARAMS), ctx)

    assert result.ok is False
    assert "NEW credential does not authenticate" in result.detail
    assert ctx.world.impacts() == []
    assert len(ctx.world.sabotage()) == 1


def test_transport_failure_leaves_the_box_alone(tmp_path):
    ssh = FakeSSH().on("chpasswd", rc="raise")
    ctx = rooted_ctx(tmp_path, ssh)
    result = execute("rotate_credlist", dict(PARAMS), ctx)

    assert result.ok is False
    assert "rotation failed" in result.detail and "TransportError" in result.detail
    assert ctx.world.sabotage() == []
    assert ctx.world.impacts() == []
    assert ctx.world.restore_table() == []


def test_a_chpasswd_that_fails_records_nothing(tmp_path):
    ssh = FakeSSH().on("chpasswd", rc=1, out="",
                       err="chpasswd: Authentication token manipulation error")
    ctx = rooted_ctx(tmp_path, ssh)
    result = execute("rotate_credlist", dict(PARAMS), ctx)

    assert result.ok is False
    assert "chpasswd rc=1" in result.detail
    assert ctx.world.sabotage() == []
    assert ctx.world.impacts() == [] and ctx.world.restore_table() == []


def test_no_eligible_account_is_a_precondition_rejection(tmp_path):
    ssh = FakeSSH()
    ctx = rooted_ctx(tmp_path, ssh)
    # only red's own accounts are left on this box
    ctx.intel._files["users.json"] = {"box_username": "ubuntu",
                                      "credlist_usernames": ["ubuntu", "root"]}
    result = execute("rotate_credlist", dict(PARAMS), ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "no credlist account" in result.detail
    assert ssh.calls == []


def test_locked_until_the_unrecoverable_tier_is_reached(tmp_path):
    ssh = FakeSSH()
    ctx = rooted_ctx(tmp_path, ssh, clock=0.3)
    result = execute("rotate_credlist", dict(PARAMS), ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "unrecoverable" in result.detail
    assert ssh.calls == []


def test_needs_a_root_path(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, ssh=ssh)
    ctx.world.add_foothold(IP, "analyst", "pw", "user")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = ctx.now() - 0.6 * ctx.cfg["red"]["duration_min"] * 60
    ctx.world.save()
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, rng=ctx.rng)
    result = execute("rotate_credlist", dict(PARAMS), ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "root path" in result.detail
    assert ssh.calls == []


def test_the_config_switch_is_honoured(tmp_path):
    ssh = FakeSSH()
    ctx = rooted_ctx(tmp_path, ssh, cfg_over={"effects": {"rotate_credlist": False}})
    result = execute("rotate_credlist", dict(PARAMS), ctx)
    assert result.ok is False
    assert "switched off" in result.detail
    assert ssh.calls == []


def test_windows_rotation_uses_net_user(tmp_path):
    ssh = FakeSSH().on(lambda call: call["user"] == USER
                       and call["password"] == ORIGINAL, rc=1, out="")
    ctx = rooted_ctx(tmp_path, ssh, windows=True)
    result = execute("rotate_credlist", {"ip": WIN_IP}, ctx)

    assert result.ok, result.detail
    new = ctx.world.sabotage()[0]["new_password"]
    rotate = [c for c in ssh.calls if (c["cmd"] or "").startswith("net user")]
    assert rotate, [c["cmd"] for c in ssh.calls]
    assert rotate[-1]["cmd"] == f"net user {USER} {new}"
    assert rotate[-1]["windows"] is True
    assert ctx.world.impacts()[0]["service"] == USER
    assert re.search(r"(?i)net user|chpasswd", " ".join(result.restore["recipe"]))
