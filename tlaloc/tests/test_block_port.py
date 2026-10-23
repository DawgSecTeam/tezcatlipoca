"""block_port: the firewall-drop mechanisms, their honesty checks, and the undo."""

import re
import shlex

import pytest

from tests.helpers import FakeC2, FakeSSH, base_cfg, make_ctx
from tlaloc.effects import block_port as bp
from tlaloc.effects import execute
from tlaloc.policy import Policy

IP = "192.168.1.10"        # web01, Blue1, linux
WIN_IP = "192.168.1.20"    # dc01, Blue1, windows
PIN = {"ip": IP, "port": 3306, "display": "MySQL pin"}

# Back-references tie the activation's drawn nft table/chain across all three nft calls.
ADD_RE = re.compile(
    r"sudo -n sh -c 'iptables -I INPUT -p tcp --dport (?P<port>\d+) -j REJECT "
    r"2>/dev/null; echo i=\$\?; ufw insert 1 deny (?P=port)/tcp 2>/dev/null; "
    r"echo u=\$\?; nft add table inet (?P<table>[A-Za-z0-9_]+) 2>/dev/null; "
    r"nft '\"'\"'add chain inet (?P=table) (?P<chain>[A-Za-z0-9_]+) "
    r"\{ type filter hook input priority -10 ; \}'\"'\"' 2>/dev/null; "
    r"nft add rule inet (?P=table) (?P=chain) tcp dport (?P=port) reject "
    r"2>/dev/null; echo n=\$\?'")


def ctx_for(tmp_path, ssh, clock=0.6, windows=False, dry_run=False, c2=None):
    """A rooted box past the 0.5-progress gate that unlocks the unrecoverable tier."""
    ctx = make_ctx(tmp_path, cfg=base_cfg(), ssh=ssh, dry_run=dry_run, c2=c2)
    ip = WIN_IP if windows else IP
    if windows:
        ctx.world.add_foothold(ip, "Administrator", "boxpw-1", "root", windows=True)
    else:
        ctx.world.add_foothold(ip, "ubuntu", "boxpw-1", "root", escalation="sudo")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = (ctx.now()
                                     - clock * ctx.cfg["red"]["duration_min"] * 60)
    ctx.world.save()
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, sensors=ctx.sensors,
                        rng=ctx.rng)
    return ctx


def markers(**kw):
    return "\n".join(f"{k}={v}" for k, v in kw.items())


