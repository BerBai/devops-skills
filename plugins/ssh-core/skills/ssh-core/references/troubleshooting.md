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

**v1.0: there is no daemon.** `ssh_execute` is BatchMode-only and spawns one `ssh` subprocess per call (ADR-001 D1). The `ssh_daemon.py` stub returns `unimplemented()` and is deferred to v1.1 (likely OpenSSH ControlMaster rather than a custom Python daemon). If a future v1.1 ships the daemon, this section will document recovery for zombie daemons (state file present, port unresponsive) via `ssh_daemon.py stop <alias>`.

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

**v1.0: there is no daemon, so no daemon log.** Deferred to v1.1 (see "Daemon won't start" above). When the daemon ships, logs will land at `$TMPDIR/ssh_daemon/<md5(alias)>.log` (1 MB rotating).

## `deploy_pubkey` quirks

Two `ssh-copy-id` integration gotchas worth knowing:

**Idempotency override.** Some OpenSSH builds (notably older ones) make `ssh-copy-id` exit non-zero when *every* key in the pubkey file is already installed, accompanied by stderr `WARNING: All keys were skipped because they already exist on the remote system.` `deploy_pubkey.py` reclassifies that case as success with `data.outcome="already_installed"` — it's an idempotent operation, not a failure. If you write your own ssh-copy-id wrapper, mirror this; otherwise re-runs of a successful deploy will misreport as broken.

**Pubkey first-line sanity check.** `deploy_pubkey.py` preflight refuses any `--key` whose first line does NOT start with one of `ssh-rsa / ssh-ed25519 / ecdsa-sha2-* / ssh-dss / sk-ssh-ed25519 / sk-ecdsa-sha2-`. Reason: a user who passes `~/.ssh/id_ed25519` (the **private** key) instead of `~/.ssh/id_ed25519.pub` would otherwise have their private key written into the remote `authorized_keys` file. The preflight catches it with `failure_class="key_file_missing"` before any subprocess spawns.

## Last resort

If you can't reproduce a failure through `ssh-core` but the same `ssh <alias> <cmd>` works from a plain terminal: report the bug. The wrapper's *job* is to be transparent to native `ssh` behavior.
