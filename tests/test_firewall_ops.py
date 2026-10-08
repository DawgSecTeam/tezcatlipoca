"""In-path firewall support, offline: the SSH config push, the per-team pfSense config
surgery (against the same anchor stanzas the real factory seed carries), the per-team
VyOS set-command generation, and the post-cutover netplan the engine cutover writes."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from firewall_ops import (cutover_netplan_yaml, firewall_kind, generate_team_config,
                          generate_vyos_commands, generate_vyos_lan_commands,
                          vyos_lan_config_path)

# The anchors generate_team_config's string surgery keys on — a minimal stand-in for the
# factory config.xml (the real seed lives in competitions/*/pfsense/, not in the repo
# proper, so tests carry their own).
SEED = """<?xml version="1.0"?>
<pfsense>
\t<system>
\t\t<hostname>pfSense</hostname>
\t\t<domain>localdomain</domain>
\t</system>
\t<interfaces>
\t\t<wan>
\t\t\t<if>vmbr0</if>
\t\t</wan>
\t\t<lan>
\t\t\t<if>vmbr1</if>
\t\t\t<ipaddr>192.168.1.1</ipaddr>
\t\t</lan>
\t</interfaces>
\t<gateways></gateways>
\t<ssh></ssh>
\t<user><name>admin</name></user>
\t<filter>
\t\t<rule>
\t\t\t<type>pass</type>
\t\t\t<interface>lan</interface>
\t\t</rule>
\t</filter>
</pfsense>
"""
TEAMS = {"team2": {"identifier": "121", "password": "x"},
         "team1": {"identifier": "120", "password": "x"}}


class TeamConfig(unittest.TestCase):
    def test_wan_allows_only_the_engine_to_ssh_the_firewall(self):
        """The phase-5 success probe is SSH to the firewall's own WAN address; pfSense blocks
        WAN-inbound to itself unless a rule says otherwise (live-found 2026-10-05)."""
        out = generate_team_config(SEED, "120")
        at = out.index("Allow engine SSH to the firewall")
        rule = out[out.rindex("<rule>", 0, at):out.index("</rule>", at)]
        self.assertIn("<interface>wan</interface>", rule)
        self.assertIn("<protocol>tcp</protocol>", rule)
        self.assertIn("<address>172.31.120.1</address>", rule)   # engine transit only
        self.assertIn("<network>(self)</network>", rule)
        self.assertIn("<port>22</port>", rule)

    def test_in_path_addresses_and_rules(self):
        out = generate_team_config(SEED, "120")
        # interfaces swapped wholesale: WAN transit /30, LAN = the team gateway
        self.assertIn("<ipaddr>172.31.120.2</ipaddr>", out)
        self.assertIn("<subnet>30</subnet>", out)
        self.assertIn("<ipaddr>192.168.120.1</ipaddr>", out)
        self.assertIn("<gateway>172.31.120.1</gateway>", out)   # WANGW = engine transit
        self.assertIn("<hostname>fw-team120</hostname>", out)
        self.assertIn("<ssh><enable>enabled</enable></ssh>", out)  # probe + operator SSH
        self.assertIn("<mode>disabled</mode>", out)             # engine keeps the NAT
        # the pfsense-ad 2026-09-28 bug: <network> takes the `lan` keyword, never a CIDR
        self.assertIn("<network>lan</network>", out)
        self.assertNotIn("<network>192.168.120.0/24</network>", out)
        # the WAN pass rule must be the FILTER section's first rule
        flt = out[out.index("<filter>"):]
        first_rule = flt[flt.index("<rule>"):flt.index("</rule>")]
        self.assertIn("Allow engine scoring", first_rule)

    def test_apt_cacher_is_redirected_to_the_engine_by_default(self):
        """Boxes' apt proxy is the gateway address; the firewall owns it after cutover."""
        out = generate_team_config(SEED, "123")
        at = out.index("apt-cacher (engine service")
        rule = out[out.rindex("<rule>", 0, at):out.index("</rule>", at)]
        self.assertIn("<address>192.168.123.1</address>", rule)
        self.assertIn("<port>3142</port>", rule)
        self.assertIn("<target>172.31.123.1</target>", rule)
        self.assertIn("<interface>lan</interface>", rule)

    def test_dnat_spec_targets_and_substitution(self):
        out = generate_team_config(SEED, "121", red_dnat_spec=["4470->10.200.0.{tid}"])
        self.assertIn("<port>4470</port>", out)
        self.assertIn("<target>10.200.0.121</target>", out)     # {tid} → team identifier
        self.assertIn("<address>192.168.121.1</address>", out)  # DNAT rides the LAN addr

    def test_no_dnats_no_forward_rule(self):
        out = generate_team_config(SEED, "122")
        # no red DNATs: the only redirect is the engine's apt-cacher
        self.assertEqual(out.count("<target>"), 1)
        self.assertIn("<target>172.31.122.1</target>", out)
        self.assertIn("<mode>disabled</mode>", out)


