# ssh-core workflows

Long-form versions of the flows referenced from `SKILL.md`. Read this when the short reference doesn't tell you enough.

## 1. Run a command on one host

```
caller (Claude Code / agent)
   │
   ▼
ssh_execute.py <alias> "<cmd>"
   │
   ├─ key route:   native ssh -o BatchMode=yes -o ConnectTimeout=N [-- <host> <cmd>]
   └─ password route:  pty (POSIX)  |  ssh -tt + SSH_ASKPASS (Windows)
```

`ssh_execute.py --control-master auto` opts into OpenSSH ControlMaster reuse
(POSIX only — Windows OpenSSH needs a POSIX socket; there it refuses with
`dependency_missing` and points at `--commands-file`).

**Batching beats reuse.** `--commands-file PATH` (or repeated `--command`)
runs N commands inside **one** connection: `ssh <alias> sh -s` reads a script
from stdin, and every command's output plus its own exit status lands in
`data.results[]`. 20 commands cost one handshake on every platform, including
Windows. Use it instead of ControlMaster whenever you have more than one
command for a host.

Each command runs as `( cmd ) < /dev/null`, for two reasons: a command that
reads stdin (`cat`, `read`, `ssh`, `sudo -S`) gets EOF instead of swallowing
the rest of the script, and a stray `exit` ends only that subshell. If the
batch stops early anyway, the envelope says `data.batch_incomplete: true` with
`data.missing_indexes` and `success: false` — a truncated batch is never a
green light.

**Ad-hoc hosts.** `user@host`, `user@host:2222` and `[v6addr]:22` are accepted
directly (translated to `-p`), and `--port` / `--identity` override the
config. You no longer have to write a `Host` block before touching a machine.

**A command that starts with `-`.** Both `ssh_execute.py <alias> -- -v` and
`ssh_execute.py <alias> -v` work; the second form is recognised because `-v`
is not a known option of the script.

**Output contract** (all scripts share this):

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "data": { "route": "remote", "failure_class": null, "elapsed_s": 0.21 }
}
```

Argument errors produce the same envelope with
`data.failure_class = "invalid_arguments"` and exit 2, so a caller can run one
`json.loads` path for everything.

When `success` is `false`, *always* surface `stderr` to the user. Do not
silently retry — unless `data.failure_class` is `network` or `timeout`, which is
exactly what `--retry` handles (see §1c).

## 1a. Escalating to root (`--sudo`)

```
ssh_execute.py <alias> --sudo -- "systemctl restart nginx" --json
ssh_execute.py <alias> --sudo-user deploy -- "id -u" --json
SSH_SUDO_PASSWORD=... ssh_execute.py <alias> --sudo \
    --sudo-password-env SSH_SUDO_PASSWORD -- "id -u" --json
