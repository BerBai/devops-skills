# ssh-core troubleshooting

Symptom → likely cause → fix. Ordered by frequency.

## "Permission denied (publickey)"

| Likely cause | Check | Fix |
|---|---|---|
| Wrong `IdentityFile` for this host | `ssh_config_manager.py find <alias>` | Update `IdentityFile` |
| Key file mode too open | `ls -l ~/.ssh/id_*` should be `600` | `chmod 600` |
| Key not loaded in `ssh-agent` (passphrase keys) | `ssh-add -l` | `ssh-add ~/.ssh/<key>` |
| Public key not installed on remote | login interactively once | `deploy_pubkey.py <alias>` (interactive password prompt) |
| User mismatch | `User` field in config | Update `User` |

## "Host key verification failed"

The remote host's key changed since last connect. Causes:
- Server reinstall
- Container/VM image rebake
- MITM (rare but real — check with the user before clearing)

Do **not** auto-clear `~/.ssh/known_hosts`. Show the user the offending line number (`ssh-keygen -F <host>`) and let them decide.

## Daemon won't start

There is still no custom Python daemon. `ssh_daemon.py` remains a stub and returns `unimplemented()`.

For repeated `ssh_execute.py` calls, V1.1 starts with OpenSSH ControlMaster instead:

```bash
ssh_execute.py <alias> "uptime" --control-master auto
```

OpenSSH owns the master socket and expires it through `ControlPersist`. To bypass a suspected reuse issue, rerun the command without `--control-master auto`; the default path is still a fresh `ssh` subprocess per call.

## Tunnel claims it's up but `nc localhost <port>` refuses

Three possibilities:

1. The forward is bound to `127.0.0.1` and you're trying to hit it from another container. Use `--bind-address 0.0.0.0` explicitly (and only if you know what you're doing).
2. The remote service is listening on `127.0.0.1` only, so the tunnel reaches the host but the host refuses. Use `--remote-host` to name the actual interface, e.g. `--remote-host 10.0.0.5`.
3. The forwarder ssh process died after binding the listener. Check `ssh_tunnel.py status <alias>` — `running: false` means the detached ssh exited; the state file lingers until `stop` is called. ssh-side server-alive probes (`ServerAliveInterval=60` / `ServerAliveCountMax=3` baked into v1.0) detect a dead master but **do not** auto-reconnect; the tunnel exits if SSH drops.

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

**v1.0 note**: only `stream` ships, so this scenario only applies once v1.1 lights up `direct`. In v1.0, `--mode direct` refuses with `failure_class=mode_deferred` (exit 20) before any session is opened.

## "Too many authentication failures"

You have more keys in `ssh-agent` than the server allows attempts. Pin the key:

```sshconfig
Host <alias>
    IdentitiesOnly yes
    IdentityFile ~/.ssh/<the-right-key>
```

`ssh_config_manager.py` writes `IdentitiesOnly yes` whenever it adds a host with an `IdentityFile`.

## Daemon log location

There is no custom daemon log because no custom daemon ships. OpenSSH ControlMaster does not write a repo-managed log; inspect the command's captured `stderr` / `data.raw_stderr`, or rerun without `--control-master auto` to compare behavior.

## `deploy_pubkey` quirks

Two `ssh-copy-id` integration gotchas worth knowing:

**Idempotency override.** Some OpenSSH builds (notably older ones) make `ssh-copy-id` exit non-zero when *every* key in the pubkey file is already installed, accompanied by stderr `WARNING: All keys were skipped because they already exist on the remote system.` `deploy_pubkey.py` reclassifies that case as success with `data.outcome="already_installed"` — it's an idempotent operation, not a failure. If you write your own ssh-copy-id wrapper, mirror this; otherwise re-runs of a successful deploy will misreport as broken.

**Pubkey first-line sanity check.** `deploy_pubkey.py` preflight refuses any `--key` whose first line does NOT start with one of `ssh-rsa / ssh-ed25519 / ecdsa-sha2-* / ssh-dss / sk-ssh-ed25519 / sk-ecdsa-sha2-`. Reason: a user who passes `~/.ssh/id_ed25519` (the **private** key) instead of `~/.ssh/id_ed25519.pub` would otherwise have their private key written into the remote `authorized_keys` file. The preflight catches it with `failure_class="key_file_missing"` before any subprocess spawns.

## Last resort

If you can't reproduce a failure through `ssh-core` but the same `ssh <alias> <cmd>` works from a plain terminal: report the bug. The wrapper's *job* is to be transparent to native `ssh` behavior.