def test_dry_run_plans_without_touching_the_box(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ctx = ctx_for(tmp_path, ssh, dry_run=True)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok and "dry-run" in result.detail
    assert ssh.calls == []
    assert ctx.world.impacts() == [] and ctx.world.restore_table() == []


MECHANISMS = [
    ({"i": 0, "u": 1, "n": 1}, {"vi": 1, "vu": 0, "vn": 0}, "iptables",
     "iptables -D INPUT -p tcp --dport 3306 -j REJECT"),
    ({"i": 1, "u": 0, "n": 1}, {"vi": 0, "vu": 1, "vn": 0}, "ufw",
     "ufw delete deny 3306/tcp"),
    ({"i": 1, "u": 1, "n": 0}, {"vi": 0, "vu": 0, "vn": 1}, "nft", None),
]


@pytest.mark.parametrize("added,verified,mechanism,undo", MECHANISMS)
def test_a_block_that_holds_is_an_impact_with_an_exact_undo(
        tmp_path, added, verified, mechanism, undo):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ssh.on("iptables -I INPUT", rc=0, out=markers(**added))
    ssh.on("iptables -C INPUT", rc=0, out=markers(**verified))
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", dict(PIN), ctx)

    assert result.ok, result.detail
    assert mechanism in result.detail

    match = ADD_RE.fullmatch(ssh.calls[0]["cmd"])
    assert match, ssh.calls[0]["cmd"]
    assert match.group("port") == "3306"
    table, chain = match.group("table"), match.group("chain")
    script = (
        "iptables -C INPUT -p tcp --dport 3306 -j REJECT >/dev/null 2>&1 "
        "&& echo vi=1 || echo vi=0; "
        "ufw status 2>/dev/null | grep -qE '^3306/tcp[[:space:]]+(DENY|REJECT)' "
        "&& echo vu=1 || echo vu=0; "
        f"if nft list chain inet {table} {chain} 2>/dev/null "
        "| grep -q 'hook input' "
        f"&& nft list chain inet {table} {chain} 2>/dev/null "
        "| grep -q 'dport 3306'; then echo vn=1; else echo vn=0; fi")
    assert ssh.calls[1]["cmd"] == "sudo -n sh -c " + shlex.quote(script)

    impacts = ctx.world.impacts()
    assert len(impacts) == 1
    assert impacts[0]["service"] == "MySQL pin"
    assert impacts[0]["mode"] == "block_port"
    assert impacts[0]["ip"] == IP
    assert impacts[0]["port"] == int(PIN["port"])

    recipe = result.restore["recipe"]
    if undo is None:
        undo = f"nft delete table inet {match.group('table')}"
    assert undo in recipe
    assert ctx.world.restore_table()[0]["recipe"] == recipe
    assert ctx.world.restore_table()[0].get("secrets") is None

    artifact = ctx.world.artifacts()[0]
    assert artifact["kind"] == "impact-block_port"
    assert undo in artifact["detail"]


def test_an_inserted_but_inert_rule_is_still_in_the_undo(tmp_path):
    """`ufw insert` succeeds on inactive ufw; the inert rule is not the impact but must be undone."""
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ssh.on("iptables -I INPUT", rc=0, out=markers(i=0, u=0, n=1))
    ssh.on("iptables -C INPUT", rc=0, out=markers(vi=1, vu=0, vn=0))
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok
    assert result.impact["mode"] == "block_port"
    recipe = "\n".join(result.restore["recipe"])
    assert "iptables -D INPUT -p tcp --dport 3306 -j REJECT" in recipe
    assert "ufw delete deny 3306/tcp" in recipe
    assert "NOT enforcing" in recipe


def test_the_names_are_drawn_per_activation_and_nft_safe(tmp_path):
    from tests.helpers import FakeSSH
    from tlaloc.effects import artifactnames as names
    tables = []
    for _ in range(2):
        ssh = FakeSSH()
        ssh.on("iptables -I INPUT", rc=0, out=markers(i=0, u=1, n=1))
        ssh.on("iptables -C INPUT", rc=0, out=markers(vi=1, vu=0, vn=0))
        ctx = ctx_for(tmp_path / str(len(tables)), ssh)
        assert execute("block_port", dict(PIN), ctx).ok
        match = ADD_RE.fullmatch(ssh.calls[0]["cmd"])
        tables.append((match.group("table"), match.group("chain")))
    for table, chain in tables:
        assert re.fullmatch(r"[A-Za-z0-9_]+", table)
        assert re.fullmatch(r"[A-Za-z0-9_]+", chain)
        assert table.rsplit("_", 1)[-1] in names.SUFFIX_SET
        assert chain.rsplit("_", 1)[-1] in names.SUFFIX_SET
    assert tables[0][0] != tables[1][0]


def test_a_rule_no_mechanism_accepts_is_a_failure(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH().on("iptables -I INPUT", rc=0, out=markers(i=127, u=127, n=127))
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok is False
    assert "no firewall mechanism accepted" in result.detail
    assert ctx.world.impacts() == []
    assert ctx.world.restore_table() == []


def test_a_block_that_does_not_hold_is_not_an_impact(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ssh.on("iptables -I INPUT", rc=0, out=markers(i=0, u=1, n=1))
    ssh.on("iptables -C INPUT", rc=0, out=markers(vi=0, vu=0, vn=0))
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok is False
    assert "did not hold" in result.detail
    assert ctx.world.impacts() == [], "a block that did not hold is not an impact"
    # the accepted rule is real even though the block did not hold: its undo is recorded
    recipe = "\n".join(ctx.world.restore_table()[0]["recipe"])
    assert "iptables -D INPUT -p tcp --dport 3306 -j REJECT" in recipe
    assert ctx.world.artifacts()[0]["kind"] == "impact-block_port"


def test_transport_failure_is_ok_false_and_never_a_crash(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH().on("iptables -I INPUT", rc="raise")
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok is False
    assert "TransportError" in result.detail
    assert ctx.world.impacts() == []


def test_refuses_to_block_port_22(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", {"ip": IP, "port": 22, "display": "ssh pin"}, ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "port 22" in result.detail
    assert ssh.calls == []
    assert ctx.world.reject_memory()


def test_port_22_is_allowed_with_the_explicit_opt_in(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ssh.on("iptables -I INPUT", rc=0, out=markers(i=0, u=1, n=1))
    ssh.on("iptables -C INPUT", rc=0, out=markers(vi=1, vu=0, vn=0))
    ctx = ctx_for(tmp_path, ssh)
    result = execute("block_port", {"ip": IP, "port": 22, "display": "ssh pin",
                                    "allow_ssh": "true"}, ctx)
    assert result.ok, result.detail
    assert "--dport 22" in ssh.calls[0]["cmd"]


def test_needs_a_root_path(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ctx = make_ctx(tmp_path, ssh=ssh)
    ctx.world.add_foothold(IP, "analyst", "pw", "user")
    ctx.world.ensure_event_start()
    ctx.world.meta["event_start"] = ctx.now() - 0.6 * ctx.cfg["red"]["duration_min"] * 60
    ctx.world.save()
    ctx.policy = Policy(ctx.cfg, ctx.world, ctx.intel, rng=ctx.rng)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok is False
    assert "precondition" in result.detail and "root path" in result.detail
    assert ssh.calls == []


def test_locked_until_the_unrecoverable_tier_is_reached(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ctx = ctx_for(tmp_path, ssh, clock=0.3)
    result = execute("block_port", dict(PIN), ctx)
    assert result.ok is False
    assert "precondition" in result.detail
    assert "unrecoverable" in result.detail
    assert ssh.calls == []
    assert ctx.world.reject_memory()


def test_windows_admin_path_blocks_via_netsh(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ssh.on("netsh advfirewall firewall add rule", rc=0, out="Ok.\n")
    # the verify script prints the marker itself; the fake returns what it printed
    ssh.on("netsh advfirewall firewall show rule", rc=0, out="TLALOC_BLOCK=ok\n")
    ctx = ctx_for(tmp_path, ssh, windows=True)
    pin = {"ip": WIN_IP, "port": 3306, "display": "MySQL pin"}
    result = execute("block_port", pin, ctx)

    assert result.ok, result.detail
    add, verify = ssh.calls[0], ssh.calls[1]
    assert add["windows"] is True and verify["windows"] is True
    match = re.fullmatch(
        r"netsh advfirewall firewall add rule name=(?P<rule>[A-Za-z0-9]+) "
        r"dir=in action=block protocol=TCP localport=3306", add["cmd"])
    assert match, add["cmd"]
    rule = match.group("rule")
    assert verify["cmd"] == (
        f"$o = netsh advfirewall firewall show rule name={rule} 2>$null; "
        f"if ($o -match '{rule}' -and $o -match 'Block' -and $o -match '3306') "
        "{ 'TLALOC_BLOCK=ok' } else { 'TLALOC_BLOCK=no' }")
    assert result.impact["service"] == "MySQL pin"
    assert result.restore["recipe"][-1] == (
        f"netsh advfirewall firewall delete rule name={rule}")
    assert ctx.world.impacts()[0]["ip"] == WIN_IP


def test_windows_rule_that_does_not_report_block_is_a_failure(tmp_path):
    from tests.helpers import FakeSSH
    ssh = FakeSSH()
    ssh.on("netsh advfirewall firewall add rule", rc=0, out="Ok.\n")
    ssh.on("netsh advfirewall firewall show rule", rc=0,
           out="No rules match the specified criteria.\n")
    ctx = ctx_for(tmp_path, ssh, windows=True)
    result = execute("block_port", {"ip": WIN_IP, "port": 445, "display": "SMB pin"},
                     ctx)
    assert result.ok is False
    assert "did not hold" in result.detail
    assert ctx.world.impacts() == []
    # the rule was accepted, so the undo is recorded even though the block is a lie
    rule = re.fullmatch(
        r"netsh advfirewall firewall add rule name=(?P<rule>[A-Za-z0-9]+) .*",
        ssh.calls[0]["cmd"]).group("rule")
    assert ctx.world.restore_table()[0]["recipe"][-1] == (
        f"netsh advfirewall firewall delete rule name={rule}")


def test_windows_ssh_auth_failure_falls_back_to_the_c2_channel(tmp_path):
    """A promoted DC refuses Administrator over SSH; red keeps the tactic through C2."""
    ssh = FakeSSH().on(lambda call: True, rc="raise")
    c2 = FakeC2(task_out=f"{bp.WINDOWS_MARKER}=ok")
    ctx = ctx_for(tmp_path, ssh, windows=True, c2=c2)
    result = execute("block_port", {"ip": WIN_IP, "port": 3389, "display": "RDP pin"}, ctx)

    assert result.ok, result.detail
    assert "via C2" in result.detail
    assert result.impact["port"] == 3389
    assert ctx.world.impacts()[0]["port"] == 3389
    scripts = [task["command"] for task in c2.tasks]
    assert len(scripts) == 2
    assert scripts[0].startswith("netsh advfirewall firewall add rule")
    assert bp.WINDOWS_MARKER + "=ok" in scripts[1]
    rule = re.search(r"name=([A-Za-z0-9]+)", scripts[0]).group(1)
    assert result.restore["recipe"][-1] == (
        f"netsh advfirewall firewall delete rule name={rule}")


def test_windows_ssh_auth_failure_without_c2_is_a_failure(tmp_path):
    ssh = FakeSSH().on(lambda call: True, rc="raise")
    ctx = ctx_for(tmp_path, ssh, windows=True)
    result = execute("block_port", {"ip": WIN_IP, "port": 445, "display": "SMB pin"}, ctx)
    assert result.ok is False
    assert "no C2 layer" in result.detail
    assert ctx.world.impacts() == []
