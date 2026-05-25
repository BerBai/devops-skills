---
name: ssh-core
description: SSH transport primitives — command execution, cluster broadcast, port-forward tunnels, SFTP upload/download, direct server-to-server transfer, and `~/.ssh/config` CRUD. Use whenever the task touches ssh/scp/rsync, a remote host alias, user@host, an IP (192.168.x.x, 10.0.x.x), or 跳板机/jumphost/bastion. Skip for localhost-only operations.
---

# ssh-core

**CRITICAL:** This skill handles ALL SSH/SCP/RSYNC operations. Do **not** call `ssh`, `scp`, or `rsync` directly from Bash — use the scripts in this skill. Direct calls leak credentials into shell history, miss the shared `~/.ssh/config` resolution layer, and break on Windows MSYS path translation.

## When to use

Trigger this skill when the user asks to:
- Run a command on a remote host (`ssh prod-web-01 'systemctl status nginx'`)
- Upload or download files (`scp ./deploy.tar prod:/srv/`)
- Open a tunnel to a remote service (`ssh -L 3306:localhost:3306 db-host`)
- Run the same command across a fleet (`for h in web-1 web-2 web-3; do ssh $h 'uptime'; done`)
- Move data between two remote machines without staging locally
- Manage `~/.ssh/config` entries

Skip when:
- The work is purely local (no host alias, no user@, no IP)
- The user wants the *safety-first* flow (request → review → execute) — use `ssh-guarded` instead

## Decision tree

```
Is the target a remote host?
├─ NO → wrong skill, stop
└─ YES → What's the action?
    ├─ Run a command
    │   ├─ Single host → ssh_execute.py <alias> "<cmd>"
    │   └─ Many hosts → ssh_cluster.py "<cmd>" --hosts a,b,c
    │                                         --tags web,prod
    │                                         --environment production
    │
    ├─ Move bytes
    │   ├─ Local → Remote → ssh_upload.py
    │   ├─ Remote → Local → ssh_download.py
    │   └─ Remote → Remote → ssh_server_transfer.py
    │                          --mode auto|direct|stream|hybrid
    │                          # v1.0 ships --mode stream only.
    │                          # auto / hybrid route to stream;
    │                          # direct refuses with mode_deferred (v1.1).
    │
    ├─ Forward a port → ssh_tunnel.py start <alias>
    │                     --remote-port 3306 --local-port 13306
    │
    └─ Manage hosts → ssh_config_manager.py
                        create | update | delete | list | find

    Bootstrap pubkey on a new host → deploy_pubkey.py <alias>
                                       [--accept-new-hostkey]
```

## Workflow (every operation)

1. **Discover.** If the user names a host you haven't seen, run `ssh_config_manager.py list` to confirm it exists in `~/.ssh/config`. If it doesn't, ask the user — do not invent connection details.
2. **Choose transport.** v1.0: key auth only via native `ssh` (BatchMode + ConnectTimeout; gets ControlMaster, ProxyJump, ForwardAgent for free). Password / keyboard-interactive auth is v1.1 (see ## Daemon model below). For first-touch bootstrap on a password-only host, use `deploy_pubkey.py <alias>` from an interactive terminal.
3. **Execute.** Pass the alias and the command. Read the JSON result. If `success` is false, read `stderr` before retrying.
4. **Report.** Show the user the command, the host, the exit code, and the last few lines of stdout/stderr. Never show keys, passwords, or `IdentityFile` paths.

See `references/workflows.md` for the long form.

## Quick reference

```bash
# Run a command (key auth only in v1.0; BatchMode + ConnectTimeout)
python scripts/ssh_execute.py prod-web-01 "systemctl status nginx" --json

# Upload with resume
python scripts/ssh_upload.py prod-web-01 ./release.tar.gz /srv/releases/ --resume

# Server-to-server transfer (e.g., snapshot prod → backup, no local staging)
# v1.0 ships stream mode only; --mode auto here routes to stream and logs
# data.auto_resolved_to="stream". --mode direct refuses with mode_deferred.
python scripts/ssh_server_transfer.py prod-db /var/backup/db.dump backup-host /restore/ --mode auto

# Broadcast to a tagged fleet
python scripts/ssh_cluster.py "uptime" --tags web,prod --parallel --max-workers 8

# Open a tunnel: local 13306 → prod-db:3306
python scripts/ssh_tunnel.py start prod-db --remote-port 3306 --local-port 13306

# Inspect / edit hosts
python scripts/ssh_config_manager.py list --environment production
python scripts/ssh_config_manager.py create --alias prod-web-02 --host 10.0.1.12 --user deploy --key ~/.ssh/id_ed25519 --environment production --tags web,nginx

# Bootstrap your pubkey on a fresh host (interactive password prompt)
python scripts/deploy_pubkey.py prod-web-02 --accept-new-hostkey
```

## Storage model

Single source of truth: **`~/.ssh/config`**. We use OpenSSH-standard `Host` blocks plus comment-line metadata so native `ssh` still works without us:

```sshconfig
# ===== prod-web-01 =====
# description: 生产环境 Web 服务器
# environment: production
# tags: web,nginx,production
# location: aliyun-beijing
Host prod-web-01
    HostName 10.0.1.11
    User deploy
    IdentityFile ~/.ssh/id_ed25519
    ProxyJump bastion
```

No proprietary database. No lock-in. `ssh prod-web-01` from any terminal still works.

## Daemon model

**v1.0: no daemon.** Each `ssh_execute` call is a fresh `ssh` subprocess (ADR-001 D1) with `BatchMode=yes` — key auth only. No paramiko, no custom socket protocol, no connection pool. Per-call cost is one SSH handshake (~200-300 ms).

Connection pooling and password / keyboard-interactive auth are planned for v1.1, likely via OpenSSH `ControlMaster` rather than a custom Python daemon (ADR-001 A2). The `ssh_daemon.py` stub in the scripts directory retains its v0.2 design intent in the source docstring for the v1.1 revival task.

## Safety baseline

- All scripts use `subprocess.run(argv_list, ...)` — never shell strings
- All file-transfer commands prefix `MSYS_NO_PATHCONV=1` on Windows
- Stdout/stderr are streamed with a 10 MB cap per stream — large outputs get truncated with a marker
- Credentials never appear in argv (passwords go through stdin to `sshpass`-equivalent or paramiko)
- See `references/safety.md` for the full list

## When something goes wrong

`references/troubleshooting.md` covers the common failures: `Permission denied (publickey)`, host-key drift, `ProxyJump` chain broken, `IdentityFile` permissions, Windows path mangling, `deploy_pubkey` ssh-copy-id quirks. Read it before guessing.
