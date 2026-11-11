---
name: remote-debug
description: "Cross-device debugging playbook. Use when the user reports a problem on a remote machine — slow service, high load, disk full, port unreachable, mystery OOM, log noise — and you need to figure out what's wrong. Provides symptom→root-cause decision trees, Linux/network diagnostics, incident response workflow, and runbook templates."
---

# remote-debug

This skill is the *application layer* of the devops-skills stack. It assumes you already have transport (via `ssh-core`, possibly through `ssh-guarded`). It tells you **what to look at** and **in what order** when something is broken on a remote host.

## When to use

- The user said something is broken / slow / down on a remote machine.
- The user wants a health check before a deploy.
- The user wants to compare a working host with a failing one.
- The user wants an incident response process and you don't have one.

Skip when the user already knows the exact command they want — that's `ssh-core` territory.

## Host model

Every script takes `<host>` as its first argument:

- `local` — inspect **this** machine. Each script serves this natively instead
  of piping a POSIX one-liner through the local shell:
  `port_check` uses `socket.connect_ex`, `tail_log` reads the file with a
  bounded seek, `diagnose_host` picks the Windows probe family on Windows.
  That matters because the one-liners (`if command -v nc …`, `tail -n`,
  `journalctl`) are unparseable by `cmd.exe`, and a probe that never ran used
  to be indistinguishable from a probe that answered "filtered".
- any other alias — forwarded to `ssh-core`'s `ssh_execute.py` as one JSON
  envelope per probe. This is the normal case for a remote host.

Four behaviours worth knowing about the local route:

| Situation | Result |
|---|---|
| a POSIX-only command on a non-POSIX host (`--unit` on Windows) | `failure_class: "local_shell_error"`, exit 1, and a message naming the alternative |
| a local binary the script shells out to is absent | `failure_class: "dependency_missing"`, exit 3 |
| a local file does not exist | `failure_class: "local_not_found"`, exit 1 |
| a POSIX-absolute path on a Windows local route (`/etc/hostname`) | `skipped: true`, `skipped_reason: "posix_path_on_windows"`, `failure_class: null`, exit 0 — listed in `data.skipped_hosts`, and it does not fail the run |

A skip is not a failure. `tail_log.py --hosts lab-240,local /etc/hostname` on
Windows is `success: true` / exit 0, with `lab-240` in `data.ok_hosts` and
`local` in `data.skipped_hosts`; the remote half still contributes its lines.
When you really mean this machine, give it a Windows path:
`tail_log.py --hosts local C:/Windows/win.ini` (or
`file://local/C:/Windows/win.ini`, the log-aggregator spelling). The same three
bucket arrays (`ok_hosts` / `failed_hosts` / `skipped_hosts`) are emitted by
`ssh_cluster.py`, so one rule reads both.

A probe that **never ran** is always a `source-error`, never a status. This is
deliberate: reporting a firewall drop when the shell merely choked on the
syntax sends an operator to the wrong layer.

### Target OS

`diagnose_host.py` has two probe families and picks one per run:

| `--target-os` | Behaviour |
|---|---|
| `auto` (default) | one `uname -s` detector probe, then the matching family. A failed detector keeps the Linux family. |
| `linux` | `uptime` / `nproc` + `/proc/loadavg` / `df -P` / `free -m` / `ps -eo stat` |
| `windows` | PowerShell via `-EncodedCommand`: boot time, CPU %, `Win32_LogicalDisk`, memory, unresponsive processes |

Three consequences worth knowing:

- A Windows target has **no load average**. Its `load` probe returns
  `parsed.cpu_pct`, and `_score()` compares that against 80 / 95 % instead of
  the load-per-core multipliers.
- The probes are base64(UTF-16LE) so the command survives both transport paths
  (a POSIX shell on the remote side, `cmd /c` locally) without quoting rules.
- Each probe script also sets `[Console]::OutputEncoding = UTF8`. A Chinese
  Windows PowerShell writes GBK otherwise, and the capturing side decodes
  UTF-8 — the date came back as replacement characters.
- The envelope carries `data.target_os`, and the summary line reads
  `diagnose_host <host> (windows): …` so a mixed fleet is not ambiguous.

## Entry decision tree

```
What does the user have?
│
├─ A symptom (vague)
│   e.g. "the API is slow", "logs look weird", "users complain"
│   → Go to "Symptom triage" below
│
├─ A specific signal
│   e.g. "load average is 30", "disk is 95%", "port 5432 refuses"
│   → references/common_issues.md, search the signal
│
├─ A page / alert
│   e.g. PagerDuty fired, SLO burned, monitoring red
│   → references/incident_response.md (SEV classification first)
│
└─ Preventive ("can you check…?")
    → diagnose_host.py <alias> --json
    → review the output, then decide
```

