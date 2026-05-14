# ssh-core workflows

Long-form versions of the flows referenced from `SKILL.md`. Read this when the short reference doesn't tell you enough.

## 1. Run a command on one host

```
caller (Claude Code)
   │
   ▼
ssh_execute.py <alias> "<cmd>"
   │
   ├─ key auth?  ──► native ssh (ControlMaster, ProxyJump, ForwardAgent)
   └─ password ?  ──► daemon present?
                       ├─ YES → send length-prefixed JSON
                       └─ NO  → spawn daemon, retry
```

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

| Mode | Path | When |
|---|---|---|
| `direct` | source machine runs `scp`/`rsync` → target machine | source can reach target directly |
| `stream` | local process bridges two open SFTP sessions | source can't egress, but local can reach both |
| `hybrid` | try `direct`; fall back to `stream` on failure | unknown egress rules |
| `auto` | pick `direct` for files > 100 MB and reachable; `stream` for small + uncertain | default |

`--use-rsync` swaps `scp` for `rsync` in the `direct` path (gets you `--partial`, `--checksum`, `--delete`).

## 4. Port-forward tunnel

```
ssh_tunnel.py start prod-db --remote-port 3306 --local-port 13306
```

Same daemon shape as `ssh_execute`: a local supervisor process holds the forward open, writes state to `$TMPDIR/ssh_tunnel/<md5(alias-port)>.json`, exits after `--idle-timeout` (default 30 min).

Local ports come from pool `10000–20000` when `--local-port` is omitted. The state file records the chosen port so Claude can answer "where's the tunnel?".

```
ssh_tunnel.py list                  # all active forwards
ssh_tunnel.py status prod-db        # this alias's forwards
ssh_tunnel.py stop prod-db --port 13306
```

## 5. Manage `~/.ssh/config`

CRUD through `ssh_config_manager.py`. Every write:
1. Reads `~/.ssh/config` into an AST that preserves comments
2. Mutates the AST
3. Writes a `~/.ssh/config.bak.<unix-ts>` backup
4. Renders the file back with stable ordering (existing hosts keep their position; new ones append)

Reads (`list`, `find`) are O(file size) and never write.

## 6. Choosing key vs password auth

The wrapper inspects what's in `~/.ssh/config` for the alias:

| Config | Path |
|---|---|
| `IdentityFile` set, key exists, no passphrase | native ssh (fastest) |
| `IdentityFile` set, key has a passphrase | native ssh + `ssh-agent` (if loaded), else fallback |
| `IdentityFile` missing, password recorded in our managed comment block | paramiko + daemon |
| Neither | refuse, ask user |

Passwords in comments are opt-in and discouraged. The preferred flow is `deploy_pubkey` (planned for v0.2) which runs `ssh-copy-id` and then strips the password.

## 7. Failure modes worth knowing

- **Daemon stuck.** State file present, port unresponsive. Recovery: `ssh_daemon.py stop <alias>` removes the state file and kills any process holding the recorded PID.
- **Host key changed.** `StrictHostKeyChecking` fails. Don't auto-accept. Ask the user; if confirmed, the user (not us) edits `~/.ssh/known_hosts`.
- **MSYS path mangling.** Symptom: `/srv/x` becomes `C:/Program Files/Git/srv/x`. We set `MSYS_NO_PATHCONV=1` on every spawn. If you ever see a Windows drive prefix in a remote path argument, the wrapper has a bug.
- **`ProxyJump` chain breaks.** Usually a key auth issue on the bastion. The wrapper retries once, then surfaces the chain it tried so the user can debug.
