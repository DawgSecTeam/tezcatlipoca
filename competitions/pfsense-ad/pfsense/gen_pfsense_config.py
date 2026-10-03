#!/usr/bin/env python3
"""Render a per-team pfSense config.xml for an in-path (pure-router) firewall.
Usage: gen_pfsense_config.py <orig_config.xml> <team_id> <out.xml>
WAN vtnet0 = 172.31.<id>.2/30 (gw engine .1); LAN vtnet1 = 192.168.<id>.1/24;
outbound NAT disabled (engine NATs to internet); WAN pass rule for engine scoring; SSH on."""
import sys
orig, tid, out = sys.argv[1], sys.argv[2], sys.argv[3]
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

# 5) outbound NAT disabled (pure router; engine masquerades to internet)
#    insert a <nat> block before <filter>
nat_block = """<nat>
		<outbound>
			<mode>disabled</mode>
		</outbound>
	</nat>
	"""
x = x.replace("\t<filter>", "\t" + nat_block + "<filter>", 1)

# 6) WAN pass rule so the engine's routed scoring (WAN->LAN net) is allowed
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

open(out, "w").write(x)
print(f"wrote {out} for team {tid}: WAN 172.31.{tid}.2/30 gw 172.31.{tid}.1, LAN 192.168.{tid}.1/24, NAT off, SSH on")