class FirewallKind(unittest.TestCase):
    def test_vyos_by_substring_everything_else_is_pfsense(self):
        self.assertEqual(firewall_kind("vyos-provision"), "vyos")
        self.assertEqual(firewall_kind("base-vyos-1.5"), "vyos")
        self.assertEqual(firewall_kind("pfsense-provision"), "pfsense")
        self.assertEqual(firewall_kind("pfsense"), "pfsense")
        self.assertEqual(firewall_kind(None), "pfsense")   # old fixtures carry no template


class VyosCommands(unittest.TestCase):
    """The generated pass-A/pass-B CLI scripts (pure functions)."""

    def test_wan_side_config_but_no_lan_address_change(self):
        """Pass A must not touch eth1: a commit that moves it kills the SSH session
        pushing the config (bootstrap_firewalls flips eth1 as pass B over the WAN)."""
        out = generate_vyos_commands("120")
        self.assertIn("set interfaces ethernet eth0 address 172.31.120.2/30", out)
        self.assertIn("set protocols static route 0.0.0.0/0 next-hop 172.31.120.1", out)
        self.assertIn("set system host-name fw-team120", out)
        self.assertIn("set service ssh port 22", out)
        self.assertNotIn("set interfaces ethernet eth1", out.split("commit")[0])

    def test_wan_in_filter_mirrors_the_pfsense_rules(self):
        out = generate_vyos_commands("120")
        self.assertIn("set firewall ipv4 name WAN-IN default-action drop", out)
        self.assertIn("set firewall ipv4 name WAN-IN rule 10 state established enable", out)
        # routed scoring: any → the team subnet
        self.assertIn("set firewall ipv4 name WAN-IN rule 20 destination group "
                      "network-group LAN-TEAM120", out)
        # the probe: engine transit → the firewall's own WAN tcp/22
        self.assertIn("set firewall ipv4 name WAN-IN rule 30 source address 172.31.120.1", out)
        self.assertIn("set firewall ipv4 name WAN-IN rule 30 destination address 172.31.120.2", out)
        self.assertIn("set firewall ipv4 name WAN-IN rule 30 destination port 22", out)
        self.assertIn("set interfaces ethernet eth0 firewall in name WAN-IN", out)

    def test_apt_cacher_dnat_and_its_filter_allow(self):
        """VyOS filters the TRANSLATED destination, so each DNAT needs its own WAN-IN
        accept rule or WAN-IN's default-drop eats the redirected traffic."""
        out = generate_vyos_commands("123")
        self.assertIn("set nat destination rule 10 destination address 192.168.123.1", out)
        self.assertIn("set nat destination rule 10 destination port 3142", out)
        self.assertIn("set nat destination rule 10 translation address 172.31.123.1", out)
        self.assertIn("set firewall ipv4 name WAN-IN rule 40 destination address 172.31.123.1", out)
        self.assertIn("set firewall ipv4 name WAN-IN rule 40 destination port 3142", out)

    def test_dnat_spec_targets_and_substitution(self):
        out = generate_vyos_commands("121", red_dnat_spec=["4470->10.200.0.{tid}"])
        self.assertIn("set nat destination rule 20 destination port 4470", out)
        self.assertIn("set nat destination rule 20 translation address 10.200.0.121", out)
        # the DNAT'd target is reachable post-translation
        self.assertIn("set firewall ipv4 name WAN-IN rule 50 destination address 10.200.0.121", out)
        self.assertIn("set firewall ipv4 name WAN-IN rule 50 destination port 4470", out)

    def test_script_commits_saves_and_ends_on_a_grep_canary(self):
        out = generate_vyos_commands("120")
        tail = out.strip().splitlines()[-4:]
        self.assertEqual(tail, ["commit", "save", "exit",
                                "show configuration commands | grep host-name"])

    def test_lan_pass_flips_eth1_off_the_bootstrap_address(self):
        out = generate_vyos_lan_commands("120")
        self.assertIn("delete interfaces ethernet eth1 address 192.168.1.1/24", out)
        self.assertIn("set interfaces ethernet eth1 address 192.168.120.1/24", out)
        self.assertIn("show interfaces ethernet eth1 | grep 192.168.120.1", out)
        self.assertNotIn("eth0", out)

    def test_lan_artifact_path_pairs_with_pass_a(self):
        self.assertEqual(vyos_lan_config_path(Path("/x/vyos/config-team7.cmds")),
                         Path("/x/vyos/config-team7-lan.cmds"))


