"""In-path firewall support, offline: the sendkey mapping, the per-team pfSense config
surgery (against the same anchor stanzas the real factory seed carries), and the
post-cutover netplan the engine cutover writes."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from firewall_ops import (FW_CONFIG_PORT, cutover_netplan_yaml, generate_team_config,
                          key_sequence)

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


class KeySequence(unittest.TestCase):
    def test_console_commands_map_fully(self):
        # The exact strings phase 5 types: no character may raise, and shift-combos
        # appear only where the layout needs them.
        seq = key_sequence(f"fetch -o /cf/conf/config.xml http://192.168.120.1:{FW_CONFIG_PORT}/config-team120.xml")
        self.assertNotIn("shift-minus", seq)      # '-' is unshifted
        self.assertIn("shift-semicolon", seq)     # ':'
        self.assertEqual(seq.count("dot"), 5)
        self.assertEqual(key_sequence("reboot"), ["r", "e", "b", "o", "o", "t"])

    def test_unknown_char_raises_loudly(self):
        with self.assertRaises(ValueError):
            key_sequence("echo 'quote'")


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
        # the WAN pass rule must be filter's FIRST rule (before the nat block's rules)
        first_rule = out[out.index("<rule>"):out.index("</rule>")]
        self.assertIn("Allow engine scoring", first_rule)

    def test_dnat_spec_targets_and_substitution(self):
        out = generate_team_config(SEED, "121", red_dnat_spec=["4470->10.200.0.{tid}"])
        self.assertIn("<port>4470</port>", out)
        self.assertIn("<target>10.200.0.121</target>", out)     # {tid} → team identifier
        self.assertIn("<address>192.168.121.1</address>", out)  # DNAT rides the LAN addr

    def test_no_dnats_no_forward_rule(self):
        out = generate_team_config(SEED, "122")
        self.assertNotIn("<target>", out)
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


if __name__ == "__main__":
    unittest.main()
