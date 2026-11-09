# ssh-core troubleshooting

Symptom → likely cause → fix. Ordered by frequency.

## "Permission denied (publickey)"

| Likely cause | Check | Fix |
|---|---|---|
| Wrong `IdentityFile` for this host | `ssh_config_manager.py find <alias>` | Update `IdentityFile` |
| Key file mode too open | `ls -l ~/.ssh/id_*` should be `600` | `chmod 600` |
| Key not loaded in `ssh-agent` (passphrase keys) | `ssh-add -l` | `ssh-add ~/.ssh/<key>` |
| Public key not installed on remote | login interactively once | `deploy_pubkey.py <alias>` |
| User mismatch | `User` field in config | Update `User` |
| Wrong port / ad-hoc host | `ssh_execute.py 'root@10.0.0.5:2222' "id"` | works as-is since 1.1 (`-p` is added for you) |

The envelope shows `<alias>: Permission denied (...)` — the real `user@host`
is redacted on purpose (`safety.md` rule 3). Run with `--no-redact` while
debugging if you need to see exactly what ssh tried.

## "Permission denied (keyboard-interactive,password)"

The host does not accept keys. `ssh_execute` reports
`failure_class: "auth"` — switch to password auth:

```bash
SSH_PASSWORD='...' python scripts/ssh_execute.py <alias> "uptime" --auth password
```

This works on Windows too: the password is answered through the `SSH_ASKPASS`
helper over a normal **exec** channel (no pty, no `-tt` needed). `data.transport`
says `pipe` there and `pty` on POSIX; either way stdout and stderr stay separate
and `exit 3` really is 3.

For an interactive CLI (switch/router/firewall) use `ssh_session.py`
instead — one exec command cannot disable paging or answer `[Y/N]`.
`ssh_config_manager.py create ... --auth password --device-type network`
makes `--auth auto` route there automatically for that alias.

## "user_required" (exit 115) — the username, not the password

Password auth needs a login name. If the target is a bare IP, the target has
no `user@`, and the matched `~/.ssh/config` block has no `User`, then ssh
falls back to your **local** account name (on Windows, `PCFM-084`, not
`root`) and the failure reads `Permission denied` — blaming the password for a
missing username.

ssh-core now refuses up front instead:

```
failure_class: "user_required"   exit 115
```

Three fixes, any one of them:

```bash
python scripts/ssh_execute.py --user root 192.168.30.54 "id" --auth password
python scripts/ssh_execute.py root@192.168.30.54 "id" --auth password
python scripts/ssh_config_manager.py update --alias lab-54 --user root
```

`ssh_cluster.py` forwards `--user` to every worker, so a fleet of bare IPs is
one flag away. The envelope also says *where* the user came from
(`data.user_source`: `flag` / `target` / `config`), which is the quickest way
to spot a config block that never matched.

## "askpass_unavailable" (exit 114) — no writable scratch directory

Password auth stages two files in a private directory: a 0600 `secret` and a
small `askpass` helper (a `.cmd` on Windows). Both are deleted afterwards. If
**none** of these directories can be created and written to, ssh-core cannot
answer the prompt at all and says so instead of raising a traceback:

1. `$SSH_CORE_ASKPASS_DIR` — used exactly as given;
2. `$SSH_CORE_ASKPASS_HOME/ssh-core-askpass`;
3. `$TMPDIR/ssh-core-askpass` (`%TEMP%` on Windows);
4. `~/.cache/ssh-core-askpass`;
5. `$DSH_HOME/tmp/ssh-core-askpass`;
6. `./.ssh-core-askpass`.

```bash
# pick a directory that really is writable for this user and pin it
export SSH_CORE_ASKPASS_DIR="$HOME/.ssh-core-askpass"     # POSIX
$env:SSH_CORE_ASKPASS_DIR = "$env:USERPROFILE\.ssh-core-askpass"   # PowerShell
python scripts/doctor.py          # the `askpass` row proves it works
```

The envelope lists every path it tried in `data.askpass_tried`. On Windows the
directory is created normally and tightened with `icacls` — `mkdir(mode=0o700)`
produces a protected DACL that can omit the creating user, which is exactly how
an earlier version locked itself out of its own scratch directory.