class VyosBootstrap(unittest.TestCase):
    """The two-pass orchestration, with the engine/SSH layer faked."""

    def _run(self, wan_up):
        import tempfile
        import firewall_ops as fo
        from unittest.mock import MagicMock, patch
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cmds = Path(d) / "config-team120.cmds"
            cmds.write_text("configure\ncommit\n")
            vyos_lan_config_path(cmds).write_text("configure\ncommit\n")

            def probe(ctx, ip, port, timeout=4):
                return wan_up if ip == "172.31.120.2" else False

            def wait(ctx, ip, port, budget_s=300, interval_s=10):
                # the bootstrap address answers (template SSH) and the WAN answers after
                # pass A's live commit
                return ip in ("172.31.120.2", fo.BOOTSTRAP_FW_IP)

            def push(ctx, ip, script, canary, who, budget_s=420):
                calls.append(("push", ip, canary))
                return script

            with patch.object(fo, "ssh_on_gateway", MagicMock()), \
                    patch.object(fo, "_probe_tcp", side_effect=probe), \
                    patch.object(fo, "_wait_tcp", side_effect=wait), \
                    patch.object(fo, "_push_vyos", side_effect=push):
                fo.bootstrap_firewalls(
                    {"team1": {"identifier": "120", "password": "x"}},
                    [{"team_key": "team1", "identifier": "120", "vmid": 9,
                      "box": {"name": "fw01", "template": "vyos-provision"}}],
                    {"team1": cmds}, {}, log=lambda *_: None)
        return calls

    def test_fresh_firewall_pass_a_on_bootstrap_then_pass_b_on_wan(self):
        calls = self._run(wan_up=False)
        self.assertEqual([("push", "192.168.1.1", "fw-team120"),
                          ("push", "172.31.120.2", "192.168.120.1")], calls)

    def test_rerun_takes_both_passes_on_the_wan_address(self):
        calls = self._run(wan_up=True)
        self.assertEqual([c[1] for c in calls], ["172.31.120.2", "172.31.120.2"])
        self.assertEqual([c[2] for c in calls], ["fw-team120", "192.168.120.1"])


FAKE_SSH_CTX = {"ssh_key_path": "k", "vm_username": "ops",
                "scoring_engine_ip": "10.0.0.252"}


