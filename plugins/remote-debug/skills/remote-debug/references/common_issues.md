# Common issues — symptom → cause → diagnostic → remedy

A working library, not exhaustive. When you fix something not in here, add it.

Each entry has the same structure so it's easy to skim:

> **Symptoms** — what the user typically reports
> **Common causes** — ordered by frequency in the wild
> **Diagnostic** — exact commands to run
> **Remedy** — what to do once you've identified the cause
> **Prevention** — what to change so it doesn't recur

---

## High load average, CPU not pinned

> **Symptoms**: `uptime` shows load > #CPUs, but `top` shows low CPU%.
>
> **Common causes**:
> 1. I/O wait — disk saturated, network FS unresponsive
> 2. Thread thrash — many threads in `D` (uninterruptible sleep)
> 3. Memory pressure causing PSI stalls
> 4. Container CPU throttling (cgroup quota exhausted, not host CPU)
>
> **Diagnostic**:
> ```
> # I/O wait?
> top -bn1 | head -5            # %wa column on the CPU line
> iostat -xz 2 5                # await, %util per device
> # Threads stuck in D?
> ps -eL -o stat,pid,comm | awk '$1 ~ /D/'
> # PSI stalls?
> cat /proc/pressure/{cpu,io,memory}
> # cgroup throttling?
> grep . /sys/fs/cgroup/cpu.stat
> ```
>
> **Remedy**: depends on cause. For disk I/O, look at the top writer (`iotop`/`pidstat -d`) — usually a noisy log or a runaway query. For cgroup throttling, raise the quota or fix the workload.
>
> **Prevention**: PSI alerts in your monitoring. I/O baseline per host.

---

## Disk full, but `df` and `du` disagree

> **Symptoms**: `df` says 100% used, `du` of all files adds up to much less.
>
> **Common causes**:
> 1. Deleted-but-open file (process still holds an fd to a deleted log)
> 2. Filesystem reserved blocks (ext4 default 5% for root)
> 3. Snapshots / overlay layers (ZFS, btrfs, overlayfs)
> 4. Inodes exhausted (df reports normally but writes fail)
>
> **Diagnostic**:
> ```
> df -h <mount>
> df -i <mount>                              # inodes
> sudo lsof +L1 | head -50                   # deleted-but-open
> sudo find / -xdev -type f -size +500M 2>/dev/null
> # ZFS / btrfs / overlay
> mount | grep <mount>
> ```
>
> **Remedy**: restart the holder of the deleted fd, or `truncate -s 0` the active log if you can't restart. For inodes, find the offender with `for d in /*; do echo $(find $d -xdev | wc -l) $d; done | sort -n | tail`.
>
> **Prevention**: log rotation that uses `copytruncate` for processes that don't reopen, alerts on inode usage independently from byte usage.

---

## OOM killer fires, but `free` looked fine moments earlier

> **Symptoms**: `dmesg` shows `Out of memory: Killed process N (name)`, process gone.
>
> **Common causes**:
> 1. Sudden allocation spike (e.g., loading a huge file)
> 2. cgroup memory limit (much smaller than host RAM)
> 3. Fragmentation — order-N allocation fails despite free pages
> 4. Memory accounted to kernel slab (rare, look at `slabtop`)
>
> **Diagnostic**:
> ```
> dmesg -T | grep -A 30 -i "killed process"
> journalctl -k --since "30 min ago" | grep -i oom
> # cgroup limits?
> systemctl status <unit> | grep -i memory
> cat /sys/fs/cgroup/<path>/memory.max
> cat /sys/fs/cgroup/<path>/memory.events
> ```
>
> **Remedy**: raise the limit, fix the leak, or add swap if the workload is genuinely bursty. Note `oom_score_adj` if some processes should die before others.
>
> **Prevention**: cgroup memory alerts on `memory.events:oom`. Heap profiling (jemalloc, pprof) for long-running services.

---

## Service won't start, no useful error