## "no matching key exchange method found" / "no matching host key type found" / "no matching cipher found" / "no matching MAC found"

A 2013-era switch offers one short list per algorithm family, and OpenSSH 8.8+
plus the RHEL/Kylin system crypto policy no longer enable most of it. ssh
negotiates kex, host key, cipher and MAC in that order and reports **one**
missing family at a time, so each rerun gets exactly one step further:

| Symptom | Cause | Action |
|---|---|---|
| `no matching key exchange method found` | the device only offers `diffie-hellman-group-exchange-sha1` / `group1-sha1` | add `-o KexAlgorithms=+diffie-hellman-group-exchange-sha1` |
| `no matching host key type found` | the device only offers `ssh-rsa` | add `-o HostKeyAlgorithms=+ssh-rsa` |
| `no matching cipher found` | the device only offers the CBC family (`aes256-cbc,aes128-cbc,3des-cbc,des-cbc`) | add `-o Ciphers=+aes256-cbc,aes128-cbc` |
| `no matching MAC found` | the device only offers `hmac-sha1` | add `-o MACs=+hmac-sha1` |

The troubleshooting order is **kex → host key → cipher → MAC**: add one group
per round trip and rerun, and do not edit the global `~/.ssh/config` for a
one-off. `failure_class` is `host_key` (exit 101) for the host-key row and `kex`
(exit 103) for the other three.

The complete set, copy-pasteable:

```bash
python scripts/ssh_execute.py old-switch "display clock" \
    -o KexAlgorithms=+diffie-hellman-group-exchange-sha1 \
    -o HostKeyAlgorithms=+ssh-rsa \
    -o Ciphers=+aes256-cbc,aes128-cbc \
    -o MACs=+hmac-sha1
```

A device that also wants a password adds `SSH_PASSWORD='...' --auth password`
(and `--user` when the alias has no `User`).

`ssh_session.py`, `ssh_cluster.py`, `ssh_tunnel.py start` and
`ssh_daemon.py start` accept the same `-o KEY=VALUE` flags.
`ProxyCommand` / `LocalCommand` / `Match` are refused on purpose.

## Commands echoed, but `failure_class: auth` — a disconnect is not an auth failure

| Symptom | Cause | Action |
|---|---|---|
| the device printed its output (banner, prompt, command result) and the envelope still says `failure_class: "auth"` / exit 100 with `stdout: ""` | VRP / Comware / IOS close the SSH session once the command is done, and OpenSSH reports that close as `Received disconnect from <host> port 22:2: The connection is closed by SSH server` + `Disconnected from <host> port 22` | the command already ran, so do not chase the password: rerun it with `ssh_session.py`, or read the transcript out of `stderr` / `data.raw_stdout`. A build that recognises the graceful close returns `success: true` / `exit_code: 0` for this pattern |
| `stdout` sometimes starts with the login greeting, sometimes not | the greeting/answer boundary on a merged (`-tt`) stream is a race: on a fast device both arrive in the same read | look at `data.ssh_banner_applied` / `data.ssh_banner_unusable_reason` and `data.verdict_basis`; the greeting is only cut on evidence (see below), and never at the price of the command's result |
| `success: true` but the answer is missing from `stdout` | `data.ssh_banner_applied: false` with the greeting still present, or `data.ssh_farewell_lines_removed > 0` having removed ssh's trailer | read `data.raw_stdout` for the untouched bytes; report it as a bug if `verdict_basis.device_lines` is 0 while the device clearly answered |

A device does not hold an exec channel open the way sshd on a Linux host does:
the command runs, the CLI prints its result, and the session is torn down. ssh
then exits 255 — or 0, depending on whether it managed to read the channel close
before the device dropped TCP. The verdict therefore comes from the transcript,
never from the exit status:

- prompt and output, then `Received disconnect ... The connection is closed by
  SSH server` / `Disconnected from ... port 22` — the command ran.
  `ssh_execute.py` decides that from the transcript (output **and** a prompt
  line), not from ssh's exit status: `success: true`, `exit_code: 0`, transcript
  in `stdout`, and `data.device_closed_after_output: true` when ssh still
  reported 255. `ssh_session.py` uses the same helper, so both entry points
  return the same verdict for the same device and command.
- the same `Received disconnect` with **no device output** before it — a real
  rejection: wrong password, no free VTY, or an ACL. That stays
  `failure_class: "auth"` / exit 100. ssh's own closure lines are stripped
  before "is there output?" is asked, so a transcript that is nothing but the
  goodbye cannot be mistaken for a completed command, and a real auth rejection
  never echoes a prompt first.
- a device CLI that rejects the password on its own login prompt (`Error: Local
  authentication is rejected.`, `Bad login name or password`) is also `auth` /
  exit 100, even though the device re-prints its banner and prompt afterwards.
  The prompt shortcut vetoes itself when a credential rejection is present.
- the same device can report the close as `client_loop: send disconnect:
  Connection reset` when it drops TCP instead of closing the channel. That is
  the *client* noticing a dead socket, so it carries no verdict: the trailer is
  stripped (whole line, or glued to the command's last line when the device's
  answer had no trailing newline) and the verdict is taken from what is left.
  `data.ssh_farewell_lines_removed` counts what went.

`data.raw_stdout` still carries the untouched transcript.

### When `stdout` looks different from run to run

`ssh_execute.py` never guesses the greeting/answer boundary from shape alone.
Two fields say what it did, and they are the first thing to look at when one run
keeps the login greeting and the next does not:

| Field | Meaning |
|---|---|
| `data.ssh_banner_lines` | the greeting was cut; its lines are listed here |
| `data.ssh_banner_applied: false` + `data.ssh_banner_unusable_reason` | a greeting was seen but **not** cut, because cutting it would have deleted the command's result (or the boundary was unknowable). The greeting then stays in `stdout` |
| `data.verdict_basis` | the four inputs to the verdict (`rc`, `prompt_returned`, `device_closed`, `credentials_rejected`) plus whether the banner was applied and how many device lines were left |

A greeting left in `stdout` is deliberate: the alternative is deleting the
command's answer, which is what DEVOPS-BUG-008b was. Use `data.raw_stdout` when
the exact bytes matter, and `ssh_session.py` when the device CLI is the thing
being driven.

When the two entry points disagree about a device, `ssh_session.py` is the one
that sees the device CLI; `ssh_execute.py` is a one-command convenience. The
entry-point table in `SKILL.md` says which one a task should use.

## "password_required" (exit 105)

`--auth password` was selected (explicitly or by `# auth: password`
metadata) but the password environment variable is empty. Export
`SSH_PASSWORD` (or pass `--password-env OTHER_VAR`). Passwords are
intentionally never accepted as CLI flags.

## "host_key" / "Host key verification failed"

The host key is unknown, or it changed since last connect.

- **Unknown (first contact)**: verify the fingerprint out of band
  (`ssh-keyscan -t ed25519 <host>`) and rerun with `--accept-new-hostkey`.
  That applies to the key route, the password route and the tunnel route
  identically since 1.1 — before, the flag was silently ignored on the key
  route, which made the documented fix useless.
- **Changed**: causes are a server reinstall, a VM image rebake, or (rarely)
  MITM. Do **not** auto-clear `~/.ssh/known_hosts`. Show the user the
  offending entry (`ssh-keygen -F <host>`) and let them decide.

## Tunnel to a password-only host

`ssh_tunnel.py start` detaches with no controlling terminal, so OpenSSH
would normally refuse to prompt. Pass `--auth password` (or mark the alias
`# auth: password`); the password is read from `$SSH_PASSWORD` and handed to
ssh through a temporary `SSH_ASKPASS` helper (0600, inside a 0700 dir,
removed once the forward is up). Without `SSH_PASSWORD` the command fails
fast with `failure_class: password_required`, exit 105.

If the forward starts but nothing listens, the usual cause is auth on the
*remote* side (the ssh process died before binding). `ssh_tunnel.py
status <alias>` shows `running: false` in that case.