class PushVyos(unittest.TestCase):
    def _fake(self, results):
        import subprocess
        from unittest.mock import MagicMock

        def run(*a, **k):
            r = results.pop(0)
            if r == "timeout":
                raise subprocess.TimeoutExpired("ssh", 30)
            return MagicMock(returncode=r[0], stdout=r[1], stderr="")
        return run

    def test_success_needs_commit_complete_and_the_canary(self):
        import firewall_ops as fo
        from unittest.mock import MagicMock, patch
        clock = {"t": 0.0}

        def run_static(stdout):
            def run(*a, **k):
                return MagicMock(returncode=0, stdout=stdout, stderr="")
            return run
        # either signal alone is not success: the canary without a commit means the
        # config never applied; a commit line without the canary means it did not land
        for stdout in ("commit complete\n", "fw-team120\n"):
            with patch.object(fo.subprocess, "run", side_effect=run_static(stdout)), \
                    patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                    patch.object(fo.time, "sleep",
                                 side_effect=lambda s: clock.update(t=clock["t"] + 100)):
                with self.assertRaises(RuntimeError):
                    fo._push_vyos(FAKE_SSH_CTX, "192.168.1.1", "script", "fw-team120", "team1",
                                  budget_s=150)
        with patch.object(fo.subprocess, "run",
                          side_effect=run_static("commit complete\n"
                                                 "set system host-name 'fw-team120'\n")), \
                patch.object(fo.time, "sleep"):
            fo._push_vyos(FAKE_SSH_CTX, "192.168.1.1", "script", "fw-team120", "team1")

    def test_ssh_has_no_remote_command_and_uses_the_vyos_user(self):
        import firewall_ops as fo
        from unittest.mock import patch
        seen = {}

        def run(args, **k):
            seen["args"], seen["input"] = args, k.get("input")
            return self._fake([(0, "commit complete\nfw-team120\n")])()
        with patch.object(fo.subprocess, "run", side_effect=run), \
                patch.object(fo.time, "sleep"):
            fo._push_vyos(FAKE_SSH_CTX, "192.168.1.1", "configure\n",
                          "fw-team120", "team1")
        self.assertTrue(seen["args"][-1].startswith("vyos@"))
        self.assertNotIn("-c", seen["args"])        # no remote command: stdin drives the CLI
        self.assertEqual(seen["input"], "configure\n")


class CutoverNetplan(unittest.TestCase):
    def test_team_nics_lose_address_transit_routes(self):
        yaml = cutover_netplan_yaml(TEAMS)
        # sorted team key order: team1 (120) then team2 (121); team NICs ens19, ens20;
        # transit NICs continue the positional sequence: ens21, ens22
        self.assertIn("    ens19:\n      dhcp4: false\n      optional: true", yaml)
        self.assertNotIn("192.168.120.1/24", yaml)  # the gateway address is GONE
        self.assertIn("    ens21:\n      addresses: [\"172.31.120.1/30\"]", yaml)
        self.assertIn("    ens22:\n      addresses: [\"172.31.121.1/30\"]", yaml)
        self.assertIn("- to: \"192.168.120.0/24\"\n          via: \"172.31.120.2\"", yaml)
        self.assertIn("- to: \"192.168.121.0/24\"\n          via: \"172.31.121.2\"", yaml)


class AuthorizedKey(unittest.TestCase):
    def test_key_lands_in_admins_authorizedkeys(self):
        import base64
        out = generate_team_config(SEED, "120", authorized_key="ssh-ed25519 AAAA test@x")
        self.assertIn("<name>admin</name><authorizedkeys>"
                      + base64.b64encode(b"ssh-ed25519 AAAA test@x\n").decode()
                      + "</authorizedkeys>", out)

    def test_no_key_leaves_the_config_alone(self):
        self.assertNotIn("authorizedkeys", generate_team_config(SEED, "120"))


class PushCommand(unittest.TestCase):
    def test_push_installs_only_when_different_and_reboots_detached(self):
        import firewall_ops as fo
        cmd = fo._push_command("<pfsense/>")
        self.assertIn("openssl base64 -d -A", cmd)
        self.assertIn("cmp -s /tmp/tez-new.xml /cf/conf/config.xml", cmd)
        self.assertIn("rm -f /tmp/config.cache", cmd)
        self.assertIn("nohup sh -c 'sleep 2; /sbin/reboot'", cmd)   # never hangs the ssh session
        self.assertIn("UNCHANGED", cmd)


class PushRetry(unittest.TestCase):
    def _fake(self, results):
        import subprocess
        from unittest.mock import MagicMock

        def run(*a, **k):
            r = results.pop(0)
            if r == "timeout":
                raise subprocess.TimeoutExpired("ssh", 30)
            return MagicMock(returncode=r[0], stdout=r[1], stderr="")
        return run

    def test_retries_a_booting_appliance_then_applies(self):
        import firewall_ops as fo
        from unittest.mock import patch
        res = ["timeout", (255, ""), (0, "APPLIED\n")]
        with patch.object(fo, "ssh_via_gateway", side_effect=self._fake(res)), \
                patch.object(fo.time, "sleep"):
            self.assertEqual(fo._push_config(None, "192.168.1.1", "<x/>", "team1"), "APPLIED")

    def test_gives_up_after_the_budget_naming_the_last_failure(self):
        import firewall_ops as fo
        from unittest.mock import patch
        clock = {"t": 0.0}
        with patch.object(fo, "ssh_via_gateway", side_effect=lambda *a, **k: self._fake(["timeout"])()), \
                patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                patch.object(fo.time, "sleep", side_effect=lambda s: clock.update(t=clock["t"] + 100)):
            with self.assertRaises(RuntimeError) as cm:
                fo._push_config(None, "192.168.1.1", "<x/>", "team1", budget_s=150)
        self.assertIn("no answer within 30s", str(cm.exception))


