# ssh-core recipes

Copy-paste command templates for the three families we get asked about most:
systemd/journald, docker, and read-only Kubernetes. Everything here is
read-only unless a recipe says **DESTRUCTIVE** in its heading.

The remote commands are ordinary shell one-liners; the point of the page is the
*invocation form* — how to run them through `ssh-core` so the result is one
JSON envelope, host identifiers are redacted, and nothing lands in shell
history.

## When the command gets complicated: send a script, do not build one

Every command crosses **three** quoting layers: your local shell (pwsh or
bash) → `ssh-core`'s argv → the remote `sh`. An `awk '{print $1}'`, a nested
quote, a `$` that must survive to the far side, or a `grep -E 'a|b'` will be
eaten by one of them sooner or later, and the failure looks like

```
awk: cmd. line:1: {print \\ :\\substr(\\,index(\\,\\))}
bash: -c:行0: 寻找匹配的"时遇到了未预期的文件结束符
```

The reliable answer is `--script-file`: write the script locally, let
`ssh-core` pipe it into `bash -s` on the far side. The script never touches
argv, so there is no quoting left to get wrong.

```bash
# local: probe.sh  (any editor, any line endings)
#   #!/bin/bash
#   journalctl --no-pager -n 5000 \
#     | awk '$0 ~ /error|panic/ {print NR": "$0}' \
#     | tail -40

python scripts/ssh_execute.py lab-231 --script-file ./probe.sh --json
scripts\run.cmd ssh_execute.py lab-231 --script-file .\probe.sh --json
```

What the envelope tells you:

- `data.command` is `bash -s` (or `sh -s` when the shebang is not bash);
- `data.script_file` names the local file that ran;
- `data.stdin_normalized` says whether a BOM/CRLF rewrite happened;
- `data.range`-style cursors are not needed here — the script's own output is
  the answer.

### The PowerShell trap: BOM + CRLF

`Set-Content` and `Out-File` write **UTF-8 with a BOM** and **CRLF** line
endings on Windows. Piped to `bash -s` that becomes

```
﻿hostname：未找到命令
```

because the BOM is glued to the first word, and the trailing `\r` turns every
line into a syntax error. `--script-file` normalises by default; if you pass
the payload yourself, do it explicitly:

```powershell
# 1. let ssh-core normalise (recommended)
python scripts/ssh_execute.py lab-231 --script-file .\probe.sh --json

# 2. or write the file without a BOM yourself
[System.IO.File]::WriteAllText("$PWD\probe.sh", $body, [System.Text.UTF8Encoding]::new($false))
```

`--stdin-file` stays **byte-exact** on purpose (a caller may be piping a tar
stream through it). Opt into the rewrite with `--normalize-lf`, opt out of the
`--script-file` default with `--keep-crlf`.

### "I only want to look at a window of that 235 MB log"

Do not `scp` it and do not hand-write `sed -n`. `ssh_slice.py` reads a bounded
window, quotes the path itself, and returns a cursor:

```bash
python scripts/ssh_slice.py lab-231 /var/log/app.log --stat --json
python scripts/ssh_slice.py lab-231 /var/log/app.log --lines 299700-302600 --json
python scripts/ssh_slice.py lab-231 /var/log/app.log --grep 'ERROR|panic' --context 2 --json
python scripts/ssh_slice.py lab-231 /var/log/app.log --tail 200

# resume where the last answer stopped (data.range.next_offset / data.range.eof)
python scripts/ssh_slice.py lab-231 /var/log/app.log --lines 12000-24000 \
    --output-file ./window.txt --append
```

`data.range` carries `start` / `end` / `next_offset` / `eof` / `truncated` /
`offset_unit`. The sliced text itself is in the envelope under `--json`:
`data.content` (and `stdout`, matching `ssh_execute`), with
`data.content_bytes`, `data.content_lines`, `data.content_sha256` and
`data.content_truncated`. `--grep` keeps its line-number prefixes in `content`
exactly as on stdout, and `content` is byte-identical to a fresh
(non-`--append`) `--output-file`.

The cursor makes a loop able to reconstruct a file larger than any envelope:

```bash
start=1
while :; do
  python scripts/ssh_slice.py lab-231 /var/log/app.log \
      --lines "$start-$((start+9999))" --output-file ./app.log --append --json \
      > slice.json
  next=$(python -c "import json;print(json.load(open('slice.json'))['data']['range']['next_offset'])")
  eof=$(python -c "import json;print(json.load(open('slice.json'))['data']['range']['eof'])")
  [ "$eof" = "True" ] && break
  start=$next
done
```