## Tunnel claims it's up but `nc localhost <port>` refuses

Three possibilities:

1. The forward is bound to `127.0.0.1` and you're trying to hit it from another
   container. Use `--bind-address 0.0.0.0` explicitly (and only if you know
   what you're doing).
2. The remote service is listening on `127.0.0.1` only, so the tunnel reaches
   the host but the host refuses. Use `--remote-host` to name the actual
   interface, e.g. `--remote-host 10.0.0.5`.
3. The forwarder ssh process died after binding the listener. Check
   `ssh_tunnel.py status <alias>` — `running: false` means the detached ssh
   exited; the state file lingers until `stop` is called.

## "tunnel_not_found" / "ambiguous_stop"

`stop` matches on `(alias, [--type], [--port])`. If the alias holds more than
one forward you must disambiguate with `--port` (and `--type` when the same
port is reused across types). `ssh_tunnel.py list` prints every forward with
its type and ports.

## "idle_stopped_at" is set and the tunnel is gone

That is the idle watchdog doing its job: `--idle-timeout` (default 1800 s)
stopped a tunnel that had no connections for that long. Pass
`--idle-timeout 0` for a tunnel that must live until `stop`.

## Device asks for the password twice

Some network devices authenticate twice: OpenSSH asks
`admin@host's password:`, then the device's own AAA prints
`User Authentication` + `Password:`. `ssh_session.py` answers up to 3
chained password prompts with the same `SSH_PASSWORD`, but only while the
session is silent — a Linux exec login that starts printing output is
never delayed by this.

On the pipe transport (Windows) ssh's own prompt is answered by the askpass
helper, so only the *server-side* prompt is typed. The driver tracks that
distinction explicitly: typing the secret into the remote shell was a real
bug during development.

## `--ssh-config` makes a legacy device fail with "no matching key exchange method found"

`ssh -F <file>` **replaces** the system config instead of layering on it.
On RHEL / Kylin the system config carries
`Include /etc/crypto-policies/back-ends/openssh.config`, which is the only
reason `diffie-hellman-group1-sha1` / `aes256-cbc` (offered by 2013-era
switches) are still enabled. Dropping it silently narrows the algorithm
list, so plain `ssh <alias>` connects but `--ssh-config` does not.

Fixed in the scripts: a caller-supplied config is wrapped in a cached
temp file (`$TMPDIR/ssh-core/cfg-<md5>.conf`) that includes the user
config first and `/etc/ssh/ssh_config` second, so both user intent and
the crypto policy survive. If you hit this on an older copy, add
`Include /etc/ssh/ssh_config` to the file you pass.

## Deploying the skill onto an older host

The scripts run on **Python 3.8+** — no `zip(strict=)`, no
`str.removeprefix`, no match statements, no `Path.write_text(newline=)` and no
`TemporaryDirectory(ignore_cleanup_errors=)` (both 3.10+; `tools/check_python_floor.py`
enforces all of it, including the test suite).

There is a supported installer for this now — don't copy files by hand:

```bash
# from the checkout: build, copy, install, verify in one step
make install-host HOST=root@192.168.30.231
SSH_PASSWORD='...' make install-host HOST=root@192.168.30.231 \
    HOST_AUTH_ARGS="--auth password"          # password-only host
```

It lands in `/opt/ssh-core` (override `SSH_CORE_PREFIX`), keeps the previous
tree as `/opt/ssh-core.bak.<timestamp>`, and links it into `~/.dsh/skills` and
`~/.agents/skills`. On the target, `tools/install-to-host.sh` verifies the
archive's `.sha256` and the bundle's own `SHA256SUMS`; `tools/verify-host-install.sh`
then runs 24 contract checks (see README "Install onto another host").

Four host-specific gotchas found while doing this on 192.168.30.231 (Kylin 3,
OpenSSH 8.2p1) and 192.168.30.239:

- **Key trust is per-host.** Running the skill *on* a jump host only works
  if that host's own key is authorized on the targets; otherwise every
  call returns `failure_class: auth` — which is correct behaviour, not a
  bug. Check with `ssh -o BatchMode=yes <target> true` first, or use
  `--auth password` with `SSH_PASSWORD` exported there.
- **`--accept-new-hostkey` covers both routes** (key and password). It
  persists the key once and still fails on a changed key.
- **`scp -O` is OpenSSH 9.0+.** On an 8.x client the flag is rejected
  (`unknown option -- O`), so the transfer backends probe for it once and only
  pass it when it is understood. Nothing to configure; just don't hardcode `-O`
  in your own wrapper.
- **A `.sha256` sidecar must be LF.** A CRLF sidecar makes `sha256sum -c` report
  `'file.zip'$'\r': No such file`, which reads like a corrupt download. The
  build tool writes LF; if you copy a sidecar by hand from Windows, convert it.

## Real-device timings (192.168.30.240, OpenSSH 8.2)

Measured after the 1.1 fixes, useful as sanity baselines:

| Scenario | Exit | failure_class | Elapsed |
|---|---|---|---|
| key auth, `hostname` | 0 | — | ~0.22 s |
| password auth (askpass pipe), `id -un` | 0 | — | ~0.35 s |
| wrong password (exec host) | 255 → 100 | `auth` | ~4 s |
| wrong password (VRP switch, chained prompt) | 255 → 100 | `auth` | ~5 s |
| unknown host, strict checking | 255 → 101 | `host_key` | ~0.03 s |
| unknown algorithm (`-o KexAlgorithms=…`) | 255 → 103 | `kex` | ~0.03 s |
| unreachable host | 255 → 102 | `network` | = `--connect-timeout` |
| unknown *alias* (`Could not resolve hostname no-such`) | 255 → 4 | `alias_not_found` | ~0.03 s |
| missing `--ssh-config` path | 6 | `config_missing` | — |
| command timeout | 124 | `timeout` | = `--timeout` |
| remote command exits 42 | 42 | `remote_error` | — |
| local metacharacter refused | 2 | `invalid_arguments` | — |

## "prompt_timeout" / "confirmation_required" from ssh_session.py

- `prompt_timeout`: the device's prompt does not match the heuristic. Pass
  `--prompt-regex` with your device's actual prompt (e.g.
  `--prompt-regex '^<[\w-]+>$'`) and/or raise `--step-timeout`.