class BootstrapFirewalls(unittest.TestCase):
    """The orchestration, with the engine/SSH layer faked."""

    def _run(self, wan_up, push_result="APPLIED", boot_ok=True):
        import tempfile
        import firewall_ops as fo
        from unittest.mock import MagicMock, patch
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config-team120.xml"
            cfg.write_text("<pfsense/>")
            ssh_on = MagicMock(return_value=MagicMock(stdout="", returncode=0))

            def probe(ctx, ip, port, timeout=4):
                return wan_up if ip == "172.31.120.2" else False

            def wait(ctx, ip, port, budget_s=300, interval_s=10):
                calls.append(("wait", ip))
                return boot_ok if ip == fo.BOOTSTRAP_FW_IP else True

            def push(ctx, ip, xml, who):
                calls.append(("push", ip))
                return push_result

            with patch.object(fo, "ssh_on_gateway", ssh_on), \
                    patch.object(fo, "_probe_tcp", side_effect=probe), \
                    patch.object(fo, "_wait_tcp", side_effect=wait), \
                    patch.object(fo, "_push_config", side_effect=push):
                fo.bootstrap_firewalls(
                    TEAMS, [{"team_key": "team1", "identifier": "120", "vmid": 9}],
                    {"team1": cfg}, {}, log=lambda *_: None)
        return calls, ssh_on

    def test_fresh_firewall_is_reached_on_the_borrowed_bootstrap_address(self):
        calls, ssh_on = self._run(wan_up=False)
        self.assertEqual(calls, [("wait", "192.168.1.1"), ("push", "192.168.1.1"),
                                 ("wait", "172.31.120.2")])
        cmds = [c.args[1] for c in ssh_on.call_args_list]
        self.assertIn("sudo ip addr add 192.168.1.2/24 dev ens19 || true", cmds)
        self.assertIn("sudo ip addr del 192.168.1.2/24 dev ens19 || true", cmds)  # always released

    def test_rerun_reaches_the_firewall_on_its_transit_address(self):
        calls, _ = self._run(wan_up=True)
        self.assertEqual(calls[0], ("push", "172.31.120.2"))

    def test_unchanged_config_skips_the_reboot_wait(self):
        calls, _ = self._run(wan_up=True, push_result="UNCHANGED")
        self.assertEqual(calls, [("push", "172.31.120.2")])

    def test_template_that_never_boots_fails_loudly_and_still_releases_the_address(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run(wan_up=False, boot_ok=False)
        self.assertIn("pfsense-provision", str(cm.exception))


class VerifyInPathConvergence(unittest.TestCase):
    """The cutover moves the gateway MAC; the box probe must retry, not fail on probe #1."""

    def test_box_probe_retries_until_it_answers(self):
        import firewall_ops as fo
        from unittest.mock import patch
        answers = iter([False, False, True])
        clock = {"t": 0.0}
        with patch.object(fo, "_probe_tcp", side_effect=lambda *a, **k: next(answers)), \
                patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                patch.object(fo.time, "sleep", side_effect=lambda s: clock.update(t=clock["t"] + s)):
            self.assertTrue(fo._wait_tcp(None, "192.168.1.2", 22))

    def test_box_probe_gives_up_after_the_budget(self):
        import firewall_ops as fo
        from unittest.mock import patch
        clock = {"t": 0.0}
        with patch.object(fo, "_probe_tcp", return_value=False), \
                patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                patch.object(fo.time, "sleep", side_effect=lambda s: clock.update(t=clock["t"] + s)):
            self.assertFalse(fo._wait_tcp(None, "192.168.1.2", 22, budget_s=30, interval_s=10))


if __name__ == "__main__":
    unittest.main()
