# ssh-core safety baseline

Things `ssh-core` does to avoid the obvious foot-guns. Read this if you're adding a new script or wiring a new transport.

## Hard rules

1. **No shell strings.** All `subprocess` calls use a list of arguments. We never join with spaces and pass to `shell=True`. The test suite includes inputs containing `;`, `&&`, `` ` ``, `$()`, and unicode RTL marks. They must round-trip verbatim to the remote command line.

2. **No credentials in argv.** Passwords are passed through paramiko in-process. The legacy fallback to `sshpass` (when present) reads the password from a file descriptor opened with `O_CLOEXEC`, never from `-p`.

3. **No host details in error strings.** When we surface an error, we keep the alias but drop `HostName`, `User`, `Port`, and `IdentityFile`. (Heavier redaction is the job of `ssh-guarded`. `ssh-core` only does this minimum.)

4. **Strict host key checking on by default.** `StrictHostKeyChecking=yes` plus `UpdateHostKeys=no`. The user has to opt in to a one-shot relaxation with `--accept-new-hostkey` per call. We never write `accept-new` into the config.

5. **`MSYS_NO_PATHCONV=1` on every spawn.** Windows-only, harmless elsewhere. Without it, `/srv/foo` becomes `C:/Program Files/Git/srv/foo` in the remote argv.

6. **No `-o ProxyCommand=...` constructed from user input.** `ProxyJump` is the only proxy mechanism we wire. It reads from the user's config, not from caller arguments.

## Soft conventions

- Stdout / stderr captured into memory have a 10 MB cap per stream. Over the cap, we truncate and append `\n... [truncated, N bytes elided]\n`.
- Daemon state files live under `$TMPDIR/ssh_daemon/` with mode `0o600`. The state file *path* is `md5(alias)`, not the alias itself, so casual `ls /tmp` doesn't reveal which hosts you talk to.
- Backup files for `~/.ssh/config` use `.bak.<unix-ts>`. We never delete old backups automatically; that's the user's call.
- Every script accepts `--json` and emits the shared output contract. Human mode (no `--json`) is for terminal use, not for parsing.

## Things we deliberately don't do

- **No auto-`ssh-copy-id`.** Public key deployment requires human intent. There is a planned `deploy_pubkey.py` that you have to invoke explicitly.
- **No auto-accept of new host keys.** Even on first connect.
- **No password storage in our own files.** If the user records one in a comment in `~/.ssh/config`, that's their decision; we read it but never write it.
- **No telemetry.** Nothing leaves the local machine except SSH traffic to the hosts you asked us to talk to.

## What this skill is *not* responsible for

- Asking the user to confirm destructive commands. That's `ssh-guarded`.
- Sandboxing remote paths into a workdir. That's `ssh-guarded`.
- Redacting host names from chat output. That's `ssh-guarded`.

If you're tempted to add those features here, install `ssh-guarded` instead and route through it.
