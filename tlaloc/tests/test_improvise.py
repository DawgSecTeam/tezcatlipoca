"""improvise — what runs, what is attributed, and what counts as done.

Each test names the defect from the earlier agent it locks out: the substring
sniff that decided whether an improvised goal was impacty, and ``ok = any(rc ==
0)`` that declared an unverified command a success. The author is always a real
``tlaloc.llm.LLM`` with an injected poster (no socket, no model endpoint), so
the authoring path is exercised end to end rather than stubbed away.
"""

import json

import pytest

from tests.helpers import DEFAULT_COMP, FakeSSH, base_cfg, make_ctx
from tlaloc.decide import Decider, StubClef
from tlaloc.effects import execute
from tlaloc.errors import EffectParamError, GuardrailError
from tlaloc.exec.gate import danger_lint
from tlaloc.llm import LLM
from tlaloc.policy import Policy

IP = "192.168.1.10"
WIN_IP = "192.168.1.20"          # dc01 in the fixture: a Windows box
COMP = str(DEFAULT_COMP)


# --- scaffolding --------------------------------------------------------------

def author_cfg(**over):
    """A config with the authoring model enabled and the fixture's ROE."""
    base = {"llm": {"enabled": True, "base_url": "http://author.invalid"},
            "red": {"assume_breach": "off"}, "run": {"host_guard": False}}
    for key, value in over.items():
        base[key] = ({**base[key], **value}
                     if isinstance(value, dict) and isinstance(base.get(key), dict)
                     else value)
    return base_cfg(**base)


def attach_author(ctx, script, seen=None):
    """Hang a real LLM on the context's author seam (HTTP replaced by closure).

    `ctx.llm` is the seam this effect prefers (see `effects/improvise.llm_of`);
    production currently reaches the same client through `ctx.decider.llm`,
    which `decided()` wires.
    """
    def poster(url, body, timeout):
        if seen is not None:
            seen.append({"url": url, "body": body})
        return {"choices": [{"message": {"content": json.dumps(script)}}]}
    llm = LLM(ctx.cfg, poster)
    ctx.__dict__["llm"] = llm
    return llm


def decided(ctx, *, intent=None, verified=None, llm=None):
    """Wire a real Decider with a stubbed clef (recorded answers)."""
    answers = {}
    if intent is not None:
        other = next(i for i in ("takedown", "recon", "persistence", "benign")
                     if i != intent)
        answers["command_intent"] = {"type": "choice", "choice": intent,
                                     "probabilities": {intent: 0.95, other: 0.05}}
    if verified is not None:
        answers["step_verified"] = {"type": "noul", "noul": 0.95 if verified else 0.05}
    ctx.decider = Decider(ctx.cfg, ctx.world, ctx.log, clef=StubClef(answers),
                          llm=llm)
    return ctx.decider


def rooted(tmp_path, ssh=None, *, clock=0.25, cfg=None, policy=True, dry_run=False,
           ip=IP, windows=False):
    """A ctx with a foothold, an event clock, and (by default) a live Policy."""
    ctx = make_ctx(tmp_path, cfg=cfg or author_cfg(), ssh=ssh or FakeSSH(),
                   comp_dir=COMP, compfile=False, dry_run=dry_run)
    ctx.world.add_foothold(ip, "Administrator" if windows else "ubuntu",
                           "boxpw-1", "root", windows=windows,
                           escalation=None if windows else "sudo")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = (ctx.now() - clock * ctx.cfg["red"]["duration_min"]
                                     * 60)
    ctx.world.save()
    if policy:
        ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors,
                            rng=ctx.rng)
    return ctx


def spy_lint(ctx):
    """Record every `ensure_command` the gate is asked for, without changing it."""
    seen = []
    real = ctx.gate.ensure_command

    def record(cmd, os_name="linux"):
        seen.append((cmd, os_name))
        return real(cmd, os_name)
    ctx.gate.ensure_command = record
    return seen


# --- dry run -----------------------------------------------------------------

def test_dry_run_plans_without_authoring_or_touching_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = rooted(tmp_path, ssh, dry_run=True)
    seen = []
    attach_author(ctx, {"command": "id", "verify": "id -u"}, seen)

    result = execute("improvise", {"ip": IP, "goal": "restore momentum"}, ctx)

    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == [], "a dry run touched the box"
    assert seen == [], "a dry run asked the model for a command"
    assert ctx.world.impacts() == []


# --- the authored happy path ---------------------------------------------------