- `confirmation_required`: the command asked `[Y/N]` and `--confirm=ask`
  (the default) refused to guess. Rerun with `--confirm yes` (or `no`)
  after checking which command it was.

## "host_key" on an SFTP transfer

The SFTP route is as strict as exec: unknown hosts are rejected
(`failure_class: "host_key"`). Verify the fingerprint with
`ssh-keyscan -t ed25519 <host>` and rerun with `--accept-new-hostkey`.
A *changed* key always fails — clear the stale entry deliberately
(`ssh-keygen -R <host>`) after confirming with the owner.

## "dependency_missing" (exit 3) from the transfer scripts

Two very different cases now:

- `--transport sftp` was requested explicitly and paramiko is missing. Install
  it (`pip install 'paramiko>=3.0,<4'`) or drop the flag.
- `auto` ran the scp fallback and **`scp` itself** is missing. Check
  `data.transport` and `data.scp_argv`: a stock OpenSSH client always ships
  `scp`, so this usually means `PATH` is broken or the client is a
  stripped-down build.

`auto` (the default) never fails just because paramiko is absent: uploads use
`scp -O`, downloads use `scp -O` or a remote `tar` stream, and the envelope
records `data.transport = "scp"|"tar"` plus a `transport_note` explaining
which options (`--resume`, `--chmod`, `--bwlimit`) were ignored.

`--delete` is the one thing the fallback cannot do: neither scp nor tar can
prune. Rather than report `success: true` without pruning, the script returns
`failure_class: "mode_deferred"` (exit 166) with a `pip install paramiko` hint.
`--preserve` *is* honoured (`scp -p`), and the envelope says
`preserved: true`.

## Server-to-server transfer mode issues

