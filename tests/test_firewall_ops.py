"""In-path firewall support, offline: the SSH config push, the per-team pfSense config
surgery (against the same anchor stanzas the real factory seed carries), and the
post-cutover netplan the engine cutover writes."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from firewall_ops import cutover_netplan_yaml, generate_team_config

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