def test_authors_the_command_through_the_llm_and_runs_it_verbatim(tmp_path):
    ssh = FakeSSH()
    ssh.on("systemctl restart vsftpd", rc=0, out="")
    ssh.on("systemctl is-active vsftpd", rc=0, out="active")
    ctx = rooted(tmp_path, ssh)
    lints = spy_lint(ctx)
    seen = []
    # the production seam: build_context hangs the client on the decider
    decided(ctx, intent="benign", verified=True,
            llm=attach_author(ctx, {"command": "systemctl restart vsftpd",
                                    "verify": "systemctl is-active vsftpd",
                                    "why": "bring the ftp daemon back"}, seen))
    goal = "restart vsftpd and confirm it is listening"

    result = execute("improvise", {"ip": IP, "goal": goal}, ctx)

    assert result.ok, result.detail
    assert [c["cmd"] for c in ssh.calls] == ["systemctl restart vsftpd",
                                             "systemctl is-active vsftpd"]
    # the same lint primitive the template uses, on the target's OS, for text
    # that only existed after the model answered
    assert lints == [("systemctl restart vsftpd", "linux"),
                     ("systemctl is-active vsftpd", "linux")]
    prompt = seen[0]["body"]["messages"][0]["content"]
    assert f"Goal: {goal}" in prompt and "bash" in prompt and "JSON only" in prompt
    assert seen[0]["url"] == "http://author.invalid/chat/completions"
    assert result.data["intent"] == "benign" and result.data["verified"] is True
    assert ctx.world.impacts() == []
    assert ctx.world.restore_table()[0]["recipe"][0].endswith("systemctl restart vsftpd")
    assert any("improvise[" in note for note in ctx.world.target_record(IP)["notes"])


def test_success_needs_the_verification_step_not_an_exit_status(tmp_path):
    """rc=0 plus a verification that says 'no' is NOT a success (the earlier
    agent's defect)."""
    ssh = FakeSSH().on("systemctl restart vsftpd", rc=0, out="Unit restarted")
    ctx = rooted(tmp_path, ssh)
    decided(ctx, intent="benign", verified=False,
            llm=attach_author(ctx, {"command": "systemctl restart vsftpd",
                                    "verify": "systemctl is-active vsftpd"}))

    result = execute("improvise", {"ip": IP,
                                   "goal": "restart vsftpd and confirm it is listening"},
                     ctx)

    assert result.ok is False and "not verified" in result.detail
    assert result.data["rc"] == 0 and result.data["verified"] is False
    assert ctx.world.impacts() == []


def test_an_authored_round_with_no_verification_is_never_a_success(tmp_path):
    ssh = FakeSSH().on("systemctl restart vsftpd", rc=0, out="")
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "systemctl restart vsftpd"})

    result = execute("improvise", {"ip": IP,
                                   "goal": "restart vsftpd and confirm it is listening"},
                     ctx)

    assert result.ok is False and "nothing verified it" in result.detail
    assert result.data["verified"] is None


# --- attribution ---------------------------------------------------------------

def test_an_impacty_intent_sets_the_impact_row(tmp_path):
    ssh = FakeSSH()
    ssh.on("systemctl stop nginx", rc=0, out="")
    ssh.on("systemctl is-active nginx", rc=0, out="inactive")
    ctx = rooted(tmp_path, ssh)
    decided(ctx, intent="takedown", verified=True,
            llm=attach_author(ctx, {"command": "systemctl stop nginx",
                                    "verify": "systemctl is-active nginx"}))

    result = execute("improvise", {"ip": IP,
                                   "goal": "stop nginx and confirm it is down"}, ctx)

    assert result.ok, result.detail
    impact = ctx.world.impacts()[0]
    assert impact["service"] == "nginx"            # derived from the goal + intel
    assert impact["mode"] == "improvise" and impact["ip"] == IP


def test_a_benign_intent_records_no_impact(tmp_path):
    ssh = FakeSSH()
    ssh.on("cat /etc/motd", rc=0, out="welcome")
    ctx = rooted(tmp_path, ssh)
    decided(ctx, intent="benign", verified=True,
            llm=attach_author(ctx, {"command": "cat /etc/motd",
                                    "verify": "test -s /etc/motd"}))

    result = execute("improvise", {"ip": IP, "goal": "read the motd banner"}, ctx)

    assert result.ok, result.detail
    assert result.data["intent"] == "benign"
    assert ctx.world.impacts() == []


def test_the_deterministic_floor_attributes_without_a_decision_layer(tmp_path):
    """No decider: keyword_intent (word boundaries, not substrings) is the read."""
    ssh = FakeSSH().on("systemctl stop nginx", rc=0, out="")
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "systemctl stop nginx",
                        "verify": "systemctl is-active nginx"})

    result = execute("improvise", {"ip": IP,
                                   "goal": "stop nginx and confirm it is down",
                                   "service": "nginx"}, ctx)

    assert result.ok, result.detail
    assert result.data["intent"] == "takedown"
    assert result.data["intent_source"] == "deterministic"
    assert ctx.world.impacts()[0]["service"] == "nginx"


