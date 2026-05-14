# Linux diagnostics — the USE method, applied

When the user reports "something is wrong on this host" and gives you nothing else, work through this list in order. Brendan Gregg's [USE method](https://brendangregg.com/usemethod.html) (Utilization / Saturation / Errors) is the spine.

## 60-second triage

In the first 60 seconds, run these. Most of the time the answer is here:

```bash
uptime
dmesg -T | tail -20
vmstat 1 5
mpstat -P ALL 1 2
pidstat 1 2
iostat -xz 1 2
free -m
sar -n DEV 1 2          # if sysstat installed
sar -n TCP,ETCP 1 2
top -bn1 | head -30
```

`diagnose_host.py --json` runs these in parallel and returns a structured summary. Use it as your default first step.

## Resource walk-through

### CPU

| Question | Command | What you're looking for |
|---|---|---|
| How busy? | `mpstat -P ALL 1 5` | `%usr + %sys + %iowait` per CPU |
| Saturated? | `uptime`, `vmstat 1` | load > #CPUs, `r` column > #CPUs |
| Errors? | `dmesg`, `mcelog` | MCEs, throttle messages |
| Who? | `pidstat 1 5`, `top -H` | top processes / threads |

If CPU is high but no user process owns it, suspect kernel (look at `softirq`/`hardirq` in `mpstat`).

### Memory

| Question | Command | What you're looking for |
|---|---|---|
| How busy? | `free -m`, `/proc/meminfo` | `available`, `Cached`, `SReclaimable` |
| Saturated? | `vmstat 1`, PSI memory | `si`/`so` non-zero, PSI `some > 0` |
| Errors? | `dmesg`, OOM logs | "Out of memory: Killed process" |
| Who? | `ps aux --sort=-rss | head` | top RSS, top cgroup `memory.current` |

Cache memory is **not** "used" in the sense most users mean. `available` is the number to watch.

### Disk

| Question | Command | What you're looking for |
|---|---|---|
| How busy? | `iostat -xz 1 5` | `%util`, `aqu-sz`, `r/s + w/s` |
| Saturated? | same | `await` > device-class latency baseline |
| Errors? | `dmesg`, `/proc/diskstats` | EXT4-fs errors, NVMe controller errors |
| Who? | `iotop -aoP`, `pidstat -d` | top processes by read/write |

A device at 100% `%util` is **not** necessarily saturated — modern SSDs can have many in-flight requests. Latency (`await`) tells you about saturation.

### Network

| Question | Command | What you're looking for |
|---|---|---|
| How busy? | `sar -n DEV 1`, `nstat` | bytes/s, packets/s |
| Saturated? | `ethtool -S <iface>` | drops, fifo overruns |
| Errors? | `ip -s link`, `netstat -s` | RX/TX errors, retrans, listen drops |
| Who? | `iftop`, `nethogs`, `ss -tip` | top talkers |
| TCP health | `nstat -az | grep -i retrans` | `TcpExtTCPLossProbes`, `Retrans` |

Conntrack saturation is the silent killer. Check:
```bash
cat /proc/sys/net/netfilter/nf_conntrack_count
cat /proc/sys/net/netfilter/nf_conntrack_max
```

When `count` is within 10% of `max`, new connections will start being dropped. Look for `nf_conntrack: table full, dropping packet` in `dmesg`.

## Process and service walk-through

### Find a wedged process

```bash
ps -eL -o stat,pid,tid,comm | awk '$1 ~ /D/'   # uninterruptible sleep
cat /proc/<pid>/stack                          # what kernel call?
cat /proc/<pid>/wchan
ls -l /proc/<pid>/fd | head -50                # what's it holding open?
```

### Find a noisy service

```bash
systemctl list-units --state=running --type=service
systemd-cgtop                                  # per-cgroup CPU/mem/IO
journalctl --since "10 min ago" --priority=warning
```

## Network "where's the packet going" decision tree

```
ping <target>?
├─ NO  → traceroute, then mtr to see where it dies
└─ YES → can you reach the port?
         nc -zv <target> <port>
         ├─ NO  → port closed / firewall / app not bound
         └─ YES → can you complete a handshake?
                  curl -v / openssl s_client / your protocol's debug tool
                  ├─ NO  → TLS, MTU, protocol mismatch
                  └─ YES → it's app behavior; go to logs
```

## When the host is reachable but everything is slow

In order:

1. **PSI**: `/proc/pressure/{cpu,io,memory}`. Any `some > 50` or `full > 10`? You have saturation.
2. **Time skew**: `chronyc tracking` / `timedatectl status`. Big skew breaks TLS, kerberos, monitoring.
3. **DNS**: `dig +trace` for the hostnames the app uses. Slow DNS feels like slow everything.
4. **Conntrack**: see above.
5. **Open files / RLIMIT_NOFILE** for the service process. `cat /proc/<pid>/limits | grep -i 'open files'`.
6. **Threads / RLIMIT_NPROC**. `ps -eL | wc -l` vs `cat /proc/<pid>/limits`.