### "The output is bigger than the envelope"

`ssh_execute --output-file PATH` streams stdout straight to disk as it arrives
(the 10 MB in-envelope cap is bypassed, memory stays flat) and returns
`data.output_file = {path, bytes, sha256, error?}` instead of the transcript:

```bash
python scripts/ssh_execute.py lab-231 "journalctl -u nginx --no-pager" \
    --output-file ./nginx.log --json
```

If the command times out you still get the bytes received so far, with
`data.output_file.partial: true`.

### "grep -c returned 1 and ssh-core called it a failure"

`grep -c` exits 1 when it finds nothing, `diff` exits 1 when the files differ,
`curl` exits 22/28 on an HTTP/network answer. That is the command *answering*,
not failing. Say so with `--allow-exit` (alias `--expect-exit`):

```bash
python scripts/ssh_execute.py lab-231 "grep -c ERROR /var/log/app.log" \
    --allow-exit 0,1 --json      # success:true, exit_code:1, data.exit_allowed:true
```

The real exit code is preserved in `exit_code`; only `success` flips. Network,
auth, host-key and timeout failures are never converted — those mean the
command never ran. In `--commands-file` batches the flag applies per command
and to the batch verdict, and `data.results[i].exit_allowed` marks the entries
that were expected to be non-zero.

## Run several probes over ONE connection

Two tools do this, and they answer different questions.

**`ssh_execute.py --commands-file`** — one host, N commands, one handshake. The
file is read locally, wrapped in a marked `sh -s` script, and shipped over a
single connection. Each command's output and its own exit status come back in
`data.results[]`:

```bash
python scripts/ssh_execute.py web-01 --commands-file /dev/stdin --json <<'EOF'
systemctl is-active nginx
journalctl -u nginx -n 50 --no-pager
EOF
```

Each line is one command; blank lines and lines starting with `#` are skipped.
`/dev/stdin` is a POSIX convenience — on Windows write the file first
(`New-TemporaryFile`) and pass its path. `--commands-file` also accepts
`--command "..."` repeated, which is the same thing without a temp file.

**`ssh_cluster.py`** — the same idea across *many* hosts, one subprocess of
`ssh_execute.py` per host, results merged into `data.results[alias]`:

```bash
python scripts/ssh_cluster.py "systemctl is-active nginx" --tags web,prod --max-workers 8 --json
```

Rules of thumb:

| Situation | Use |
|---|---|
| One host, 2-10 probes | `ssh_execute.py --commands-file` |
| One host, same probe many times a day | `ssh_daemon.py start` + `run` |
| Many hosts, one probe | `ssh_cluster.py --hosts/--tags/--environment` |
| Many hosts, several probes | one `ssh_cluster.py` call per probe — do not nest |
| Anything interactive (`[Y/N]`, paging) | `ssh_session.py`, never a batch |

A batch is **not** `set -e`: every command runs, and the envelope reports the
worst exit status plus `data.failed_indexes`. Read `data.results[]`, not just
`exit_code`.

## systemd / journald

### R1 · What the kernel and every unit logged this boot

**Proves:** whether the host rebooted recently, and what happened in the first
minutes after boot.

**Remote command:** `journalctl -b --no-pager -n 200`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "journalctl -b --no-pager -n 200" --json
```

### R2 · Is the service actually up, and since when

**Proves:** `active (running)` vs `failed` vs `activating (auto-restart)`, the
main PID, and the last few log lines systemd itself attached.

**Remote command:** `systemctl status nginx --no-pager -l`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "systemctl status nginx --no-pager -l" --json
```

`-l` matters: without it `systemctl` truncates long command lines and you lose
the exact path that failed.

### R3 · Every unit that is not happy, in one shot

**Proves:** the host-level failure list — no need to guess unit names.

**Remote command:** `systemctl --failed --no-pager`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "systemctl --failed --no-pager" --json
```

An empty list plus exit 0 is a real answer; a non-zero exit here usually means
systemd is not PID 1 (container) — check `data.stderr`.

### R4 · Restart storm: is it crash-looping?

**Proves:** how many times systemd restarted the unit, when it last entered
`active`, and what preceded each restart.

**Remote command:**
`systemctl show nginx -p NRestarts -p ActiveEnterTimestamp -p ExecMainStartTimestamp`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 --json --commands-file /dev/stdin <<'EOF'
systemctl show nginx -p NRestarts -p ActiveEnterTimestamp -p ExecMainStartTimestamp
journalctl -u nginx --since "-30 min" --no-pager | grep -E "Scheduled restart|Started|Stopped|Failed" | tail -30
EOF
```