```

The wrapper is `sudo -n [-u USER] -- sh -c '<cmd>'`. `-n` is the whole point:
there is no terminal on an exec channel, so an interactive sudo prompt would
hang until the timeout, and a hang is much harder to diagnose than a failure.

Before your command runs, ssh-core runs `sudo -n true` **through the same
route** (same port, identity, auth method, `-o` options). If that fails, nothing
is executed, the envelope says `data.command_not_run: true`, and the
remediation text names both fixes (NOPASSWD sudoers rule, or
`--sudo-password-env`). The failure class is the *real* one: `sudo_required`
(112) when sudo itself refused, and the original `auth` / `host_key` /
`network` class (with `data.sudo_preflight: "unreachable"`) when the preflight
could not even reach the host — a dead box is not a sudoers problem.

`--sudo-password-env VAR` sends the password over **stdin** (`sudo -S`), never
argv and never the envelope; a wrong password is `sudo_auth_failed` (exit 113),
which is deliberately a different class from "you have no rule at all". It
cannot be combined with `--stdin-text` / `--commands-file`, because stdin is
then carrying the secret.

`--sudo` is refused on the `local` route (you already have your own
privileges) and it is never retried — an escalation misconfiguration is not
transient.

## 1b. Following a log (`--follow` / `--tail`)

```
ssh_execute.py web-01 --follow "journalctl -fu nginx --no-pager"
ssh_execute.py web-01 --follow --tail 50 "tail -f /var/log/nginx/error.log" --json
```

`--tail N` wraps the command (`stdbuf -oL -eL sh -c '<cmd>' | tail -n N -f`),
so for an already-following command prefer `--follow` alone or
`--tail 50 "journalctl -fu nginx"`; pairing it with `tail -f` gives you two
tails and the inner one buffers.

Output is relayed line by line while the command runs. The contract:

| Situation | exit_code | data |
|---|---|---|
| remote command exits | its own status | `follow: true`, `lines`, `truncated` |
| Ctrl-C | 130 | `interrupted: true`, transcript so far |
| `--timeout` reached | 124 | `failure_class: "timeout"`, partial transcript |

With `--json`, the **live transcript goes to stderr** and stdout carries exactly
one JSON document at the end. That is what lets `... --follow --json | jq` keep
working after Ctrl-C. The transcript is still bounded by the usual 10 MB cap;
`data.truncated` / `data.bytes_elided` report anything dropped.

`--tail N` wraps the command as `stdbuf -oL -eL sh -c '<cmd>' | tail -n N -f`
(with a plain-pipe fallback when `stdbuf` is missing), which is what makes a
slow log appear immediately. Do not combine it with a command that is already
`tail -f`: the inner `tail` would never flush through a pipe.

## 1c. Retrying (`--retry`)

```
ssh_execute.py web-01 "uptime" --retry 3 --retry-delay 2 --retry-backoff 2
ssh_cluster.py "uptime" --hosts web-1,web-2 --retry 3
ssh_upload.py web-01 ./release.tgz /srv/ --retry 3
```

`--retry N` counts **total attempts** (`3` = one call plus up to two retries).
Only `network` and `timeout` are repeated. `auth`, `host_key`, `kex`,
`permission` and `invalid_arguments` are terminal on the first attempt: a
rejected credential retried five times is how an account gets locked, and a
changed host key retried is how people learn to ignore the warning.

`data.attempts` is the number of tries that actually happened,
`data.retry_history` has one record per abandoned attempt (`failure_class`,
`exit_code`, `waited_s`) and `data.retry_policy` echoes the policy. The delay
grows `delay × backoff^(n-1)` and is capped at 30 s per wait and 300 s in
total, so `--retry 100` cannot turn a 2-minute command into an hour.

In a cluster, each **host** retries independently: one flaky node does not force
a second sweep, and `data.results[alias].attempts` says which node needed it.

## 2. Broadcast to a fleet

```
ssh_cluster.py "<cmd>" --tags web,prod --max-workers 8
ssh_cluster.py "<cmd>" --hosts web-1,web-2 --environment production
```

Targets resolve as the **intersection** of `--hosts`, `--tags`, and
`--environment`. `--tags` is AND across the list (`web,prod` = has both) and
matches the `# tags:` comment metadata written by `ssh_config_manager.py`;
`--environment` is an exact match. With no filter at all the call fails
(`target_resolution_failed`, exit 4) — pass `--tags`/`--environment` or
`--hosts` explicitly. `--parallel` is accepted for compatibility and is a
**no-op** — concurrency is `--max-workers`.

Default `max-workers` is 8. Above that, SSH MaxStartups on the bastion bites.

`--ssh-config PATH` has one meaning here, the same as in `ssh_execute.py`: the
file is used to resolve targets **and** it is forwarded to every worker as
`ssh -F`. Before that it only affected selection, so with two configs in play
(test vs production) the cluster could select an alias from one and connect
through the other — different machine, no warning.

**stdin is never inherited.** Every worker gets `stdin=DEVNULL` unless you pass
`--stdin-file PATH`, in which case each worker opens its own handle and reads
the same bytes. Sharing one inherited descriptor was a race: the first host to
read got the payload, the rest saw EOF, and all of them reported success.

```
python scripts/ssh_cluster.py "bash -s" --hosts web-1,web-2 --stdin-file ./setup.sh
```