| Symptom | Fix |
|---|---|
| `direct` says `dependency_missing` | `tar`/`ssh` missing locally, or `--use-rsync` without `rsync` on the **source** host (rsync runs there: both endpoints are remote) |
| `direct` says `remote_path_missing` | the *source* path is wrong, or the destination directory cannot be created |
| `stream` says `dependency_missing` | `--mode stream` needs paramiko |
| want incremental re-sync | `--mode direct --use-rsync` (needs `rsync` on the **source** host) |
| want no paramiko at all | `--mode auto` picks `direct` when paramiko is absent |

## "integrity_mismatch" (exit 165) after a transfer

The sha256 of the local copy and the remote file differ. The file is on disk
but must not be trusted. Re-run the transfer; if it repeats, suspect a proxy
that rewrites payloads, a full disk (`df -h` on the destination), or a
truncated `scp` with a flaky link.

## "invalid_arguments" (exit 2) with a JSON envelope

That is the normal answer to a wrong command line: every script now emits the
shared envelope even when argparse rejects the arguments, with
`data.message` and `data.usage` filled in. Read `data.message` — it names the
exact flag.

## "wildcard_refused" (exit 180)

`create` / `update` / `delete` refuse `Host *` and other glob patterns,
because removing the global defaults silently changes every host. `list`
shows them with `--include-wildcards` (marked `wildcard: true`). Pass
`--allow-wildcard` if you really mean it.

## `ssh -G` says "no argument after keyword" after editing the config

That was the old `update --user ''` bug: it wrote a valueless directive, which
makes OpenSSH reject the **entire** file. Since 1.1 an empty value deletes the
field, the renderer refuses to emit valueless directives at all, and every
write is validated with `ssh -F <tmp> -G <alias>` first. If you already have a
broken file, fix it with:

```bash
ssh_config_manager.py validate                       # names the offending line
ssh_config_manager.py restore --list-backups         # pick a good one
ssh_config_manager.py restore --from latest --diff
```

## "permission denied writing" during concurrent edits

The config lock now serialises writers. If you still see
`failure_class: permission` with a lock-timeout message, another process has
held `<cfg>.lock` for more than 10 s — usually a stuck editor helper. Remove
the stale process (the `.lock` file itself is never deleted, by design).

## "daemon_not_running" (exit 200) / stale daemon

`ssh_daemon.py status <alias>` distinguishes the two cases. A stale state file
means the daemon died (host reboot, session dropped): `stop` clears the
file, `start` brings a new one up. The daemon exits on its own after
`--idle-timeout` seconds without requests.

For repeated `ssh_execute.py` calls the simpler options are:

```bash
ssh_execute.py <alias> --commands-file cmds.txt        # one connection, N commands
ssh_execute.py <alias> "uptime" --control-master auto  # POSIX only
```

## Windows: what works and what does not

| Capability | Windows |
|---|---|
| `ssh_execute` key auth (local + remote) | yes |
| `ssh_execute --auth password` | **yes** (`ssh -tt` + `SSH_ASKPASS`, `data.transport="pipe"`) |
| `ssh_session.py` (VRP/Comware/IOS) | **yes**, same state machine over pipes |
| `deploy_pubkey.py` (`--password` and key route) | **yes** (no `ssh-copy-id` needed) |
| `ssh_upload` / `ssh_download` | yes — SFTP with paramiko, else `scp`/`tar` |
| `ssh_server_transfer` | yes — `stream` with paramiko, else `direct` (`tar`/`rsync`) |
| `ssh_cluster`, `ssh_config_manager`, `ssh_tunnel.py` | yes |
| `--control-master auto` | **no** (needs a POSIX socket) → use `--commands-file` |
| `ssh_daemon.py` | yes |
| `--sudo` (non-interactive) | yes — the escalation happens on the remote side |
| `--follow` / `--tail` | yes |
| `--probe tcp:PORT` (fleet health) | yes (plain socket, no ssh) |
| `ssh_config_manager list --format ansible\|yaml` | yes |
| `audit.py summarize\|prune\|rotate` | yes |
| `local` route with Windows paths (`C:\\Users\\...`) | yes |

