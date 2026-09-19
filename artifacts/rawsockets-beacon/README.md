# rawsockets-beacon — How to Run It

A defensive-security test beacon that sends traffic through a **Linux raw
socket** (`socket(AF_INET, SOCK_RAW, IPPROTO_RAW)`). Its purpose is to let you
validate that your **kernel-level firewall module** can (1) **detect** that a
raw socket was created, and (2) **block** packets emitted through that raw
socket.

This guide covers everything needed to build and run it on your own test
hosts, capture the traffic, and confirm your firewall's detection/block
behavior.

> ⚠️ **Run this only on systems you own / your own network segment.** It forges
> IP/TCP headers and is the same primitive malware beacons use — exactly why
> your firewall should be tested against it. Do not point it at hosts you do
> not control.

---

## 1. Prerequisites

| Requirement | Why |
|-------------|-----|
| Linux host (this is a Linux raw-socket tool) | `SOCK_RAW` sockets are the point |
| `gcc` (or any C99 compiler) | To build from source |
| `make` (optional — see Build) | Convenience wrapper around `gcc` |
| **root, or `CAP_NET_RAW`** | Required to open a raw socket |
| A reachable destination host | Where you observe the packets |

Check for a compiler:

```sh
gcc --version
```

If you have no `gcc`, install it (Debian/Ubuntu):

```sh
sudo apt-get install -y gcc make
```

---

## 2. Build

Clone or copy the project, then:

```sh
cd rawsockets-beacon
make
```

This runs `gcc -Wall -Wextra -O2 -std=gnu99 -o beacon beacon.c` and produces
the `./beacon` binary. A clean build prints no warnings.

To rebuild from scratch:

```sh
make clean && make
```

---

## 3. Grant raw-socket privilege

The beacon needs `CAP_NET_RAW`. Two options:

**Option A — root (simplest).** Run every invocation with `sudo`.

**Option B — least privilege (recommended).** Grant just the capability once
so you don't need full root each time:

```sh
make cap
# equivalent: sudo setcap cap_net_raw+ep ./beacon
```

Verify the capability is applied:

```sh
getcap ./beacon
# expect: ./beacon = cap_net_raw+ep
```

> If you set the capability, do **not** also run with `sudo` — it works either
> way, but pick one so your test logs aren't confusing.

---

## 4. Run the beacon

Basic one-shot run (10 packets, one every 2 seconds):

```sh
sudo ./beacon -t 192.168.1.10 -p 4444 -i 2 -n 10 -b "fw-test-01"
```

If you used `make cap`, drop the `sudo`:

```sh
./beacon -t 192.168.1.10 -p 4444 -i 2 -n 10 -b "fw-test-01"
```

### Flags

| Flag | Meaning | Default |
|------|---------|---------|
| `-t <ip>` | Destination IP (required) | — |
| `-p <port>` | Destination TCP port | `4444` |
| `-s <ip>` | Forged source IP (see note below) | `0.0.0.0` |
| `-i <secs>` | Beacon interval in seconds | `2` |
| `-n <count>` | Number of beacons (`0` = run forever) | `10` |
| `-b <str>` | Beacon identifier string | `fw-test` |
| `-m <iface>` | L2 bypass mode: inject full Ethernet frames via `AF_PACKET` on `<iface>`, bypassing netfilter's `LOCAL_OUT` hook so iptables OUTPUT rules cannot see the packets | off |
| `-M <mac>` | Next-hop MAC (`aa:bb:cc:dd:ee:ff`); skips ARP resolution (only meaningful with `-m`) | ARP-resolved |
| `-v` | Print a hex dump of each packet on the wire | off |
| `-h` | Show help | — |

### What you should see

If the raw socket opened and packets are being sent:

```text
[beacon] raw socket opened (fd=3)
[beacon] target 192.168.1.10:4444  interval 2s  count 10  id "fw-test-01"
[beacon] 0.0.0.0:12345 -> 192.168.1.10:4444  SYN  seq=1
[beacon] 0.0.0.0:45678 -> 192.168.1.10:4444  SYN  seq=2
...
[beacon] done
```

Each `[beacon] ... SYN` line is one forged packet emitted.

### L2 bypass mode (`-m`) — why it evades iptables

The default mode uses `socket(AF_INET, SOCK_RAW, IPPROTO_RAW)`. Packets sent
through it traverse the kernel IP stack and hit netfilter's **`LOCAL_OUT`
hook** — the exact hook the iptables OUTPUT chain attaches to. That is why
iptables can block the default-mode beacon.

`-m <iface>` switches to **AF_PACKET** (`socket(AF_PACKET, SOCK_RAW)`): the
beacon crafts the complete Ethernet frame itself and injects it straight to
the device via `dev_queue_xmit()`, which **never passes through the IP stack
or the `LOCAL_OUT` netfilter hook**. iptables OUTPUT rules therefore do not
see these packets. This is the classic raw-socket evasion primitive — a kernel
firewall that only hooks `LOCAL_OUT` will miss it and must hook at the
**driver / XDP / TC** layer instead.