## Symptom triage (the first 5 minutes)

When the user gives you only a vague complaint, **always** run this baseline first:

```
diagnose_host.py <alias> --json
```

It returns the four golden signals plus the obvious traps:
- Load average, CPU %, top processes by CPU
- Memory: used / available / cached / swap
- Disk: per-mount usage + inodes
- Network: established TCP conns, listeners, errors, drops
- Zombies, recent OOMs (last 24 h from `dmesg`), recent service restarts

Read the `summary` field first. It scores each domain `ok | warn | crit` so you can branch fast.

## Cross-host comparison

When one host is broken and another is fine:

```
compare_across_hosts.py prod-web-01 prod-web-02 \
  --files /etc/nginx/nginx.conf,/etc/sysctl.conf \
  --commands "nginx -V" "uname -r" "systemctl list-units --state=failed"
```

Output is a side-by-side diff, with a `summary` field listing every domain that differs. This is by far the highest-leverage tool when "it works on the other one".

## Log surgery

```
tail_log.py <alias> /var/log/nginx/error.log --since 15min --grep "5[0-9][0-9]"
tail_log.py <alias> --unit nginx --since "5min ago" --lines 200
```

When you need to pull from multiple hosts at once:

```
tail_log.py --hosts web-1,web-2,web-3 /var/log/myapp/app.log --lines 200
```

Prefixes each line with the host alias so you don't lose track.

### Follow

```
tail_log.py <alias> /var/log/myapp/app.log --follow
tail_log.py --hosts web-1,web-2 --unit nginx --follow --follow-timeout 120
```

`--follow` switches the remote command to `tail -F` (path mode, survives log
rotation) or `journalctl -f` (unit mode) and relays each line as it arrives.
It ends on **Ctrl-C** or after `--follow-timeout` seconds (default 600, floor
5). `--follow --json` still emits exactly one JSON document at the end, so a
wrapper script can parse it; the live lines go to stderr in that mode.

Path mode reads the file directly — use it for application logs that don't go through journald (nginx error.log, your app's log file). `--unit` shells out to `journalctl -u`, which is required for systemd-managed services and is the only option on modern distros (Debian 12+, RHEL 9+, Ubuntu 22.04+) where `/var/log/syslog` no longer exists because journald replaced it.

## Port and reachability

```
port_check.py <alias> --target db-host --ports 5432,6379,9092
```

Runs `nc -zv` from `<alias>` (not from your laptop). Use this whenever the question is "can the app server reach the database?" — checking from your machine doesn't answer it.

For a TCP matrix across many sources and targets:

```
port_check.py --from web-1,web-2,web-3 --to db-1,cache-1 --ports 5432,6379
```

### UDP

```
port_check.py <alias> --target dns-1 --ports 53 --udp
```

UDP has no handshake, so the answer is weaker by construction and the tool
says so rather than guessing:

| Result | Meaning |
|---|---|
| `refused` | the target answered with ICMP port-unreachable — definitively closed |
| `open-or-filtered` | silence: the port is open, **or** a firewall dropped the packet |
| `source-error` | no `nc` on the source host, so nothing was probed |

The envelope carries `data.protocol = "udp"` and
`data.summary.note` repeats the caveat. Do not report
`open-or-filtered` as "open" to a user.

## Pairing with ssh-guarded

If `ssh-guarded` is installed and you're on a production host:

- **Reads** (`diagnose_host`, `tail_log`, `port_check`, `compare_across_hosts`) stay direct. They don't write.
- **Remediations** suggested by these scripts must go through `request_command` → `run_request`. Never let `remote-debug` execute a fix directly; it surfaces the fix as a *suggestion*, and the user decides whether to draft a request.

There is no safety-mode env switch: these scripts are read-only and the
suggestion text is prose. Turning a suggestion into a request is the human's
step (`ssh-guarded`'s `request_command.py`), not an env var.

## Where to read more

- `references/common_issues.md` — symptom → cause → diagnostic → remedy
- `references/linux_diagnostics.md` — the canonical "USE method" walk-through for Linux hosts
- `references/network_debug.md` — DNS, TCP, MTU, NAT, conntrack
- `references/incident_response.md` — SEV1–SEV4, on-call structure, post-mortem template
- `assets/runbooks/` — runbook templates the user can fill in for their own services
- `assets/tmux/` — recommended tmux config for multi-host debugging sessions
- `assets/systemd/` — service unit templates that fail loudly (which is what you want)