### 2a. Health checks that can actually fail

`--health-check` used to run `true`: it proved "SSH accepts me" and nothing
else, so a host with a dead nginx looked healthy. Now:

```
ssh_cluster.py "<cmd>" --hosts web-1,web-2 \
    --health-check \
    --health-check-command "systemctl is-active nginx" \
    --probe tcp:22 --probe port:3306 --json
```

- `--health-check-command CMD` replaces `true`. A non-zero exit marks the host
  unhealthy, sets `failure_class: "health_check_failed"`, `skipped: true`, and
  the broadcast never touches it.
- `--probe [tcp:|port:]PORT` is a plain socket connect that never touches sshd.
  That is how you tell the two outage shapes apart: *port closed* (host down /
  service not listening) versus *port open but ssh refuses me* (auth, host key,
  wedged sshd). Records land in `data.results[alias].probe` as
  `{ok, probes, failed, failed_targets}` plus the raw `probe_records`.
- Both run in parallel before the broadcast; a host failing either is skipped.

### 2b. Retry: only what is transient

```
ssh_cluster.py "<cmd>" --hosts web-1,web-2 --retry 3 --retry-delay 2 --retry-backoff 2
```

Each host retries **independently**, so one flaky node does not force a whole
second sweep. Only `failure_class` `network` and `timeout` are repeated;
`auth`, `host_key` and `kex` never are — repeating a rejected credential is how
accounts get locked. `data.results[alias].attempts` records the real count, and
`retry_history` lists each abandoned attempt with the wait that followed it.

## 3. File transfer (upload / download / server-to-server)

### 3a. Upload and download

```
ssh_upload.py   <alias> <local-path>  <remote-path>  [--recursive]
ssh_download.py <alias> <remote-path> <local-path>   [--recursive]
```

`--transport auto` (the default) uses paramiko/SFTP when it is importable and
falls back otherwise:

| Situation | Upload | Download |
|---|---|---|
| paramiko installed | SFTP (`--resume`, `--bwlimit`, `--preserve`, `--delete`, progress; `--chmod` is upload-only) | SFTP (`--resume`, `--bwlimit`, `--preserve`, `--delete`, progress) |
| no paramiko, single file | `scp -O` (+ `-p` for `--preserve`) + remote sha256 check | `scp -O` (+ `-p`) + local sha256 check |
| no paramiko, `--recursive` | `scp -r` | remote `tar -cf -` piped into local `tar -xf -` |

`data.transport` is one of `sftp | scp | tar | rsync | direct`, and
`data.transport_note` says what the fallback cannot do. The fallback exists
because "paramiko is not installed" used to mean "this skill cannot move a
single byte", while `scp` ships with every OpenSSH client.

`--transport openssh` names that fallback explicitly (it is the `scp` backend
under a clearer name); `--transport scp` still works. An SFTP-only flag on that
backend is **refused** with `failure_class: "mode_deferred"` (exit 166) and
`data.deferred_flags`, not silently ignored: `--resume`, `--bwlimit` and
`--block-size` (download) plus `--chmod` (upload). Two commands, one answer —
the alternative was a `success: true` that resumed nothing.

### 3a-bis. Metadata and mirroring

```
ssh_upload.py   <alias> <local-dir>  <remote-dir>  --recursive --preserve
ssh_upload.py   <alias> <local-dir>  <remote-dir>  --recursive --delete --yes
ssh_download.py <alias> <remote-dir> <local-dir>   --recursive --preserve
```

- `--preserve` applies the source's **mtime and permission bits** to the copy
  (`cp -p` semantics). With SFTP a server that refuses SETSTAT still delivers
  every byte and reports `data.preserved: false` plus `data.preserve_note`
  rather than failing. With `scp` it maps to `scp -p` and reports
  `data.preserved: true`.