def test_a_takedown_intent_with_no_named_service_records_no_impact(tmp_path):
    """An impact row naming a service the box does not have is worse than none."""
    ssh = FakeSSH().on("systemctl stop zabbix", rc=0, out="")
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "systemctl stop zabbix",
                        "verify": "systemctl is-active zabbix"})

    result = execute("improvise", {"ip": IP,
                                   "goal": "stop zabbix and confirm it is down"}, ctx)

    assert result.ok, result.detail
    assert result.data["intent"] == "takedown" and ctx.world.impacts() == []
    assert any(e["kind"] == "improvise_impact_unattributed"
               for e in ctx.log.read())


# --- no model / authoring failures ---------------------------------------------

def test_no_author_available_is_a_failed_result_not_a_crash(tmp_path):
    ssh = FakeSSH()
    ctx = rooted(tmp_path, ssh, cfg=author_cfg(llm={"enabled": False}))

    result = execute("improvise", {"ip": IP, "goal": "restore momentum"}, ctx)

    assert result.ok is False
    assert "llm" in result.detail and "ladder" in result.detail
    assert ssh.calls == [] and ctx.world.impacts() == []
    assert ctx.world.failure_count("improvise", IP) == 1     # the loop can see it


def test_an_unparseable_authoring_reply_is_a_failed_result(tmp_path):
    ssh = FakeSSH()
    ctx = rooted(tmp_path, ssh)

    def poster(url, body, timeout):
        return {"choices": [{"message": {"content": "I would run `id` first"}}]}
    ctx.__dict__["llm"] = LLM(ctx.cfg, poster)

    result = execute("improvise", {"ip": IP, "goal": "restore momentum"}, ctx)

    assert result.ok is False and "authoring failed" in result.detail
    assert ssh.calls == []
    assert ctx.world.failure_count("improvise", IP) == 1


# --- command failures ----------------------------------------------------------

def test_a_failing_command_is_reported_with_its_rc(tmp_path):
    ssh = FakeSSH().on("systemctl restart vsftpd", rc=1,
                       err="Job for vsftpd.service failed")
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "systemctl restart vsftpd",
                        "verify": "systemctl is-active vsftpd"})

    result = execute("improvise", {"ip": IP,
                                   "goal": "restart vsftpd and confirm it is listening"},
                     ctx)

    assert result.ok is False and "rc=1" in result.detail
    assert "vsftpd.service failed" in result.detail
    assert ctx.world.impacts() == []
    assert ctx.world.failure_count("improvise", IP) == 1


def test_a_transport_failure_is_ok_false_not_an_exception(tmp_path):
    ssh = FakeSSH().on("systemctl restart vsftpd", rc="raise")
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "systemctl restart vsftpd",
                        "verify": "systemctl is-active vsftpd"})

    result = execute("improvise", {"ip": IP,
                                   "goal": "restart vsftpd and confirm it is listening"},
                     ctx)

    assert result.ok is False and "TransportError" in result.detail
    assert ctx.world.impacts() == []


# --- linting: nothing runs unscreened ------------------------------------------

def test_a_destructive_authored_command_never_reaches_the_box(tmp_path):
    ssh = FakeSSH()
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "rm -rf /var/www",
                        "verify": "test -d /var/www"})

    result = execute("improvise", {"ip": IP, "goal": "tidy up the web root"}, ctx)

    assert result.ok is False and "lint" in result.detail
    assert ssh.calls == [], "an unscreened command reached the transport"
    assert any(e["kind"] == "improvise_rejected" for e in ctx.log.read())


def test_a_destructive_caller_supplied_command_is_stopped_by_the_template(tmp_path):
    """`command` is a free-form param: the shared template screens it."""
    ssh = FakeSSH()
    ctx = rooted(tmp_path, ssh)
    attach_author(ctx, {"command": "id"})

    with pytest.raises(GuardrailError):
        execute("improvise", {"ip": IP, "command": "rm -rf /var/www"}, ctx)
    assert ssh.calls == []


def test_the_authored_command_is_linted_with_the_targets_os_pattern_set(tmp_path):
    """The audited gap: a POSIX-only lint in front of PowerShell."""
    windows_cmd = "Remove-Item -Recurse -Force C:\\Windows\\Temp\\tlaloc"
    assert danger_lint(windows_cmd, "linux") is None, \
        "pick a command the POSIX lint lets through"
    ssh = FakeSSH()
    ctx = rooted(tmp_path, ssh, ip=WIN_IP, windows=True)
    lints = spy_lint(ctx)
    attach_author(ctx, {"command": windows_cmd, "verify": "(Get-Process).Count"})

    result = execute("improvise", {"ip": WIN_IP, "goal": "clean the temp directory"},
                     ctx)

    assert result.ok is False and "lint" in result.detail
    assert ssh.calls == []
    assert lints and lints[0] == (windows_cmd, "windows")


