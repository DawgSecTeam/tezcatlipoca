"""spray_access — the credential-spray port: candidates, caps, and honesty.

What these tests are for (the defects from the earlier agent this effect is
shaped by):

* candidates come from intel per box, not from a table in the module;
* the attempt cap and the per-login retry cap bound the work, and a cap hit with
  no working credential is ``ok=False`` (the earlier agent returned ``ok=True``
  there and the
  seed recorded `access` for boxes it had never entered);
* a box that stops answering costs two probes, not the whole cap;
* nothing is planted, so nothing needs an undo recipe.
"""

from tests.helpers import DEFAULT_COMP, FakeSSH, base_cfg, make_ctx
from tlaloc.effects import execute
from tlaloc.effects.spray_access import candidate_pairs
from tlaloc.intel.nakon import PAM_ANY_PASSWORD

FIXTURE = str(DEFAULT_COMP)
IP = "192.168.1.10"
WIN_IP = "192.168.1.20"


def _ctx(tmp_path, ssh, cfg=None, **over):
    """A live ctx on the fixture range, with the earlier agent's human-mimic
    delay switched off."""
    cfg = cfg or base_cfg(limits={"spray_delay_sec": 0})
    ctx = make_ctx(tmp_path, cfg=cfg, comp_dir=FIXTURE, ssh=ssh, compfile=False,
                   **over)
    ctx.world.ensure_event_start()
    return ctx


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

    return Silent(FIXTURE)


# --- candidates -----------------------------------------------------------------

def test_candidates_are_intel_facts_first_then_bounded_guesses(tmp_path):
    ctx = _ctx(tmp_path, FakeSSH())
    pairs = candidate_pairs(ctx, IP)
    facts = list(ctx.intel.candidate_pairs(IP))
    assert facts and pairs[:len(facts)] == facts, \
        "intel's per-box facts are tried before any generic guess"
    # nakon planted pam_permit on this box: any password authenticates, and that
    # is one of the facts — not something this module knows about.
    assert ("root", PAM_ANY_PASSWORD) in facts
    assert len(pairs) > len(facts), "the generic pool follows the exact pairs"


def test_windows_candidates_use_the_machine_account(tmp_path):
    ctx = _ctx(tmp_path, FakeSSH())
    pairs = candidate_pairs(ctx, WIN_IP)
    assert pairs[0] == ("Administrator", "boxpw-1")           # nakon machine account


# --- planning -------------------------------------------------------------------