- `--delete` turns the copy into a **mirror**: after a clean transfer, entries
  that exist only on the destination are removed and listed in
  `data.deleted_files`. Three guards, all deliberate:
  1. `--yes` is required — ssh-core never prompts (no tty guarantee), so
     refusing loudly is the only safe default;
  2. `--recursive` is required — a single-file mirror would mean deleting the
     whole destination directory;
  3. nothing is pruned when the copy itself recorded a failure, because pruning
     next to a half-copied tree is how a mirror destroys data.

`--delete` needs the SFTP transport. Without paramiko the script **refuses**
with `failure_class: "mode_deferred"` (exit 166) and a `pip install paramiko`
hint instead of pretending: scp and tar cannot prune, and a `success: true`
without pruning would be a lie. `--preserve` is honoured on both transports.

### 3b. Server-to-server

| Mode | Path | Needs paramiko | Notes |
|---|---|---|---|
| `stream` | bytes bridge through two SFTP sessions | yes | per-file progress, bwlimit |
| `direct` | `ssh src 'tar -cf -' \| ssh dst 'tar -xf -'` | no | no local staging |
| `direct --use-rsync` | `ssh src "rsync -a -e 'ssh …' src_path dst:path"` | no | incremental re-sync; rsync must exist on the **source** host |
| `hybrid` | `direct`, falling back to `stream` | only for the fallback | reported as `hybrid_resolved_to` |
| `auto` | `stream` if paramiko is importable, else `direct` | — | reported as `auto_resolved_to` |

All four modes are implemented; nothing is deferred any more.

## 4. Port-forward tunnels

```
ssh_tunnel.py start prod-db --remote-port 3306 --local-port 13306
ssh_tunnel.py start bastion  --type remote --remote-port 2222 --local-port 22 \
                             --remote-host 10.0.0.9
ssh_tunnel.py start bastion  --type dynamic --local-port 1080
ssh_tunnel.py list | status <alias> | stop <alias> [--port N] [--type T]
```

* `--type local` (default) is `-L`: a local port reaches a service on the far
  side.
* `--type remote` is `-R`: the remote side listens on `--remote-port` and
  forwards back to `--remote-host:--local-port`, resolved **on this machine**
  (the bastion-reverse case). `--reverse` is an alias. Pass
  `--remote-host 127.0.0.1` when the local service binds IPv4 only —
  `localhost` may resolve to `::1` and the forward then fails with
  `Connection refused` while the listener itself looks healthy.
* `--type dynamic` is `-D`: a SOCKS5 proxy on the local port. `--dynamic` is
  an alias.

A detached `subprocess.Popen` (POSIX `setsid`, Windows detached-process flags)
runs `ssh -N`, and `Popen.pid` **is** the ssh PID — no `lsof`/`pgrep` needed.
State lives at `$TMPDIR/ssh_tunnel/<md5(alias-type-port)>.json` (0600 in a
0700 directory) and records the type, both ports, the bind address, the
watchdog PID and the last activity timestamp.

Local ports come from the pool `10000–20000` when `--local-port` is omitted.

**`--idle-timeout` is real.** `start` launches a detached watchdog that polls
the OS socket table (`netstat` on Windows, `ss`/`netstat` elsewhere) for
connections to the forwarded port and stops the tunnel after N idle seconds,
removing the state file and writing an `idle_stop` audit record. `0` disables
the watchdog (tunnel runs until `stop`). The default is 1800 s.

`start` returns as soon as the local port accepts a connection (typically
~300 ms), not after `connect-timeout + 1s`.

## 5. Manage `~/.ssh/config`

CRUD + restore through `ssh_config_manager.py`:

```
ssh_config_manager.py list [--include-wildcards] [--environment prod] [--tags web]
ssh_config_manager.py find <alias> [--show-key]
ssh_config_manager.py create --alias A --host H --user U [--key …] [--port …]
ssh_config_manager.py update --alias A [--user …] [--dry-run] [--diff]
ssh_config_manager.py delete --alias A [--allow-wildcard]
ssh_config_manager.py restore [--alias A] [--from <bak>|latest] [--list-backups]
ssh_config_manager.py validate
```

Every write:

1. takes a cross-process lock on `<cfg>.lock`;
2. re-reads `~/.ssh/config` **under that lock** (so a concurrent writer's host
   is not lost);
3. mutates an AST that preserves comments, order and `Match` blocks;
4. refuses to render any directive without a value;
5. validates the result with `ssh -F <tmp> -G <alias>`;
6. copies to `<cfg>.bak.<epoch-ms>-<pid>-<n>` and `os.replace`s the new file.

`update` treats an empty value as **delete the field**: `--user ''`, `--user=`,
or a bare `--user` all remove the line. (`User ` with no argument makes
OpenSSH reject the whole file, which is why this matters.)

Alias matching is case-insensitive and glob-aware, exactly like OpenSSH:
`find WIN-PROD-01` finds `win-prod-01`, and `create` reports the on-disk
spelling instead of allowing a second block. Wildcard blocks (`Host *`,
`Host web-*`) appear in `list --include-wildcards` marked `wildcard: true`;
editing one requires `--allow-wildcard`.

`create` creates a missing config file when its parent directory exists, so a
fresh machine works without a manual `New-Item ~/.ssh/config`.

Reads (`list`, `find`, `validate`) never write.

### 5a. Inventory export (`list --format`)

```
ssh_config_manager.py list --format ansible  > inventory.json
ssh_config_manager.py list --format yaml
ssh_config_manager.py list --format ssh-config > clean_config
ssh_config_manager.py list --format json
```

| Format | Output | Use |
|---|---|---|
| `table` | the aligned human table (default) | look at it |
| `json` | the record list, pretty-printed (`data.hosts` carries it too) | your own tooling |
| `ansible` | `{"all": {"hosts": {...}}, "_meta": {"hostvars": {}}}` | `ansible-inventory --list -i inventory.json` |
| `yaml` | the same inventory as YAML, rendered without PyYAML | config-management pipelines |
| `ssh-config` | clean `Host` blocks only — no metadata comments, no `Match`/`Include`, no wildcards | feeding another `ssh_config` consumer |

Ansible has native keys for `HostName` / `User` / `Port` / `IdentityFile`; the
ssh-core metadata has none, so it rides along namespaced:
`ssh_core_environment`, `ssh_core_tags`, `ssh_core_description`,
`ssh_core_location`, `ssh_core_auth`, `ssh_core_device_type`. Dropping it would
produce an inventory that cannot drive a tag-based playbook.

`ssh-config` is the *effective* view: it is re-emitted from the parsed `Host`
blocks (so the ssh-core comments are gone) and wildcard patterns are excluded,
because a wildcard block is a rule, not a host.

## 6. Choosing key vs password auth

The wrapper inspects what's in `~/.ssh/config` for the alias:

| Config | Path | Status |
|---|---|---|
| `IdentityFile` set, key exists, no passphrase | native ssh (fastest) | ✓ |
| `IdentityFile` set, key has a passphrase + `ssh-agent` loaded | native ssh + agent | ✓ |
| `# auth: password` metadata (or `--auth password`) | pty (POSIX) / `ssh -tt` + askpass (Windows) | ✓ |
| `# device-type: network` metadata | interactive session | ✓ |
| Neither | refuse, ask user | ✓ |

The password itself is never stored by us: export `SSH_PASSWORD` (or the
variable named by `--password-env`) for the call. Passwords recorded in
`~/.ssh/config` comments are **not** read — the `# auth: password` key only
records *which* method to use.

On Windows the password route is a `ssh -tt` pipe whose prompt is answered by
the `SSH_ASKPASS` helper in `lib/askpass.py`, so `--auth password`,
`ssh_session.py` and `deploy_pubkey.py --password` all work there. No WSL, no
pty, no `sshpass`.

The preferred long-term flow is `deploy_pubkey.py <alias>`, which installs the
key over a plain exec channel (no `ssh-copy-id`), proves key-only auth works,
and with `--promote-auth` flips the host's metadata to `# auth: key`.

## 6b. Interactive network-device sessions

`ssh_session.py` opens one `RequestTTY=force` session and drives it:

```
ssh_session.py <alias> \
    --disable-paging "screen-length 0 temporary" \
    --command "system-view" \
    --command "vlan batch 50" \
    --command "save" --confirm yes
```

- Each command waits for the device prompt (`--prompt-regex` overrides the
  heuristic) and is recorded in the audit JSONL.
- `--confirm ask` (default) refuses to answer `[Y/N]` prompts — it fails
  with `failure_class: "confirmation_required"` so nobody types `y` into a
  device by accident.
- `--snapshot-cmd "display current-configuration"` stores before/after
  captures and returns their paths.
- Transport: `pty.fork()` on POSIX; `ssh -tt` + `SSH_ASKPASS` on Windows
  (`--force-pty` / `--no-pty` override the choice). Both drive the *same*
  state machine in `lib/session.py`, so VRP/Comware/IOS behaviour is identical.

## 7. Maintaining a long-lived session (daemon)

```
ssh_daemon.py start <alias> [--idle-timeout 1800]
ssh_daemon.py run   <alias> "<command>" [--timeout 30]
ssh_daemon.py status <alias> | list | stop <alias>
```

One `ssh -T <alias> sh` child stays open; each `run` sends a command into it
and reads the marked result back over a loopback TCP socket protected by a
0600 bearer token. Measured on this Windows box: 5 calls cost ~0.7 s through
the daemon versus ~1.5 s with a fresh handshake each time. The daemon exits
after `--idle-timeout` seconds without a request and removes its state file.

## 8. Watching the trail (audit + reports)

```
audit.py summarize --since 24h
audit.py summarize --since 7d --host web-01 --json
audit.py list
audit.py prune --older-than 30d --dry-run
audit.py rotate --max-bytes 10MB
```

`summarize` is the "which host is the problem" report: one row per alias with
calls, failures, failure rate, failure classes, average duration, slowest
command, last error and last-seen timestamp — worst first, with a `worst:`
line at the bottom. Damaged lines (a half-written record after a kill) are
counted in `skipped_lines` instead of aborting the report, because the moment
you need the trail is the moment something went wrong.

`prune` enforces retention (`--older-than 30d`, `--dry-run` first), `rotate`
forces the size cap for hosts that have not written since the policy changed,
and `list` shows files, sizes and record counts plus the current policy.

## 9. Failure modes worth knowing

- **Daemon stuck / stale.** `ssh_daemon.py status <alias>` distinguishes
  `running` from a stale state file; `stop` clears both. For
  `ssh_execute.py --control-master auto`, OpenSSH owns the master socket and
  expires it through `ControlPersist`; rerun without the flag to bypass reuse
  while debugging.
- **Host key changed.** `StrictHostKeyChecking` fails. Don't auto-accept. Ask
  the user; if confirmed, the user (not us) edits `~/.ssh/known_hosts`.
- **MSYS path mangling.** Symptom: `/srv/x` becomes `C:/Program Files/Git/srv/x`.
  We set `MSYS_NO_PATHCONV=1` on every spawn. If you ever see a Windows drive
  prefix in a remote path argument, the wrapper has a bug.
- **`ProxyJump` chain breaks.** Usually a key auth issue on the bastion.
  There is **no automatic retry** for `auth`-class failures — the ssh client's
  stderr is surfaced as-is (plus `data.failure_class`), so the first hop can be
  tested directly with `ssh_execute.py <bastion> "echo ok"`. If the class is
  `network`/`timeout`, `--retry` will repeat it; nothing else is repeated.
- **`dependency_missing` on a transfer.** No paramiko and no `scp` — check
  `data.transport`; `--transport scp` or `direct` mode may still work.
- **`sudo_required` (112).** No NOPASSWD rule and no `--sudo-password-env`.
  Nothing was executed (`data.command_not_run: true`). See
  `references/troubleshooting.md` for the two fixes.
- **Health check says a host is fine but the service is not.** That is what
  `--health-check-command` is for: `true` only proves SSH works. Add
  `--probe tcp:<port>` to catch the "port closed" half without needing sshd.