Also useful on Windows: `python scripts/selfcheck.py` prints this machine's row
by row, and `references/platform-matrix.md` is the generated, full matrix.

Windows process control does **not** use `os.kill(pid, 0)` to probe a PID —
on Windows that call terminates the process instead of testing it. The
platform layer uses `OpenProcess` + `GetExitCodeProcess` and
`TerminateProcess`.

## Windows path gets mangled

You see something like `C:/Program Files/Git/srv/release.tar` on the remote side. The `MSYS_NO_PATHCONV=1` env var isn't reaching the spawn. Verify:

```python
# Every transport call must spawn with this env addition:
env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
subprocess.run(argv, env=env, ...)
```

If you find a path that *doesn't* set this, that's the bug.

## `--version` / version mismatch

Every script answers `--version` (and `--version --json`) from
`lib/version.py`. If the three places disagree (`plugin.json`, script banner,
CHANGELOG), the single source of truth is `__version__` in that module.

## "Too many authentication failures"

You have more keys in `ssh-agent` than the server allows attempts. Pin the key:

```sshconfig
Host <alias>
    IdentitiesOnly yes
    IdentityFile ~/.ssh/<the-right-key>
```

`ssh_config_manager.py` writes `IdentitiesOnly yes` whenever it adds a host with an `IdentityFile`.

## `deploy_pubkey` behaviour

`deploy_pubkey.py` no longer shells out to `ssh-copy-id`; it appends the key
itself over a normal exec channel:

```bash
umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys
grep -qxF '<key>' ~/.ssh/authorized_keys || echo '<key>' >> ~/.ssh/authorized_keys
```

so it is idempotent on every platform (`data.outcome = "already_installed"` on
a re-run) and needs no helper binary. Two failure classes are worth knowing:

- `verify_failed` (9): the key was appended but key-only auth still fails —
  almost always `~/.ssh` 0700 / `authorized_keys` 0600, an SELinux context
  (`restorecon -R ~/.ssh`), or `PubkeyAuthentication no` on the server.
- `key_file_missing` (141): the `--key` file is absent, unreadable, or its
  first line is not an OpenSSH public key. That last check exists because
  passing `~/.ssh/id_ed25519` (the private key) would otherwise write the
  private key into `authorized_keys`.

The `--ssh-config` + `ssh-copy-id` incompatibility (`ssh_config_unsupported`,
exit 142) only applies to the legacy `ssh-copy-id` path and is now
unreachable in practice.

## Host has no key yet and there is no terminal

Use the headless bootstrap — it works on Windows too:

```bash
SSH_PASSWORD='...' python scripts/deploy_pubkey.py <alias> \
    --password --accept-new-hostkey --promote-auth
```

It logs in over a password session (pty on POSIX, `ssh -tt` + askpass on
Windows), appends the key idempotently, **verifies key-only auth works**, and
with `--promote-auth` rewrites that host's `# auth:` metadata to `key` (and
installs `IdentityFile` / `IdentitiesOnly yes`) for later calls.

This path is for Linux/Unix hosts that have a shell and `~/.ssh`. A network
device has neither; it returns `unsupported_target` — keep it on
`ssh_session.py`.

## Where are the audit logs?

`$TMPDIR/ssh-core/<md5(alias)>.jsonl` (mode 0600 in a 0700 directory). Every
script can write them: `ssh_session.py` defaults to on, the others take
`--audit-log`, and `SSH_CORE_AUDIT=on|off` changes the default for the whole
skill. Each file rotates at **10 MB** into `.1` … `.3` (override with
`SSH_CORE_AUDIT_MAX_BYTES` / `SSH_CORE_AUDIT_KEEP`), so nothing grows without
bound. They contain commands, byte counts and exit codes — never passwords.

To read them back instead of grepping:

```bash
python scripts/audit.py summarize --since 24h     # calls/failures per host, worst first
python scripts/audit.py summarize --since 7d --host web-01 --json
python scripts/audit.py list                      # files, sizes, record counts
python scripts/audit.py prune --older-than 30d --dry-run
python scripts/audit.py rotate --max-bytes 10MB
```

