# ssh-core troubleshooting

Symptom → likely cause → fix. Ordered by frequency.

## "Permission denied (publickey)"

| Likely cause | Check | Fix |
|---|---|---|
| Wrong `IdentityFile` for this host | `ssh_config_manager.py find <alias>` | Update `IdentityFile` |
| Key file mode too open | `ls -l ~/.ssh/id_*` should be `600` | `chmod 600` |
| Key not loaded in `ssh-agent` (passphrase keys) | `ssh-add -l` | `ssh-add ~/.ssh/<key>` |
| Public key not installed on remote | login interactively once | `deploy_pubkey.py <alias>` (planned v0.2) |
| User mismatch | `User` field in config | Update `User` |

## "Host key verification failed"

The remote host's key changed since last connect. Causes:
- Server reinstall
- Container/VM image rebake
- MITM (rare but real — check with the user before clearing)

Do **not** auto-clear `~/.ssh/known_hosts`. Show the user the offending line number (`ssh-keygen -F <host>`) and let them decide.

## Daemon won't start

```
ssh_daemon.py status <alias>
```

Look for `state_file_present: true, port_responsive: false`. That's a zombie daemon. Recovery:

```
ssh_daemon.py stop <alias>     # removes state file + kills recorded PID if alive
```

If `port_responsive: true` but commands time out, the SSH transport is wedged (network change, suspend/resume). Same recovery — stop and let it respawn.

## Tunnel claims it's up but `nc localhost <port>` refuses

Three possibilities:

1. The forward is bound to `127.0.0.1` and you're trying to hit it from another container. Use `--bind-address 0.0.0.0` explicitly (and only if you know what you're doing).
2. The remote service is listening on `127.0.0.1` only, so the tunnel reaches the host but the host refuses. Use `--remote-host` to name the actual interface, e.g. `--remote-host 10.0.0.5`.
3. The daemon died after starting the listener. Check `ssh_tunnel.py status`.

## `ProxyJump` failures

Symptom: `Could not connect to <bastion>`.

```
ssh_execute.py <bastion> "echo ok"
```

If that fails, the chain's first hop is broken — fix the bastion alias before chasing the downstream alias.

If the bastion is reachable but the next hop fails, the bastion can't see the next hop (network/firewall). Confirm with `ssh_execute.py <bastion> "nc -zv <next-hop> 22"`.

## Windows path gets mangled

You see something like `C:/Program Files/Git/srv/release.tar` on the remote side. The `MSYS_NO_PATHCONV=1` env var isn't reaching the spawn. Verify:

```python
# Every transport call must spawn with this env addition:
env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
subprocess.run(argv, env=env, ...)
```

If you find a path that *doesn't* set this, that's the bug.

## Large file transfer hangs near the end

Probably `BatchMode` plus a server-side `MaxSessions` limit. `direct` mode on `ssh_server_transfer.py` opens *three* sessions on the source machine (control + data + the spawned `scp`). Lower the parallelism or use `stream` mode.

## "Too many authentication failures"

You have more keys in `ssh-agent` than the server allows attempts. Pin the key:

```sshconfig
Host <alias>
    IdentitiesOnly yes
    IdentityFile ~/.ssh/<the-right-key>
```

`ssh_config_manager.py` writes `IdentitiesOnly yes` whenever it adds a host with an `IdentityFile`.

## Daemon log location

`$TMPDIR/ssh_daemon/<md5(alias)>.log` — keeps the last 1 MB, rotates in place. If `ssh_daemon.py status` is unhelpful, the log usually isn't.

## Last resort

If you can't reproduce a failure through `ssh-core` but the same `ssh <alias> <cmd>` works from a plain terminal: report the bug. The wrapper's *job* is to be transparent to native `ssh` behavior.
