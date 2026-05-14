# Network debug

DNS, TCP, MTU, NAT, conntrack — the common rabbit holes when a remote application is "almost working".

## Layer 1: the host can see the network at all?

```bash
ip -br link                       # interface state
ip -br addr                       # IPs assigned
ip route                          # default route present?
ethtool <iface>                   # link up, speed/duplex sane
ethtool -S <iface> | grep -iE "drop|err|fail"
```

If `Link detected: no`, you're done diagnosing in software.

## Layer 2: ARP and neighbors

```bash
ip neigh                          # ARP table
arping -I <iface> <gateway-ip>    # is the gateway responsive at L2?
```

A long-lived ARP entry for the gateway in `STALE` state can cause minute-long stalls after suspend/resume.

## Layer 3: routing and connectivity

```bash
ping -c 3 -W 2 <target>
mtr -rwc 50 <target>             # who drops where, with packet loss %
tracepath -n <target>            # also probes path MTU
ip route get <target>            # exact route the kernel will use
```

`mtr -rwc 50` is the single best tool for "the path is flaky". 50 packets gives you statistically meaningful loss numbers per hop.

## Layer 4: TCP / UDP

```bash
nc -zv <target> <port>            # TCP open?
nc -uzv <target> <port>           # UDP — but remember UDP is fire-and-forget
ss -tan state established '( dst <target> )'
ss -tan | awk '{print $1}' | sort | uniq -c   # TCP state distribution
ss -s                              # socket summary
nstat -az | grep -iE "retrans|loss|reset"
```

**TCP states cheat sheet:**

| State | Meaning | Worry if |
|---|---|---|
| `ESTAB` | normal | per-socket bytes-in-flight is huge |
| `TIME-WAIT` | recently closed by us | tens of thousands of them (port exhaustion) |
| `CLOSE-WAIT` | peer closed, our app hasn't | growing — app isn't closing sockets |
| `SYN-SENT` | we sent SYN, no SYN-ACK | many — firewall dropping or service down |
| `SYN-RECV` | got SYN, sent SYN-ACK | many — possible SYN flood or app blocked |

## Conntrack saturation

The classic "everything was fine until suddenly it wasn't":

```bash
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max
# When count/max > 0.85 you start seeing drops
dmesg | grep -i "nf_conntrack"
```

Fix: raise `nf_conntrack_max` (and `hashsize` accordingly), or remove conntrack from paths that don't need it (`NOTRACK` rule).

## MTU and PMTU black holes

Symptom: small packets work, large transfers stall. Classic with VPNs, GRE, IPSec.

```bash
# Probe path MTU
tracepath -n <target>             # last line: "pmtu N"
ping -M do -s 1472 <target>       # 1472 + 28 (ICMP+IP) = 1500
# If it fails with EMSGSIZE, the path can't carry your MTU.

# Lower the interface MTU as a test
sudo ip link set <iface> mtu 1400
```

Cause: ICMP "fragmentation needed" being dropped by a firewall along the path. Permanent fix: ensure ICMP type 3 code 4 passes everywhere, or use MSS clamping (`iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu`).

## DNS in detail

```bash
# Direct: bypass local resolver
dig @8.8.8.8 example.com
# What does the local resolver do?
dig example.com
# Trace the whole chain
dig +trace example.com
# Check timing
dig example.com | grep "Query time"
# What's the resolver actually using?
cat /etc/resolv.conf
resolvectl status   # systemd
nscd -g 2>/dev/null && echo "nscd running"
```

DNS slowness is asymmetric: a 200 ms resolution feels like every page is slow because everything resolves at the start of every connection.

## NAT / SNAT

When the host is behind NAT and outbound looks broken:

```bash
# What source IP/port does the host see itself as?
curl -s ifconfig.me
# What's actually being sent on the wire?
sudo tcpdump -ni <iface> -c 10 host <target> and port <port>
# Is conntrack masquerading us correctly?
sudo conntrack -L | grep <target>
```

A surprising amount of "weird connectivity" in containers is conntrack confusion. `conntrack -L` answers the "what does the kernel think is happening" question.

## When nothing else helps: tcpdump

```bash
# 10 packets, no DNS resolution, both directions
sudo tcpdump -ni <iface> -c 10 -nn host <target> and port <port>
# Save for Wireshark
sudo tcpdump -ni <iface> -w /tmp/cap.pcap -c 1000 host <target>
# Filter for retransmissions:
sudo tcpdump -ni <iface> 'tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-ack and tcp[20:4] = 0'
```

Capture with `-c <N>` always — it's too easy to fill `/var` otherwise.

## Hosts can't talk through your tunnel

If `ssh-core`'s `ssh_tunnel.py` is up but you can't reach `localhost:<port>`:

1. Wrong bind address (covered in `ssh-core`'s troubleshooting).
2. The remote service binds to `127.0.0.1` only — use `--remote-host <real-iface-ip>`.
3. SELinux on the remote denies the local forward (`setsebool -P sshd_enable_forwarded yes` rarely, but seen on hardened RHEL).
4. The daemon died; the listener is gone. `ssh_tunnel.py status`.