`ssh_tunnel.py` and `ssh_daemon.py` do not write audit records unless
`--audit-log` is passed; the idle watchdog writes an `idle_stop` record when
`SSH_CORE_AUDIT=on`.

## `--sudo` fails with `sudo_required` (exit 112)

By design. `ssh_execute --sudo` runs `sudo -n` — non-interactive — so it fails
instead of hanging on a password prompt that has no terminal. The preflight
(`sudo -n true`) runs *before* your command, so nothing was executed.

Two fixes, in order of preference:

```bash
# 1. a NOPASSWD rule for exactly the commands the operator runs
echo '%sudo ALL=(root) NOPASSWD: /bin/systemctl' | sudo tee /etc/sudoers.d/ssh-core
sudo visudo -c

# 2. opt into a stdin password (never argv, never the envelope)
SSH_SUDO_PASSWORD='...' python scripts/ssh_execute.py <alias> --sudo \
    --sudo-password-env SSH_SUDO_PASSWORD -- "systemctl restart nginx"
```

A wrong password in that variable reports `sudo_auth_failed` (exit 113), not
`sudo_required`, so the two are distinguishable. If the preflight could not
reach the host at all, you get the *original* class instead (`auth` 100,
`host_key` 101, `network` 102, `alias_not_found` 4) plus
`data.sudo_preflight: "unreachable"`: fix the connection, not sudoers.

## `--follow` never returns

That is what `--follow` is for. It stops on Ctrl-C (exit 130, still a valid
envelope with `data.interrupted: true`), when the remote command exits, or at
`--timeout` (exit 124). Two common surprises:

- Piping it into `jq` with `--json`: the **live transcript goes to stderr** on
  purpose, so stdout stays one parseable JSON document. Redirect stderr
  (`2>live.log`) if you want to watch it separately.
- `--tail N` wraps the remote command as
  `stdbuf -oL -eL sh -c '<cmd>' | tail -n N -f` (falling back to a plain pipe
  where `stdbuf` is absent). That is what makes a slow log appear immediately;
  combining it with a command that is itself `tail -f` defeats it, because the
  inner `tail` never flushes through a pipe.

## Output looks empty / mangled (SSHCORE-015, 019, 021, 022)

| Symptom | Meaning | What to look at |
|---|---|---|
| `success: true`, `stdout: ""`, but the command definitely printed | Pre-1.4: a single invalid UTF-8 byte emptied the stream | now impossible; check `data.decode_replaced` / `data.lossy_output` |
| `data.lossy_output: true` | some bytes were not valid UTF-8 and became U+FFFD | expected for `/dev/urandom`, `xxd`, a GBK file name |
| `data.truncated: true` | more than 10 MB on one stream | `data.bytes_elided` says how much was dropped; the child was not killed |
| `\r\n` became `\n` in an old version | universal-newline translation in a text-mode pipe | fixed: pipes are binary, bytes are exact |
| `stdout_bytes_captured` too small for Chinese text | it used to be a character count | fixed: it is a byte count |

`LC_ALL=C` (a Kylin/RHEL minimal install) is a fully supported client
environment: decoding is pinned to UTF-8 regardless of the locale, and the same
applies to `ssh_cluster`, whose workers no longer crash on a non-ASCII byte.

## Timeout semantics (SSHCORE-020, 021)

- Only the **local** `ssh` is killed. The remote command keeps running: a
  SIGKILLed client never sends `SSH_MSG_CHANNEL_CLOSE`, so the far side cannot
  notice. Wrap the command yourself (`timeout 60 grep ...`) when the target must
  stop, or expect it to finish in the background.
- `--timeout -1` / `--timeout 0` / a negative `--connect-timeout` are rejected
  up front with `failure_class: "invalid_option"` (exit 107). They used to be
  forwarded and produced a perfectly normal-looking timeout envelope.
- A child killed by a signal reports `128 + N` and `data.terminated_by_signal`.

## Last resort

If you can't reproduce a failure through `ssh-core` but the same `ssh <alias> <cmd>` works from a plain terminal: report the bug. The wrapper's *job* is to be transparent to native `ssh` behavior.