`NRestarts` climbing between two runs is a crash loop. `--since "-30 min"` is
journalctl's relative form and is safer than computing timestamps in the
caller.

### R5 · Warnings and worse, for one unit, this boot

**Proves:** whether the unit is noisy without drowning in `info` lines.

**Remote command:** `journalctl -u nginx -b -p warning --no-pager -n 100`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "journalctl -u nginx -b -p warning --no-pager -n 100" --json
```

### R6 · Who is eating the CPU right now (cgroup view)

**Proves:** per-unit CPU/memory, which `top` cannot attribute to a service.

**Remote command:** `systemd-cgtop -b -n 1 --order=cpu`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "systemd-cgtop -b -n 1 --order=cpu" --json
```

`-b` is batch mode (no curses), `-n 1` is one sample — both required inside a
non-interactive connection. Add `--order=memory` for the memory view.

### R7 · What is scheduled to fire, and what already fired

**Proves:** whether a timer-driven job (backup, certbot, logrotate) is armed or
silently dead.

**Remote command:** `systemctl list-timers --all --no-pager`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "systemctl list-timers --all --no-pager" --json
```

## docker

### R8 · What is running, with image and published ports

**Proves:** container names, uptime, restart policy fallout and port
collisions, without the wrapping of the default table.

**Remote command:**
`docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}'" --json
```

Go templates contain `{{ }}`, so the whole remote command must stay one argv
string — which is exactly what `ssh_execute.py` takes.

### R9 · Container logs for the last N minutes

**Proves:** what the app said around an incident, bounded by time rather than
by line count.

**Remote command:** `docker logs --since 30m --tail 200 api`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "docker logs --since 30m --tail 200 api" --json
```

### R10 · Health status and restart count in one line

**Proves:** whether the orchestrator's healthcheck is passing and how many
times the container was restarted — the two fields `docker ps` makes you
squint for.

**Remote command:**
`docker inspect --format '{{.Name}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} started={{.State.StartedAt}}' api`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "docker inspect --format '{{.Name}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restarts={{.RestartCount}} started={{.State.StartedAt}}' api" --json
```

For every container at once, swap `api` for `$(docker ps -q)` — still one
remote shell, still one envelope.

### R11 · Run a read-only probe inside the container namespace

**Proves:** what the process sees — mounts, disk, listening sockets — as
opposed to what the host sees.

**Remote command:** `docker exec api sh -c 'df -h / && cat /proc/1/cgroup'`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "docker exec api sh -c 'df -h / && cat /proc/1/cgroup'" --json
```

The quoted `sh -c` runs **on the remote host** inside the container. Never
build this string by piping your local shell into `docker exec` — see the
"do not" section.

### R12 · How much disk docker itself is holding

**Proves:** the split between images, containers, volumes and build cache
before anyone runs a prune.

**Remote command:** `docker system df -v`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py web-01 "docker system df -v" --json
```

### R13 · DESTRUCTIVE · prune, after the dry run

**Proves:** nothing. It deletes.

Docker's `prune` subcommands have no portable `--dry-run`, so R12 (`docker
system df -v`) **is** the dry run: read it, quote the reclaimable number to
whoever owns the host, and only then run the prune. `ssh-core` has no general
confirmation gate — the one destructive flag pair it does gate (`--delete` on
the transfer scripts) is unrelated here — so this must never sit in a batch file
or a loop over `--tags`:

```bash
# DESTRUCTIVE — get explicit human approval before running this line.
python scripts/ssh_execute.py web-01 "docker system prune -af --volumes" --json
```

`-a` also removes images not referenced by a container; `--volumes` removes
named volumes. Both are unrecoverable.

## Kubernetes (read-only)

Every recipe here needs `kubectl` on the **remote** host and a kubeconfig the
login user can read. No `apply`, no `delete`, no `exec`, no `port-forward`.

### R14 · Pods across all namespaces, with node and IP

**Proves:** where each pod landed, whether it is `Running`/`CrashLoopBackOff`,
and whether it is being rescheduled.

**Remote command:** `kubectl get pods -A -o wide`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py k8s-01 "kubectl get pods -A -o wide" --json
```

### R15 · Why one pod is not starting

**Proves:** the event tail, probe failures, resource limits and the exact
reason for `Pending`/`ImagePullBackOff`.