```sh
sudo ./beacon -t 192.168.1.10 -p 4444 -m eth0 -i 2 -n 10 -b "fw-l2-test"
```

When `-m` is given, the beacon:

1. Reads your routing table (`/proc/net/route`) to find the next hop to the
   target (on-link, or the gateway for remote hosts).
2. Learns that next hop's MAC with an ARP request (or uses `-M <mac>` to skip
   ARP if the firewall drops ARP too).
3. Sends each beacon as a full Ethernet frame addressed to that MAC.

`-M` is the escape hatch when ARP is unreliable (e.g. a firewall that filters
ARP): `sudo ./beacon -t 192.168.1.10 -p 4444 -m eth0 -M aa:bb:cc:dd:ee:ff`.
The MAC must be the *next hop* (the gateway for remote targets).

### Source-IP note (`-s`)

With `IPPROTO_RAW` you *can* forge the source address with `-s`. However, the
kernel may route/rewrite the source based on your interface and routing table.
For a clean test where the firewall sees the **real** local address, run
without `-s` (the kernel replaces `0.0.0.0` with the local address).

---

## 5. Verify the traffic actually left (packet capture)

Run a capture on the destination host while the beacon is running. On Linux:

```sh
sudo tcpdump -i eth0 'tcp port 4444' -XX
```

You are looking for the beacon's `BEA1` marker in the payload. A SYN packet
with payload bytes starting `42 45 41 31` (`BEA1`) confirms the packet arrived.

If `tcpdump` is not installed:

```sh
sudo apt-get install -y tcpdump
```

To capture on a specific interface instead of `eth0`, run `ip a` or
`ip link show` first to find the interface name.

---

## 6. What your firewall should be checking

While the beacon runs, verify these three things against your kernel module's
logs:

1. **Socket creation** — the `socket(AF_INET, SOCK_RAW, IPPROTO_RAW)` syscall.
   The beacon prints `[beacon] raw socket opened (fd=N)`; your module should
   log the raw-socket creation event.

2. **Packet egress** — each `sendto()` emits one raw packet. **Important:** if
   your firewall blocks raw egress, `sendto()` may still return success (the
   kernel copied the buffer into the stack) but the packet never reaches the
   wire. This is exactly why step 5 (capture at the destination) matters: if
   the capture shows **no** `BEA1` packets but the beacon keeps printing
   `SYN` lines, your firewall is blocking raw egress — that is the desired
   test outcome.

3. **Periodicity** — the constant `-i` cadence is the giveaway signature of a
   beacon. A periodic, fixed-interval burst of forged SYN packets is highly
   correlatable across your telemetry.

---

## 7. Testing detection vs. blocking

**Detection test** — run the beacon and check whether your firewall logs the
raw-socket event. The `socket(...)` line appearing in your module's log
proves detection works.

**Blocking test** — run the beacon while capturing at the destination:
- Beacon prints `SYN` lines (sends succeed), **and**
- Destination capture shows **no** `BEA1` packets.

That combination means the firewall is blocking raw-socket egress without
crashing the sending process — the desired behavior.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `socket(...) failed: Operation not permitted` | No `CAP_NET_RAW` | Run with `sudo`, or `make cap` (see step 3) |
| `invalid target address: <ip>` | Bad `-t` value | Use a dotted-quad IPv4 like `192.168.1.10` |
| `interval must be >= 1 second` | `-i 0` or negative | Use `-i 1` or larger |
| `packet buffer too small` | Very long `-b` id | Use a shorter beacon id (the buffer is 128 bytes) |
| No `BEA1` at destination | Firewall blocking, wrong interface, or wrong port filter | Check firewall logs; confirm interface; verify the `tcp port` matches `-p` |

### Example: block-only run (run forever)

```sh
sudo ./beacon -t 192.168.1.10 -p 4444 -i 5 -n 0 -b "fw-persist"
# Ctrl-C to stop
```

---

## 9. Cleanup

Stop any running beacon with `Ctrl-C` (or `kill` the process). Remove the
build artifact if you no longer need it:

```sh
make clean
```

---

## Reference: packet layout

Wire format of each beacon (network byte order):

Default mode:

```
[ IPv4 header (20) ][ TCP header (20) ][ beacon payload ]
```

L2 bypass mode (`-m`):

```
[ Ethernet header (14) ][ IPv4 header (20) ][ TCP header (20) ][ beacon payload ]
```

The Ethernet header carries the resolved next-hop MAC and your interface's
MAC, ethertype `0x0800` (IPv4).

Payload layout:

```
bytes 0..3   "BEA1"  (magic marker)
bytes 4..7   reserved (zeros)
then         beacon id string, NUL-terminated
then         ASCII Unix timestamp, NUL-terminated
```