def test_dry_run_plans_without_touching_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, comp_dir=FIXTURE, ssh=ssh, dry_run=True, compfile=False)
    result = execute("spray_access", {"ip": IP}, ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.foothold(IP) is None


# --- the happy path --------------------------------------------------------------

def test_spray_lands_a_foothold_with_the_exact_probe(tmp_path):
    ssh = FakeSSH()
    # the first ground-truth pair is refused, the next one works
    ssh.on(lambda call: call["user"] == "ubuntu", rc="raise",
           err="ssh ubuntu@192.168.1.10: Authentication failed.")
    ssh.on(lambda call: call["user"] == "svc-backup", rc=0, out="svc-backup\n")
    ctx = _ctx(tmp_path, ssh)
    pairs = candidate_pairs(ctx, IP)

    result = execute("spray_access", {"ip": IP}, ctx)

    assert result.ok, result.detail
    assert result.data["user"] == "svc-backup" and result.data["attempts"] == 2
    assert [call["cmd"] for call in ssh.calls] == ["id -un", "id -un"]
    assert [(call["user"], call["password"]) for call in ssh.calls] == list(pairs[:2])
    assert all(call["ip"] == IP and call["windows"] is False for call in ssh.calls)

    foothold = ctx.world.foothold(IP)
    assert foothold["user"] == "svc-backup" and foothold["kind"] == "user"
    assert foothold["password"] == pairs[1][1] and foothold["windows"] is False
    cred = ctx.world.valid_creds(IP)[0]
    assert cred["user"] == "svc-backup" and cred["password"] == pairs[1][1]
    assert cred["source"] == "spray_access:nakon"
    assert "svc-backup" in (ctx.world.target_record(IP)["notes"][-1])
    # a login is not a mutation: no artifact, no undo recipe
    assert ctx.world.artifacts(IP) == []
    assert ctx.world.restore_table() == []


def test_windows_boxes_probe_with_whoami(tmp_path):
    ssh = FakeSSH().on("whoami", rc=0, out="dc01\\Administrator\n")
    ctx = _ctx(tmp_path, ssh)
    result = execute("spray_access", {"ip": WIN_IP}, ctx)
    assert result.ok, result.detail
    call = ssh.calls[0]
    assert call["cmd"] == "whoami" and call["windows"] is True
    assert (call["user"], call["password"]) == ("Administrator", "boxpw-1")
    assert ctx.world.foothold(WIN_IP)["windows"] is True


# --- give-up paths ---------------------------------------------------------------

def test_a_cap_hit_is_not_a_success(tmp_path):
    ssh = FakeSSH().on("id -un", rc=0, out="")               # answers, never a login
    ctx = _ctx(tmp_path, ssh, cfg=base_cfg(limits={"spray_delay_sec": 0,
                                                   "cred_spray_max": 2}))
    result = execute("spray_access", {"ip": IP}, ctx)
    assert result.ok is False
    assert "attempt cap (2) hit" in result.detail
    assert len(ssh.calls) == 2
    assert ctx.world.foothold(IP) is None


def test_an_unreachable_box_costs_two_probes_and_fails(tmp_path):
    ssh = FakeSSH().on("id -un", rc="raise")
    ctx = _ctx(tmp_path, ssh)
    result = execute("spray_access", {"ip": IP}, ctx)
    assert result.ok is False
    assert "stopped answering SSH" in result.detail
    assert len(ssh.calls) == 2, "a dead box must not burn the whole cap"
    assert ctx.world.foothold(IP) is None


def test_a_login_at_the_retry_cap_is_skipped_not_hammered(tmp_path):
    ssh = FakeSSH().on(lambda call: call["user"] == "ubuntu", rc=0, out="ubuntu\n")
    ctx = _ctx(tmp_path, ssh, cfg=base_cfg(limits={"spray_delay_sec": 0,
                                                   "max_retries_per_service": 2}))
    ctx.world.count_cred_try(IP, "root")
    ctx.world.count_cred_try(IP, "root")
    result = execute("spray_access", {"ip": IP}, ctx)
    assert result.ok, result.detail
    assert ssh.calls and all(call["user"] != "root" for call in ssh.calls)
    assert result.data["user"] == "ubuntu"


# --- preconditions ---------------------------------------------------------------

def test_check_rejects_a_box_with_nothing_to_spray(tmp_path):
    ssh = FakeSSH()
    ctx = _ctx(tmp_path, ssh)
    ctx.intel = _silent_intel()
    result = execute("spray_access", {"ip": IP}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail
    assert ssh.calls == []
    assert ctx.world.reject_memory(), "the rejection is remembered for the arbiter"


def test_two_failures_switch_the_candidate_off(tmp_path):
    cfg = base_cfg(limits={"spray_delay_sec": 0, "cred_spray_max": 3})
    ctx = _ctx(tmp_path, FakeSSH().on("id -un", rc=0, out=""), cfg=cfg)
    assert execute("spray_access", {"ip": IP}, ctx).ok is False
    assert execute("spray_access", {"ip": IP}, ctx).ok is False
    assert ctx.world.failure_count("spray_access", IP) == 2

    ssh = FakeSSH().on("id -un", rc=0, out="ubuntu\n")
    ctx = _ctx(tmp_path, ssh)                # same state dir: the tally persists
    result = execute("spray_access", {"ip": IP}, ctx)
    assert result.ok is False and "precondition" in result.detail
    assert ssh.calls == [], "a burned box must not be sprayed again"
