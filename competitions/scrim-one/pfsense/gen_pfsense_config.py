#!/usr/bin/env python3
"""Render the per-team pfSense config.xml for testcomp-7box (team 120), in-path pure router.
Usage: gen_pfsense_config.py <orig_config.xml> <team_id> <out.xml> [red_seg_ip]
WAN vtnet0 = 172.31.<id>.2/30 (gw engine .1); LAN vtnet1 = 192.168.<id>.1/24;
outbound NAT disabled (engine NATs to internet); WAN pass rule for engine scoring; SSH on.
Adds the beacon C2 DNAT port-forward (LAN :4470 -> red01 on the routed segment) that the
engine-side NAT can't provide when pfSense owns the team gateway address (docs/e2e-testing.md §8.3)."""
import sys

orig, tid, out = sys.argv[1], sys.argv[2], sys.argv[3]
red_seg_ip = sys.argv[4] if len(sys.argv) > 4 else "10.200.0.10"
x = open(orig).read()

# 1) SSH on
x = x.replace("<ssh></ssh>", "<ssh><enable>enabled</enable></ssh>")
# 2) hostname
x = x.replace("<hostname>pfSense</hostname>", f"<hostname>fw-team{tid}</hostname>")

# 3) interfaces: replace whole <interfaces>...</interfaces>
new_ifaces = f"""<interfaces>
		<wan>
			<enable></enable>
			<if>vtnet0</if>
			<ipaddr>172.31.{tid}.2</ipaddr>
			<subnet>30</subnet>
			<gateway>WANGW</gateway>
			<ipaddrv6></ipaddrv6>
			<subnetv6></subnetv6>
		</wan>
		<lan>
			<enable></enable>
			<if>vtnet1</if>
			<ipaddr>192.168.{tid}.1</ipaddr>
			<subnet>24</subnet>
			<ipaddrv6></ipaddrv6>
			<subnetv6></subnetv6>
		</lan>
	</interfaces>"""
i0 = x.index("<interfaces>"); i1 = x.index("</interfaces>") + len("</interfaces>")
x = x[:i0] + new_ifaces + x[i1:]

# 4) gateways: define the WAN upstream gateway (engine)
x = x.replace("<gateways></gateways>", f"""<gateways>
		<gateway_item>
			<interface>wan</interface>
			<gateway>172.31.{tid}.1</gateway>
			<name>WANGW</name>
			<weight>1</weight>
			<ipprotocol>inet</ipprotocol>
			<defaultgw>on</defaultgw>
		</gateway_item>
	</gateways>""")

# 5) WAN pass rule FIRST (the first "\t\t<rule>" in the document must be filter's, before
#    the nat block below adds its own) so the engine's routed scoring (WAN->LAN net) is allowed
wan_rule = f"""<rule>
			<type>pass</type>
			<ipprotocol>inet</ipprotocol>
			<descr><![CDATA[Allow engine scoring + routed traffic to LAN]]></descr>
			<interface>wan</interface>
			<tracker>0100000201</tracker>
			<source>
				<any></any>
			</source>
			<destination>
				<network>lan</network>
			</destination>
		</rule>
		"""
x = x.replace("\t\t<rule>", "\t\t" + wan_rule + "<rule>", 1)

# 6) outbound NAT disabled (pure router; engine masquerades to internet) + the beacon C2
#    port-forward: boxes beacon to their gateway .1:4470 (now pfSense, not the engine), so
#    pfSense must DNAT to red01 on the routed red segment; return path is symmetric through
#    the engine (route 192.168.<id>.0/24 via 172.31.<id>.2).
nat_block = f"""<nat>
		<outbound>
			<mode>disabled</mode>
		</outbound>
		<rule>
			<interface>lan</interface>
			<ipprotocol>inet</ipprotocol>
			<protocol>tcp/udp</protocol>
			<source>
				<any></any>
			</source>
			<destination>
				<address>192.168.{tid}.1</address>
				<port>4470</port>
			</destination>
			<target>{red_seg_ip}</target>
			<local-port>4470</local-port>
			<descr><![CDATA[beacon C2 DNAT to red01 (routed segment)]]></descr>
			<associated-rule-id></associated-rule-id>
		</rule>
	</nat>
	"""
x = x.replace("\t<filter>", "\t" + nat_block + "<filter>", 1)

open(out, "w").write(x)
print(f"wrote {out} for team {tid}: WAN 172.31.{tid}.2/30 gw 172.31.{tid}.1, LAN 192.168.{tid}.1/24, "
      f"NAT off, SSH on, C2 DNAT :4470 -> {red_seg_ip}")
