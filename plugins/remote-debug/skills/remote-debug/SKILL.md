---
name: remote-debug
description: Cross-device debugging playbook. Use when the user reports a problem on a remote machine — slow service, high load, disk full, port unreachable, mystery OOM, log noise — and you need to figure out what's wrong. Provides symptom→root-cause decision trees, Linux/network diagnostics, incident response workflow, and runbook templates.
---

# remote-debug

This skill is the *application layer* of the devops-skills stack. It assumes you already have transport (via `ssh-core`, possibly through `ssh-guarded`). It tells you **what to look at** and **in what order** when something is broken on a remote host.

## When to use

- The user said something is broken / slow / down on a remote machine.
- The user wants a health check before a deploy.
- The user wants to compare a working host with a failing one.
- The user wants an incident response process and you don't have one.

Skip when the user already knows the exact command they want — that's `ssh-core` territory.

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
tail_log.py <alias> /var/log/syslog --follow --since 5min
```

When you need to watch multiple hosts at once:

```
tail_log.py --hosts web-1,web-2,web-3 /var/log/myapp/app.log --follow
```

Prefixes each line with the host alias so you don't lose track.

## Port and reachability

```
port_check.py <alias> --target db-host --ports 5432,6379,9092
```

Runs `nc -zv` from `<alias>` (not from your laptop). Use this whenever the question is "can the app server reach the database?" — checking from your machine doesn't answer it.

For a TCP matrix across many sources and targets:

```
port_check.py --from web-1,web-2,web-3 --to db-1,cache-1 --ports 5432,6379
```

## Pairing with ssh-guarded

If `ssh-guarded` is installed and you're on a production host:

- **Reads** (`diagnose_host`, `tail_log`, `port_check`, `compare_across_hosts`) stay direct. They don't write.
- **Remediations** suggested by these scripts must go through `request_command` → `run_request`. Never let `remote-debug` execute a fix directly; it surfaces the fix as a *suggestion*, and the user decides whether to draft a request.

The scripts respect the `DEVOPS_SAFETY_MODE=guarded` env var. When set, suggestions print as request-shaped JSON ready to pipe into `ssh-guarded`.

## Where to read more

- `references/common_issues.md` — symptom → cause → diagnostic → remedy
- `references/linux_diagnostics.md` — the canonical "USE method" walk-through for Linux hosts
- `references/network_debug.md` — DNS, TCP, MTU, NAT, conntrack
- `references/incident_response.md` — SEV1–SEV4, on-call structure, post-mortem template
- `assets/runbooks/` — runbook templates the user can fill in for their own services
- `assets/tmux/` — recommended tmux config for multi-host debugging sessions
- `assets/systemd/` — service unit templates that fail loudly (which is what you want)
