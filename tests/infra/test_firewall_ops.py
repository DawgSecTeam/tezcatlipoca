"""In-path firewall support, offline: the SSH config push, pfSense/VyOS config generation, and the cutover netplan."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from net.firewall_ops import (cutover_netplan_yaml, firewall_kind, generate_team_config,
                          generate_vyos_commands, generate_vyos_lan_commands,
                          vyos_lan_config_path)

# A minimal stand-in for the factory config.xml; the real seed lives in
# competitions/*/pfsense/, not in the repo proper.
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
    # The phase-5 probe SSHes to the firewall's own WAN address; pfSense blocks WAN-inbound
    # to itself unless a rule says otherwise (caught live).
        out = generate_team_config(SEED, "120")
        at = out.index("Allow engine SSH to the firewall")
        rule = out[out.rindex("<rule>", 0, at):out.index("</rule>", at)]
        self.assertIn("<interface>wan</interface>", rule)
        self.assertIn("<protocol>tcp</protocol>", rule)
        self.assertIn("<address>172.31.120.1</address>", rule)
        self.assertIn("<network>(self)</network>", rule)
        self.assertIn("<port>22</port>", rule)

    def test_in_path_addresses_and_rules(self):
        out = generate_team_config(SEED, "120")
        # interfaces swapped wholesale: WAN transit /30, LAN = the team gateway
        self.assertIn("<ipaddr>172.31.120.2</ipaddr>", out)
        self.assertIn("<subnet>30</subnet>", out)
        self.assertIn("<ipaddr>192.168.120.1</ipaddr>", out)
        self.assertIn("<gateway>172.31.120.1</gateway>", out)
        self.assertIn("<hostname>fw-team120</hostname>", out)
        self.assertIn("<ssh><enable>enabled</enable></ssh>", out)
        self.assertIn("<mode>disabled</mode>", out)             # engine keeps the NAT
        # caught live: <network> takes the `lan` keyword, never a CIDR
        self.assertIn("<network>lan</network>", out)
        self.assertNotIn("<network>192.168.120.0/24</network>", out)
        flt = out[out.index("<filter>"):]                       # WAN pass rule goes first
        first_rule = flt[flt.index("<rule>"):flt.index("</rule>")]
        self.assertIn("Allow engine scoring", first_rule)

    def test_apt_cacher_is_redirected_to_the_engine_by_default(self):
    # Boxes' apt proxy is the gateway address; the firewall owns it after cutover.
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
        self.assertIn("<target>10.200.0.121</target>", out)
        self.assertIn("<address>192.168.121.1</address>", out)





class VyosCommands(unittest.TestCase):

    def test_wan_side_config_but_no_lan_address_change(self):
    # Pass A must not touch eth1: a commit that moves it kills the SSH session pushing the
        # config (bootstrap_firewalls flips eth1 as pass B over the WAN).
        out = generate_vyos_commands("120")
        self.assertIn("set interfaces ethernet eth0 address 172.31.120.2/30", out)
        self.assertIn("set protocols static route 0.0.0.0/0 next-hop 172.31.120.1", out)
        self.assertIn("set system host-name fw-team120", out)
        self.assertIn("set service ssh port 22", out)
        self.assertNotIn("set interfaces ethernet eth1", out.split("commit")[0])

    def test_no_filter_rules_circinus_default_accepts_the_path(self):
    # The 1.5 revamp removed interface-attached rulesets AND the `firewall interface`
    # attachment (caught live); the default-accept chains already allow the path.
        out = generate_vyos_commands("120")
        self.assertNotIn("set firewall", out)
        self.assertNotIn("inbound-interface", out)


    def test_dnat_spec_targets_and_substitution(self):
        out = generate_vyos_commands("121", red_dnat_spec=["4470->10.200.0.{tid}"])
        self.assertIn("set nat destination rule 20 destination port 4470", out)
        self.assertIn("set nat destination rule 20 translation address 10.200.0.121", out)


    def test_lan_pass_flips_eth1_off_the_bootstrap_address(self):
        out = generate_vyos_lan_commands("120")
        self.assertIn("delete interfaces ethernet eth1 address 192.168.1.1/24", out)
        self.assertIn("set interfaces ethernet eth1 address 192.168.120.1/24", out)
        self.assertIn("show interfaces ethernet eth1 | grep 192.168.120.1", out)
        self.assertNotIn("eth0", out)



class VyosBootstrap(unittest.TestCase):
    def _run(self, wan_up):
        import tempfile
        from net import firewall_ops as fo
        from unittest.mock import MagicMock, patch
        calls = []
        with tempfile.TemporaryDirectory() as d:
            cmds = Path(d) / "config-team120.cmds"
            cmds.write_text("configure\ncommit\n")
            vyos_lan_config_path(cmds).write_text("configure\ncommit\n")

            def probe(ctx, ip, port, timeout=4):
                return wan_up if ip == "172.31.120.2" else False

            def wait(ctx, ip, port, budget_s=300, interval_s=10):
                # the bootstrap address answers (template SSH); the WAN answers after pass A's commit
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
                    {"team1": cmds}, {}, node="pve", log=lambda *_: None)
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
                "scoring_engine_ip": "10.0.0.248"}


class PushVyos(unittest.TestCase):
    def test_success_needs_clean_exit_and_the_canary(self):
    # circinus commits print NOTHING on success — canary + clean exit are the only signals.
        from net import firewall_ops as fo
        from unittest.mock import patch
        clock = {"t": 0.0}
        for rc, out in ((1, "set system host-name 'fw-team120'\n"),
                        (0, "nothing useful")):
            with patch.object(fo, "_pty_session", return_value=(rc, out)), \
                    patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                    patch.object(fo.time, "sleep",
                                 side_effect=lambda s: clock.update(t=clock["t"] + 100)):
                with self.assertRaises(RuntimeError):
                    fo._push_vyos(FAKE_SSH_CTX, "192.168.1.1", "script", "fw-team120",
                                  "team1", budget_s=150)
        with patch.object(fo, "_pty_session",
                          return_value=(0, "set system host-name 'fw-team120'\n")), \
                patch.object(fo.time, "sleep"):
            fo._push_vyos(FAKE_SSH_CTX, "192.168.1.1", "script", "fw-team120", "team1")




class CutoverNetplan(unittest.TestCase):
    def test_team_nics_lose_address_transit_routes(self):
        yaml = cutover_netplan_yaml(TEAMS)
        # team NICs ens19/ens20 in sorted team order, transit NICs continuing the
        # positional sequence as ens21/ens22
        self.assertIn("    ens19:\n      dhcp4: false\n      optional: true", yaml)
        self.assertNotIn("192.168.120.1/24", yaml)
        self.assertIn("    ens21:\n      addresses: [\"172.31.120.1/30\"]", yaml)
        self.assertIn("    ens22:\n      addresses: [\"172.31.121.1/30\"]", yaml)
        self.assertIn("- to: \"192.168.120.0/24\"\n          via: \"172.31.120.2\"", yaml)
        self.assertIn("- to: \"192.168.121.0/24\"\n          via: \"172.31.121.2\"", yaml)

    def test_the_red_gateway_survives_the_cutover(self):
        """red01 routes the team subnets through the engine's red NIC: a cutover
        that drops it leaves the day-0 seed with no path to any box."""
        yaml = cutover_netplan_yaml(TEAMS, red_segment={"subnet": "10.200.0.0/24",
                                                       "gw": "10.200.0.1"})
        self.assertIn("    ens23:\n      addresses: [\"10.200.0.1/24\"]", yaml)


class AuthorizedKey(unittest.TestCase):
    def test_key_lands_in_admins_authorizedkeys(self):
        import base64
        out = generate_team_config(SEED, "120", authorized_key="ssh-ed25519 AAAA test@x")
        self.assertIn("<name>admin</name><authorizedkeys>"
                      + base64.b64encode(b"ssh-ed25519 AAAA test@x\n").decode()
                      + "</authorizedkeys>", out)



class PushCommand(unittest.TestCase):
    def test_push_installs_only_when_different_and_reboots_detached(self):
        from net import firewall_ops as fo
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
        from net import firewall_ops as fo
        from unittest.mock import patch
        res = ["timeout", (255, ""), (0, "APPLIED\n")]
        with patch.object(fo, "ssh_via_gateway", side_effect=self._fake(res)), \
                patch.object(fo.time, "sleep"):
            self.assertEqual(fo._push_config(None, "192.168.1.1", "<x/>", "team1"), "APPLIED")



class BootstrapFirewalls(unittest.TestCase):

    def _run(self, wan_up, push_result="APPLIED", boot_ok=True):
        import tempfile
        from net import firewall_ops as fo
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
                    {"team1": cfg}, {}, node="pve", log=lambda *_: None)
        return calls, ssh_on

    def test_fresh_firewall_is_reached_on_the_borrowed_bootstrap_address(self):
        calls, ssh_on = self._run(wan_up=False)
        self.assertEqual(calls, [("wait", "192.168.1.1"), ("push", "192.168.1.1"),
                                 ("wait", "172.31.120.2")])
        cmds = [c.args[1] for c in ssh_on.call_args_list]
        self.assertIn("sudo ip addr add 192.168.1.2/24 dev ens19 || true", cmds)
        self.assertIn("sudo ip addr del 192.168.1.2/24 dev ens19 || true", cmds)


    def test_unchanged_config_skips_the_reboot_wait(self):
        calls, _ = self._run(wan_up=True, push_result="UNCHANGED")
        self.assertEqual(calls, [("push", "172.31.120.2")])

    def test_template_that_never_boots_fails_loudly_and_still_releases_the_address(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run(wan_up=False, boot_ok=False)
        self.assertIn("pfsense-provision", str(cm.exception))


class VerifyInPathConvergence(unittest.TestCase):
# The cutover moves the gateway MAC, so the box probe must retry, not fail on probe #1.

    def test_box_probe_retries_until_it_answers(self):
        from net import firewall_ops as fo
        from unittest.mock import patch
        answers = iter([False, False, True])
        clock = {"t": 0.0}
        with patch.object(fo, "_probe_tcp", side_effect=lambda *a, **k: next(answers)), \
                patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                patch.object(fo.time, "sleep", side_effect=lambda s: clock.update(t=clock["t"] + s)):
            self.assertTrue(fo._wait_tcp(None, "192.168.1.2", 22))



class GuestAgentShellcmd(unittest.TestCase):
# pfSense never runs /usr/local/etc/rc.d/* (the local-script pass in /etc/rc only globs
#     *.sh), so the <system><afterbootupshellcmd> tag is the only mechanism that survives —
#     and it must ride every config the firewall runs, the template's and the per-team ones.

    def test_generated_config_carries_the_agent_boot_hook(self):
        out = generate_team_config(SEED, "120")
        self.assertIn("<afterbootupshellcmd>/usr/local/etc/rc.d/qemu-guest-agent start"
                      "</afterbootupshellcmd>", out)



class AgentDiagnostics(unittest.TestCase):
# fw_agent_diagnostic: never raises, never fires on an agentless clone.

    def test_agentless_clone_returns_empty_string(self):
        from net import firewall_ops as fo
        from unittest.mock import patch
        clock = {"t": 0.0}
        with patch.object(fo, "proxmox_api", side_effect=RuntimeError("agent not running")), \
                patch.object(fo.time, "time", side_effect=lambda: clock["t"]), \
                patch.object(fo.time, "sleep", side_effect=lambda s: clock.update(t=clock["t"] + s)):
            self.assertEqual(fo.fw_agent_diagnostic("pve", 9), "")






if __name__ == "__main__":
    unittest.main()
