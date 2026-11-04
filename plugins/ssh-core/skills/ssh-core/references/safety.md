# ssh-core safety baseline

Things `ssh-core` does to avoid the obvious foot-guns. Read this if you're adding a new script or wiring a new transport.

## Hard rules

1. **No shell strings.** All `subprocess` calls use a list of arguments. We never join with spaces and pass to `shell=True`. The regression suite (`tests/test_local_route.py`) feeds `;`, `&&`, `&`, `|`, `` ` ``, `$()`, `^`, `%VAR%` and `!VAR!` through the `local` route and asserts they are **refused** rather than silently interpreted, and that `bash -c '...'` still round-trips them verbatim. Unicode RTL marks and emoji round-trip byte-for-byte.

2. **No credentials in argv.** Passwords are read from an environment variable (default `SSH_PASSWORD`) and handed to OpenSSH through one of two mechanisms: a `pty.fork()` child (POSIX), or a 0600 `SSH_ASKPASS` helper in a 0700 directory (Windows, and any caller without a tty). They never enter argv, the JSON envelope, stderr, or the audit log; any accidental echo is scrubbed from captured output before it leaves the process. (There is no `sshpass` path — that historical note is gone.)

3. **No host details in error strings.** `lib.redact` resolves the alias with `ssh -G` (a pure config parse, no network) and replaces `user@host`, `HostName`, `Port` and `IdentityFile` with `<alias>` / `<host>` / `<port>` / `<key>` in `stdout`, `stderr`, `data.raw_stderr` and `data.ssh_argv` before the envelope is emitted. A *name* alias (`lab-240`) is kept — it is the handle the caller already knows — but an alias that **is an address literal** (`192.168.30.240`, `[fe80::1]`) is treated like a `HostName` and becomes `<host>`, including inside `data.ssh_argv`; otherwise the same envelope would say `<alias>` in `stderr` and print the address in argv. `find` hides the key path unless `--show-key` is passed. `--no-redact` exists for debugging and is documented as such. (A separate, not-yet-published `ssh-guarded` skill was once planned for heavier approval/redaction policy; nothing in this repository depends on it.)

4. **Strict host key checking on every route, identically.** exec/password/SCP paths use `StrictHostKeyChecking=yes` plus `UpdateHostKeys=no`; the SFTP path uses paramiko `RejectPolicy` (never `AutoAddPolicy`). `--accept-new-hostkey` is a per-call opt-in with `accept-new` semantics: it persists an unknown key once and **still fails** when an existing key no longer matches. It applies to the key route, the password route and the tunnel route alike (it used to be silently dropped on the key route). We never write `accept-new` into the config.

5. **`MSYS_NO_PATHCONV=1` on every spawn.** Windows-only, harmless elsewhere. Without it, `/srv/foo` becomes `C:/Program Files/Git/srv/foo` in the remote argv.

6. **Only validated `-o` options pass through.** `ProxyJump` is the only proxy mechanism we wire from config. Caller-supplied `-o` is checked by `lib.validate_ssh_option`: it must be a single-line `KEY=VALUE`, and `ProxyCommand` / `LocalCommand` / `PermitLocalCommand` / `Match` / `KnownHostsCommand` / `RemoteCommand` / `IdentityAgent` are refused with `failure_class: "invalid_option"`.

7. **Config writes are locked, atomic and validated.** `ssh_config_manager` takes a cross-process lock (`lib.configfile.ConfigLock`), re-reads the file under it, renders, refuses any valueless directive, checks the result with `ssh -F <tmp> -G <alias>`, and only then replaces the file — keeping a uniquely named backup. A concurrent writer can no longer lose an update, and an empty `--user ''` deletes the field instead of emitting `User ` (which makes OpenSSH reject the **entire** config).

8. **Wildcard `Host` blocks are visible but not editable.** `list` marks `*` / `web-*` rows with `wildcard: true`; `create`/`update`/`delete` refuse them unless `--allow-wildcard` is passed, so a typo cannot remove the global defaults.

## Soft conventions

- Stdout / stderr are captured with a 10 MB cap per stream, counted in **bytes** (streamed, not `subprocess.run(capture_output=True)`). Over the cap, the excess is dropped, a `\n... [truncated, N bytes elided]\n` marker is appended *outside* the cap, and `data.truncated` / `data.bytes_elided` / `data.bytes_captured` report it on every path. The child keeps running to completion — we never signal it early.
- Output is kept as bytes until the envelope is written, then decoded once as UTF-8 with `errors="replace"`. Undecodable bytes become U+FFFD and are counted (`data.decode_replaced`, `data.lossy_output`) instead of emptying the stream; `\r\n` and a lone `\r` are preserved.
- A timeout returns everything captured before the kill: `failure_class: "timeout"`, `data.partial_output: true`, exit 124. **Only the local `ssh` client is killed** — the remote command can keep running, because a killed client never sends `SSH_MSG_CHANNEL_CLOSE`. Use a remote `timeout N` when the target must stop too.
- A child killed by a signal is reported as `128 + N` with `data.terminated_by_signal: N`, never as a negative number.
- sshd's pre-auth banner is removed from `stderr` (reported in `data.ssh_banner_lines`), so an empty `stderr` really means the command was silent. The client keeps its default log level on purpose: `LogLevel=ERROR` loses `Connection refused` and `Unable to negotiate ...`, which are what `failure_class` is built from.
- Per-alias state files (tunnels, daemons) live under `$TMPDIR/ssh_tunnel/` and `$TMPDIR/ssh_daemon/` with mode `0o600`. The file *name* is `md5(...)`, not the alias, so casual `ls /tmp` doesn't reveal which hosts you talk to. Daemon files also carry the bearer token that protects the loopback control port.
- Audit logs live under `$TMPDIR/ssh-core/<md5(alias)>.jsonl` (directory `0o700`, file `0o600`, append-only JSONL, rotated at 10 MB into `.1` … `.3`). Every script that changes state or moves bytes can write them: `ssh_session` defaults to on, everything else takes `--audit-log`, and `SSH_CORE_AUDIT=on|off` sets the default for the whole skill. Records never contain the password.
- Backup files for `~/.ssh/config` are `<cfg>.bak.<epoch-ms>-<pid>-<n>`: unique even within one second, never deleted automatically, and restorable with `ssh_config_manager.py restore [--from <bak>] [--dry-run]`.
- Every script accepts `--json` (before *or* after the verb) and emits the shared output contract — **including on argument errors**, which used to exit 2 with an empty stdout. Every script also answers `--version`. Human mode prints at least one `ssh-core: exit_code=N` line to stderr on failure.
- Exit codes come from one table (`lib/exit_codes.py`): one `failure_class` → one code, with stable buckets 0-9 and per-family hundreds. A remote command's own exit status is still passed through untouched.

## Things we deliberately don't do

- **No auto key deployment.** Public key deployment requires human intent. `deploy_pubkey.py <alias>` installs your pubkey explicitly over an ordinary SSH exec channel (no `ssh-copy-id` needed), verifies that key-only auth works, and only then reports success.
- **No auto-accept of new host keys.** Even on first connect.
- **No password storage in our own files.** The password exists only in the environment and, for the duration of one call, in a 0600 askpass file that is deleted in a `finally`.
- **No telemetry.** Nothing leaves the local machine except SSH traffic to the hosts you asked us to talk to.

## What this skill is *not* responsible for

- Asking the user to confirm arbitrary destructive commands. `ssh-core` refuses
  only what it can prove is destructive — `--delete` mirrors require `--yes`,
  wildcard `Host` blocks need `--allow-wildcard` — and leaves the rest to you.
  A separate `ssh-guarded` skill was planned for the general
  request → review → execute flow; **it is not published**, so do not route
  through it.
- Sandboxing remote paths into a workdir. Not implemented anywhere yet.
- Redacting host names from the chat transcript itself. The envelopes are
  redacted; what a caller prints afterwards is the caller's business.

If you need those, they do not exist yet: say so rather than pointing at a
skill that cannot be installed.

## Non-interactive escalation (DV-14)

`ssh_execute --sudo` is the only privilege-escalation path, and it is built so
it cannot hang: `sudo -n` (never `-A`, never a tty prompt) with a `sudo -n true`
preflight. A machine without a `NOPASSWD` rule fails with
`failure_class: "sudo_required"` (exit 112) *before* the real command runs.
`--sudo-password-env VAR` is the deliberate exception: the secret comes from
the environment, travels over stdin (`sudo -S`), and never touches argv.

## Destructive-by-design flags

- `--delete` on `ssh_upload` / `ssh_download` is a *mirror*: it removes entries
  that exist only on the destination. It requires `--yes`, requires
  `--recursive`, refuses to run when the copy itself recorded a failure
  (pruning next to a half-copied tree is how a mirror destroys data), and is
  refused outright with `mode_deferred` (166) when the active transport is scp
  or tar — neither can prune, and reporting success without pruning would be a
  lie.
- `ssh_config_manager delete` and every write refuse wildcard blocks unless
  `--allow-wildcard` is explicit, and all four write/lookup paths resolve
  aliases **exactly** (case-insensitive). A `Host *` block therefore can never
  capture `create`/`update`/`delete` — before 1.3.2 it captured all three, and
  `delete no-such-host` removed the global defaults.

## Audit trail

- Files live under `$TMPDIR/ssh-core/<md5(alias)>.jsonl` (directory `0o700`,
  file `0o600`, append-only JSONL).
- Rotation is policy-driven (DV-13): **10 MB** per file with **3** generations
  (`.1` … `.3`), overridable per run with `SSH_CORE_AUDIT_MAX_BYTES` and
  `SSH_CORE_AUDIT_KEEP`.
- Retention is a command, not a wish: `audit.py prune --older-than 30d`,
  `audit.py rotate --max-bytes 10MB`, `audit.py summarize --since 24h`.
- Records never contain the password; they carry `ts`, `host`, `route`,
  `command`/`direction`, `exit_code`, `failure_class`, `elapsed_s`, `bytes`.
  The `session` route is the exception: it records `commands` and `exit_ok`
  instead of a per-command `exit_code`/`failure_class`/`bytes`.