def test_a_windows_target_rides_the_windows_transport(tmp_path):
    ssh = FakeSSH().on("Get-Service", rc=0, out="Running")
    ctx = rooted(tmp_path, ssh, ip=WIN_IP, windows=True)
    lints = spy_lint(ctx)
    attach_author(ctx, {"command": "Get-Service WinRM",
                        "verify": "(Get-Service WinRM).Status"})

    result = execute("improvise", {"ip": WIN_IP, "goal": "list the WinRM service"}, ctx)

    assert result.ok, result.detail
    assert [c["cmd"] for c in ssh.calls] == ["Get-Service WinRM",
                                             "(Get-Service WinRM).Status"]
    assert all(c["windows"] for c in ssh.calls)
    assert all(os_name == "windows" for _, os_name in lints)


# --- escalation ---------------------------------------------------------------

def test_as_root_rides_the_footholds_sudo_label(tmp_path):
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, cfg=author_cfg(), ssh=ssh, comp_dir=COMP, compfile=False)
    ctx.world.add_foothold(IP, "analyst", "boxpw-1", "user", escalation="sudo")
    attach_author(ctx, {"command": "id", "verify": "id -u"})

    result = execute("improvise", {"ip": IP, "goal": "restore momentum",
                                   "as_root": "true"}, ctx)

    assert result.ok, result.detail
    assert [c["cmd"] for c in ssh.calls] == ["sudo -n id", "sudo -n id -u"]


def test_as_root_never_rides_an_escape_wrapper(tmp_path):
    """Escape wrappers break on arbitrary quoting (effects/base.root_cmd)."""
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, cfg=author_cfg(), ssh=ssh, comp_dir=COMP, compfile=False)
    ctx.world.add_foothold(IP, "analyst", "boxpw-1", "user",
                           escalation="escape:python3 -c")
    attach_author(ctx, {"command": "id", "verify": "id -u"})

    result = execute("improvise", {"ip": IP, "goal": "restore momentum",
                                   "as_root": "true"}, ctx)

    assert result.ok, result.detail
    assert ssh.calls[0]["cmd"] == "id", "free-form text rode an escape wrapper"


# --- the shape of what a caller may ask for ------------------------------------

def test_a_caller_supplied_command_runs_verbatim(tmp_path):
    ssh = FakeSSH().on("id", rc=0, out="uid=1000(ubuntu)")
    ctx = rooted(tmp_path, ssh)

    result = execute("improvise", {"ip": IP, "command": "id"}, ctx)

    assert ssh.calls[0]["cmd"] == "id"
    assert result.ok is False and "nothing verified it" in result.detail


def test_a_caller_supplied_command_is_verified_by_the_decision_layer(tmp_path):
    ssh = FakeSSH().on("systemctl restart nginx", rc=0, out="")
    ctx = rooted(tmp_path, ssh, policy=False)
    decided(ctx, intent="benign", verified=True)

    result = execute("improvise", {"ip": IP, "command": "systemctl restart nginx"}, ctx)

    assert result.ok, result.detail
    assert [c["cmd"] for c in ssh.calls] == ["systemctl restart nginx"], \
        "no verification command was proposed, so none may be run"


def test_a_command_with_no_goal_at_all_is_a_shape_error(tmp_path):
    ctx = rooted(tmp_path, FakeSSH())
    with pytest.raises(EffectParamError):
        execute("improvise", {"ip": IP}, ctx)


# --- preconditions -------------------------------------------------------------

def test_improvise_needs_a_foothold(tmp_path):
    ctx = rooted(tmp_path, FakeSSH(), policy=False)
    ctx.world.drop_foothold(IP)

    result = execute("improvise", {"ip": IP, "goal": "restore momentum"}, ctx)

    assert result.ok is False and "precondition" in result.detail
    assert "foothold" in result.detail


def test_an_impacty_goal_obeys_the_pressure_cap(tmp_path):
    """The earlier agent gated improvise on the goal looking impacty; the floor
    still does."""
    ctx = rooted(tmp_path, FakeSSH(), clock=0.0)      # observe tier / grace window

    result = execute("improvise", {"ip": IP,
                                   "goal": "stop nginx and confirm it is down"}, ctx)

    assert result.ok is False and "precondition" in result.detail
    assert "impact move" in result.detail
    assert ctx.world.reject_memory()                  # the rejection is remembered


def test_a_third_attempt_on_the_same_goal_is_refused(tmp_path):
    ctx = rooted(tmp_path, FakeSSH(), policy=False)
    ctx.world.tally_failure("improvise", IP)
    ctx.world.tally_failure("improvise", IP)

    result = execute("improvise", {"ip": IP, "goal": "restore momentum"}, ctx)

    assert result.ok is False and "precondition" in result.detail
    assert "twice" in result.detail