> **Symptoms**: `systemctl start <svc>` returns non-zero. `status` shows `failed`.
>
> **Common causes**:
> 1. Bind to a port already in use
> 2. EnvironmentFile missing or unreadable
> 3. ExecStart path not executable / wrong arch / missing libs
> 4. SELinux / AppArmor blocking
> 5. Resource limits — too many open files, RLIMIT_NOFILE
>
> **Diagnostic**:
> ```
> journalctl -u <svc> --since "5 min ago" -n 200 -o cat
> systemctl cat <svc>
> # Port collision?
> ss -tlnp | grep ':<port>\b'
> # Binary works at all?
> sudo -u <runas> <ExecStart>
> ldd <binary>
> # SELinux?
> ausearch -m AVC -ts recent
> ```
>
> **Remedy**: read the journal carefully; the answer is almost always there in the last 50 lines.
>
> **Prevention**: `systemd-analyze verify <unit>` in CI.

---

## SSH connection hangs at "kex"

> **Symptoms**: `ssh -v` shows `debug1: SSH2_MSG_KEXINIT sent` then silence.
>
> **Common causes**:
> 1. MTU / fragmentation — VPN or tunnel reducing MSS
> 2. Stateful firewall dropping idle connections mid-handshake
> 3. Server-side `MaxStartups` saturated
> 4. Slow PRNG on the server (rare, embedded only)
>
> **Diagnostic**:
> ```
> ssh -vvv <host> 2>&1 | tee /tmp/ssh.log
> # From the client, MTU probe:
> tracepath -n <host>
> # Reduce MSS to test:
> ssh -o IPQoS=throughput -o MACs=hmac-sha2-256 -o KexAlgorithms=curve25519-sha256 <host>
> # On the server side (if reachable):
> ss -ant state listening 'sport = :22'
> grep -i maxstart /etc/ssh/sshd_config
> ```
>
> **Remedy**: lower client MTU on the tunnel (`ip link set <dev> mtu 1400`), or raise server `MaxStartups`.
>
> **Prevention**: keep SSH `ClientAliveInterval` short on long-running mgmt connections.

---

## DNS works locally, fails on the host

> **Symptoms**: `dig @8.8.8.8 example.com` works, `dig example.com` does not.
>
> **Common causes**:
> 1. `/etc/resolv.conf` overridden by systemd-resolved without `127.0.0.53` reachable
> 2. NetworkManager wrote a stale `resolv.conf`
> 3. Container's `/etc/resolv.conf` pointing at the host's old resolver
> 4. `nsswitch.conf` order puts `files` before `dns` and `/etc/hosts` has the wrong entry
>
> **Diagnostic**:
> ```
> cat /etc/resolv.conf
> cat /etc/nsswitch.conf | grep hosts
> systemd-resolve --status   # or resolvectl status
> getent hosts example.com
> dig +trace example.com
> ```
>
> **Remedy**: pin a resolver (`resolvectl dns <iface> 1.1.1.1`), or fix `/etc/hosts` if it's wrong.
>
> **Prevention**: container images that ship a correct `resolv.conf`. Monitoring on resolution latency.

---

## TLS works in browser, fails from curl on the server

> **Symptoms**: `curl https://api.example.com` errors with `unable to get local issuer certificate`.
>
> **Common causes**:
> 1. Missing CA bundle (`ca-certificates` package not installed)
> 2. Out-of-date CA bundle (Let's Encrypt root rotation)
> 3. MITM proxy in the network path injecting its own root, which the host doesn't trust
> 4. SNI mismatch — the host has no SNI support (old curl) and the server requires it
>
> **Diagnostic**:
> ```
> curl -vk https://api.example.com 2>&1 | grep -E "subject|issuer|verify"
> openssl s_client -connect api.example.com:443 -servername api.example.com < /dev/null
> ls -la /etc/ssl/certs/ca-certificates.crt
> ```
>
> **Remedy**: `apt-get install --reinstall ca-certificates` and `update-ca-certificates`. For a corporate MITM, install the corporate root into `/usr/local/share/ca-certificates/` and update.
>
> **Prevention**: monthly `ca-certificates` updates in your image baseline.