**Remote command:** `kubectl -n prod describe pod api-7d9f8c6b5-4xj2p`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py k8s-01 "kubectl -n prod describe pod api-7d9f8c6b5-4xj2p" --json
```

### R16 · Logs from the previous container instance

**Proves:** the crash, not the restart — the only way to see why a
`CrashLoopBackOff` pod died.

**Remote command:** `kubectl -n prod logs api-7d9f8c6b5-4xj2p --previous --tail=200`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py k8s-01 "kubectl -n prod logs api-7d9f8c6b5-4xj2p --previous --tail=200" --json
```

`--previous` fails with "previous terminated container not found" when the pod
never restarted; that is a valid answer, not an infrastructure problem.

### R17 · Events, newest last, for the whole namespace

**Proves:** the cluster's own story — scheduling, evictions, OOMKills,
failed mounts — in chronological order.

**Remote command:**
`kubectl -n prod get events --sort-by=.lastTimestamp`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py k8s-01 "kubectl -n prod get events --sort-by=.lastTimestamp" --json
```

Add `| tail -40` remotely if the namespace is chatty; the 10 MB output cap will
otherwise silently truncate (the envelope then says `data.truncated: true`).

### R18 · CPU and memory per pod

**Proves:** which workload is actually consuming the node, independent of the
dashboard.

**Remote command:** `kubectl -n prod top pods --sort-by=memory`

**Via ssh-core:**

```bash
python scripts/ssh_execute.py k8s-01 "kubectl -n prod top pods --sort-by=memory" --json
```

`kubectl top` needs metrics-server; `error: Metrics API not available` means
the cluster has none, not that the command is wrong.

## Fleet versions of the same probes

The recipes above are per host. When the question is "which hosts are
different", let `ssh_cluster.py` fan out — one `ssh_execute.py` subprocess per
target, `data.summary` for the roll-up:

```bash
python scripts/ssh_cluster.py "systemctl --failed --no-pager" --tags web --environment production --health-check --max-workers 8 --json
```

`--health-check` probes each host with `true` first (or with
`--health-check-command CMD`) and reports the unreachable ones separately
instead of mixing them into the failure count. `--probe tcp:22` /
`--probe port:3306` adds a raw socket check that never needs SSH, so "sshd is
up" and "the service behind it is up" stop being the same question.
`--retry N` re-runs *only* the hosts whose failure class is `network` or
`timeout` — never `auth`, never `host_key`. `--parallel` is accepted for
compatibility and is a no-op; concurrency is `--max-workers` (default 8 —
higher values run into MaxStartups on a bastion).

## Do not

- **Do not pipe `docker exec` output through your local shell.** `ssh host
  "docker exec api sh -c 'ps aux'"` is fine; `docker exec ... | grep` executed
  locally after the fact is fine; `ssh host "docker logs ..." | sh` is not.
  Anything that looks like `eval`, `sh -s` fed from remote output, or a
  command substitution over remote text turns a compromised container into a
  local shell.
- **Do not use `--follow` / `-f` inside a batch.** `docker logs -f`,
  `journalctl -f` and `kubectl logs -f` never return: the batch hits
  `--timeout`, the envelope reports `failure_class: "timeout"` (exit 124) and
  every later command in the file is skipped. Poll with `--since`/`--tail`
  instead. If you genuinely need to watch, that is a single dedicated call:
  `ssh_execute.py <alias> --follow --tail 50 "journalctl -fu nginx"` — the live
  transcript goes to stderr and the final envelope to stdout, so `--json`
  still parses when you stop it.
- **Do not put a destructive command in `--commands-file`.** A batch keeps
  going after a non-zero exit, so a prune next to a `systemctl is-active` will
  run even when the probe already told you something is wrong.
- **Do not nest `ssh` inside a batch.** `ssh_execute.py` already owns the
  connection; a nested `ssh other-host` re-authenticates without our strict
  host-key settings and without redaction. Use `ssh_server_transfer.py` for
  host-to-host work and a second `ssh_execute.py` call for host-to-host
  queries.
- **Do not pass secrets as command arguments.** `docker login -p ...`,
  `kubectl --token ...`, `mysql -p...` all end up in the remote process table
  and in the audit JSONL. Use the tool's own env var or credential file.
- **Do not run `kubectl delete`, `docker system prune`, `systemctl restart`
  or `apt upgrade` from a recipe above.** They are out of scope by
  construction; the read-only commands exist so you can decide, then act
  deliberately.
- **Do not raise `--max-workers` to "make it faster".** 8 is the practical
  ceiling for one bastion; past it you get `MaxStartups` rejections that look
  like network failures.
- **Do not debug redaction by leaving `--no-redact` on.** It prints real host
  names, ports and `IdentityFile` paths into logs that outlive the session.
  Use it for one call, in one terminal.
