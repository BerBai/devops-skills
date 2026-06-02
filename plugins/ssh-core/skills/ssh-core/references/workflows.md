# ssh-core workflows

Long-form versions of the flows referenced from `SKILL.md`. Read this when the short reference doesn't tell you enough.

## 1. Run a command on one host

```
caller (Claude Code)
   │
   ▼
ssh_execute.py <alias> "<cmd>"
   │
   └─ native ssh -o BatchMode=yes -o ConnectTimeout=N
      (ProxyJump / ForwardAgent are inherited from ~/.ssh/config;
       add --control-master auto to opt into OpenSSH ControlMaster reuse)
```

`ssh_execute.py --control-master auto` is the V1.1 first slice for connection reuse. It appends OpenSSH `ControlMaster=auto`, `ControlPersist=<seconds>`, and a hashed `ControlPath` under a private temp directory. The default remains off, so V1 argv stays unchanged unless the caller asks for reuse.

Password / keyboard-interactive auth and a custom Python connection-reuse daemon remain deferred (ADR-001 D1 + A2). For first-touch bootstrap on a password-only host, use `deploy_pubkey.py <alias>` from an interactive terminal.

**Output contract** (all scripts share this):
```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "data": { "alias": "prod-web-01", "duration_ms": 123 }
}
```

When `success` is `false`, *always* surface `stderr` to the user. Do not silently retry.

## 2. Broadcast to a fleet

```
ssh_cluster.py "<cmd>" --tags web,prod --parallel --max-workers 8
```

Targets resolve as the **intersection** of `--hosts`, `--tags`, and `--environment`. Empty filter = the entire `~/.ssh/config`. A `--health-check` flag asks each host `true` first; failing hosts are reported but the rest of the broadcast proceeds.

Default `max-workers` is 8. Above that, SSH MaxStartups on the bastion bites.

## 3. Server-to-server transfer

Four modes:

| Mode | Path | When | v1.0 status |
|---|---|---|---|
| `direct` | source machine runs `scp`/`rsync` → target machine | source can reach target directly | refused with `failure_class=mode_deferred` (v1.1) |
| `stream` | local process bridges two open SFTP sessions | source can't egress, but local can reach both | **shipped** |
| `hybrid` | try `direct`; fall back to `stream` on failure | unknown egress rules | routes to stream (fallback semantics deferred to v1.1) |
| `auto` | pick `direct` for files > 100 MB and reachable; `stream` for small + uncertain | default | routes to stream (auto decision logic deferred to v1.1) |

`--use-rsync` swaps `scp` for `rsync` in the `direct` path (gets you `--partial`, `--checksum`, `--delete`). In v1.0 it is refused with `failure_class=flag_deferred` because it requires `--mode direct`.

**v1.0 status**: only `stream` is fully implemented. `auto` / `hybrid` route to stream and log `data.{auto,hybrid}_resolved_to="stream"`; `direct` and `--use-rsync` refuse with structured envelopes (exit codes 20 and 21). The CLI surface is preserved so v1.1 can light up `direct` additively without breaking callers. See `.trellis/tasks/05-24-ssh-core-ssh-server-transfer/prd.md` for the full deferral rationale.

## 4. Port-forward tunnel

```
ssh_tunnel.py start prod-db --remote-port 3306 --local-port 13306
```

v1.0 ships a single detached forward per invocation: a Python `subprocess.Popen(start_new_session=True)` detach (POSIX-only) launches `ssh -N -L ...` which inherits a fresh session and is reaped by init after the parent exits. The forward's `(alias, local_port)` state lives at `$TMPDIR/ssh_tunnel/<md5(alias-port)>.json` (mode `0o600`, directory `0o700`). `Popen.pid` is the detached ssh PID directly — no `lsof` / `pgrep` lookup is needed to recover it.

Local ports come from pool `10000–20000` when `--local-port` is omitted. The state file records the chosen port so Claude can answer "where's the tunnel?".

```
ssh_tunnel.py list                  # all active forwards
ssh_tunnel.py status prod-db        # this alias's forwards
ssh_tunnel.py stop prod-db --port 13306
```

**v1.0 status**: `--idle-timeout` is accepted (surface-stable) but is a no-op; the tunnel runs until you call `stop` or kill its PID. The script emits `data.flag_deferred=["--idle-timeout"]` + a stderr warning so callers can notice. v1.1 will reintroduce idle exit later, likely by reusing the ControlMaster direction now started in `ssh_execute.py`. Windows support is also v1.1 (the `start_new_session=True` detach is POSIX-only; Windows starts refuse with `failure_class=posix_only`, exit 30). Until then, callers should clean up explicitly. See `.trellis/tasks/05-24-ssh-core-ssh-tunnel/prd.md` for the full deferral rationale.

## 5. Manage `~/.ssh/config`

CRUD through `ssh_config_manager.py`. Every write:
1. Reads `~/.ssh/config` into an AST that preserves comments
2. Mutates the AST
3. Writes a `~/.ssh/config.bak.<unix-ts>` backup
4. Renders the file back with stable ordering (existing hosts keep their position; new ones append)

Reads (`list`, `find`) are O(file size) and never write.

## 6. Choosing key vs password auth

The wrapper inspects what's in `~/.ssh/config` for the alias:

| Config | Path | v1.0 |
|---|---|---|
| `IdentityFile` set, key exists, no passphrase | native ssh (fastest) | ✓ |
| `IdentityFile` set, key has a passphrase + `ssh-agent` loaded | native ssh + agent | ✓ |
| `IdentityFile` missing, password recorded in `# password:` comment | paramiko + daemon | **v1.1 deferred** (ADR-001 A2) |
| Neither | refuse, ask user | ✓ |

Passwords in comments are opt-in and discouraged. The preferred flow is `deploy_pubkey.py <alias>` (v1.0 ships), which delegates to system `ssh-copy-id` and inherits your controlling tty for the one-time password prompt. The `--strip-password` companion behavior (auto-remove the comment after bootstrap) is **v1.1 deferred** — it requires `ssh_config_manager` to first learn the `# password:` metadata key, which is itself deferred until the paramiko + daemon path returns. For now, edit `~/.ssh/config` by hand to remove the comment after a successful `deploy_pubkey` run.

## 7. Failure modes worth knowing

- **Daemon stuck.** There is still no custom Python daemon. For `ssh_execute.py --control-master auto`, OpenSSH owns the master socket and expires it through `ControlPersist`; rerun without `--control-master auto` to bypass reuse while debugging.
- **Host key changed.** `StrictHostKeyChecking` fails. Don't auto-accept. Ask the user; if confirmed, the user (not us) edits `~/.ssh/known_hosts`.
- **MSYS path mangling.** Symptom: `/srv/x` becomes `C:/Program Files/Git/srv/x`. We set `MSYS_NO_PATHCONV=1` on every spawn. If you ever see a Windows drive prefix in a remote path argument, the wrapper has a bug.
- **`ProxyJump` chain breaks.** Usually a key auth issue on the bastion. The wrapper retries once, then surfaces the chain it tried so the user can debug.
