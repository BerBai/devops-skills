---
name: ssh-core
description: "SSH transport primitives — command execution (key and password auth), interactive network-device sessions, cluster broadcast, port-forward tunnels (-L/-R/-D), SFTP/scp/tar file transfer, a reusable session daemon, and `~/.ssh/config` CRUD. Use whenever the task touches ssh/scp/rsync, a remote host alias, user@host, an IP (192.168.x.x, 10.0.x.x), 跳板机/jumphost/bastion, or a switch/router/firewall CLI. Skip for localhost-only operations."
disable-model-invocation: false
---

# ssh-core

**CRITICAL:** This skill handles ALL SSH/SCP/RSYNC operations. Do **not** call `ssh`, `scp`, or `rsync` directly from Bash — use the scripts in this skill. Direct calls leak credentials into shell history, miss the shared `~/.ssh/config` resolution layer, and break on Windows MSYS path translation.

## Prerequisites

**Run this first on an unfamiliar machine:**

```bash
python scripts/selfcheck.py          # tools present on this box
python scripts/doctor.py             # what actually WORKS here (A3)
python scripts/doctor.py --profile full --host <alias>   # every plugin's backends
```

`selfcheck.py` prints one row per tool (`ssh`, `scp`, `sftp`, `ssh-keygen`,
`ssh-copy-id`, `rsync`, `tar`, `paramiko`, `~/.ssh/config`), then the list of
capabilities that work here and the ones that do not. Exit 0 means everything
*required* is present; missing optional backends are reported, not fatal.

`doctor.py` goes one step further and **runs** each capability instead of
looking for a binary: it stages a real `SSH_ASKPASS` helper, and with
`--host ALIAS` it performs a real login (key and password), a real
upload/download round trip with a sha256 check, and optionally a `sudo -n`
probe. Use `--require CAPABILITY` to gate a deployment on it:

```bash
python scripts/doctor.py --host lab-231 --require transfer --require exec_password
```

`--profile full` widens the probe from "can ssh-core run here" to "which of the
eight plugins can run here": it looks for `docker`/`podman`, `kubectl`/`helm`,
`terraform`/`tofu`, `gh`/`glab`, the optional Python modules (`yaml`, `hcl2`)
and the log tools (`journalctl`, `tail`, `nc`), prints one readiness row per
plugin, and returns the whole picture as `data.capabilities[]` plus
`data.plugin_readiness`, so a CI gate can ask for a plugin by name instead of
grepping prose (`--require k8s-debug` under this profile gates on readiness).
A `/` group is any-of: docker-quick is ready with either runtime, iac-state
with either tool, cicd-debug with either provider. Python modules are resolved
on this machine because the parsing happens here; binaries are resolved on the
target when `--host` is given, and the whole remote set costs **one** ssh round
trip rather than one per binary.

Three statuses, not two: `OK`, `DEGRADED` (the target answered and named a
missing backend), and `UNKNOWN` (the probe itself failed — an unreachable
alias). `UNKNOWN` exists so a host that could not be probed never produces a
green row. Run it once when you land on a new machine; that is the difference
between "eight manual probes" and one command. `selfcheck.py` stays a local,
dependency-free probe and names this command instead of duplicating it, and the
default `--profile transport` output is unchanged.

**No `python` on PATH?** That is the normal case on a DSH host: the interpreter
lives inside the harness payload. Use the bundled launcher — it finds
`$SSH_CORE_PYTHON`, then `$DSH_PYTHON`, then `python`/`py -3`, then the
`$DSH_HOME/dsh-runtimes/*/dependencies/python/python.exe` payload:

```bat
scripts\run.cmd ssh_execute.py lab-231 "uptime" --json
scripts\run.cmd doctor.py --host lab-231
```

```bash
scripts/run.sh ssh_execute.py lab-231 "uptime" --json      # POSIX equivalent
python scripts/selfcheck.py                                # if python exists
```

Hard requirements and what each buys you:

- `ssh` on `PATH` (OpenSSH client) — required by everything.
- `paramiko>=3.0,<4` — **optional**. It gives the SFTP backend (`--resume`,
  `--bwlimit`, `--preserve`, `--delete`, progress, `stream` mode; `--chmod` is
  upload-only) for `ssh_upload.py` / `ssh_download.py` /
  `ssh_server_transfer.py`. Without it those scripts fall back to `scp` /
  `tar` / `rsync` and say so in `data.transport` (`sftp | scp | tar | rsync |
  direct`). Install with `pip install 'paramiko>=3.0,<4'`. Ask
  `doctor.py` whether the fallback is enough for *your* task; `--resume`,
  `--bwlimit` and `--delete` are refused with exit 166 on it rather than
  silently ignored.
- `scp` (ships with OpenSSH) for the paramiko-free upload/download fallback.
- Password auth needs the password in an environment variable (default
  `SSH_PASSWORD`). No `ssh-copy-id`, no `sshpass`, no pty requirement —
  see the Windows row below.
- **A writable scratch directory for the `SSH_ASKPASS` helper.** Password auth
  stages a 0600 secret and a tiny helper program, and deletes both afterwards.
  The candidates are, in order: `$SSH_CORE_ASKPASS_DIR`,
  `$SSH_CORE_ASKPASS_HOME/ssh-core-askpass`, `$TMPDIR/ssh-core-askpass`,
  `~/.cache/ssh-core-askpass`, `$DSH_HOME/tmp/ssh-core-askpass`,
  `./.ssh-core-askpass`. If every one is denied you get
  `failure_class: "askpass_unavailable"` (exit 114) listing what was tried —
  set `SSH_CORE_ASKPASS_DIR` and retry. On Windows the directory is created
  normally and tightened with `icacls` instead of trusting `mkdir(mode=0o700)`,
  which produces a DACL that can lock the creating user out.
- **Windows**: everything works, including password auth and
  `deploy_pubkey --password`, because the password route answers OpenSSH's
  prompt with an `SSH_ASKPASS` helper (`SSH_ASKPASS_REQUIRE=force`) instead of
  needing a terminal. The single exception is `--control-master auto` (OpenSSH
  for Windows needs a POSIX socket for `ControlPath`) — use `--commands-file` or
  `ssh_daemon.py` instead, which give the same "one handshake, many commands"
  benefit.
- **Python 3.8+** — the marketplace holds this floor: `requires-python` in the
  repository `pyproject.toml`, a 3.8 CI leg, and a construct check that rejects
  3.9/3.10-only APIs such as `str.removeprefix`,
  `Path.write_text(newline=)` and `TemporaryDirectory(ignore_cleanup_errors=)`.
  The scripts avoid 3.9/3.10-only syntax so the skill still runs on older
  enterprise hosts (Kylin 3, CentOS 7 with EPEL). Python 3.7 is EOL since
  2023-06 and is **not** supported.

## When to use

Trigger this skill when the user asks to:

- Run a command on a remote host (`ssh prod-web-01 'systemctl status nginx'`)
- Configure a network device (Huawei VRP, H3C Comware, Cisco IOS) — see
  `ssh_session.py` below; this needs an interactive session, not exec
- Upload or download files (`scp ./deploy.tar prod:/srv/`)
- Open a tunnel to a remote service (`ssh -L 3306:localhost:3306 db-host`),
  expose a local port on the far side (`-R`), or run a SOCKS proxy (`-D`)
- Run the same command across a fleet (`for h in web-1 web-2 web-3; do ssh $h 'uptime'; done`)
- Move data between two remote machines without staging locally
- Manage `~/.ssh/config` entries (including rolling a change back)

Skip when:

- The work is purely local (no host alias, no user@, no IP)
- The user wants a formal request → review → execute approval flow. No such
  companion skill is published; if that is required, say so explicitly rather
  than implying `ssh-core` enforces it (its own guardrails are limited to
  `--delete` needing `--yes`, wildcard config edits needing `--allow-wildcard`,
  and strict host-key checking).

## Decision tree

```
Is the target a remote host?
├─ NO → wrong skill, stop
└─ YES → What's the action?
    ├─ Run a command
    │   ├─ Key auth (Linux/Unix host)   → ssh_execute.py <alias> "<cmd>"
    │   ├─ Password-only host           → SSH_PASSWORD=... ssh_execute.py <alias> "<cmd>"
    │   │                                 (auto-routes to the password transport when
    │   │                                  ~/.ssh/config says "# auth: password")
    │   ├─ Needs root                   → ssh_execute.py <alias> --sudo -- "<cmd>"
    │   │                                 # sudo -n, fails fast with sudo_required (112)
    │   │                                 # --sudo-user deploy  /  --sudo-password-env VAR
    │   ├─ Watch a log live             → ssh_execute.py <alias> --follow "journalctl -fu nginx"
    │   │                                 # --tail N starts from the last N lines; Ctrl-C
    │   │                                 # still yields a valid envelope (exit 130)
    │   ├─ Flaky link / restarting svc  → add --retry 3 --retry-backoff 2
    │   │                                 # only network/timeout are retried, never auth
    │   ├─ Several commands, one login  → ssh_execute.py <alias> --commands-file cmds.txt
    │   │                                 # per-command stdout, stderr AND exit code in
    │   │                                 # data.results[]; batch exit_code = first failure
    │   ├─ grep/diff/curl answers       → add --allow-exit 0,1
    │   │                                 # success=true, real exit_code kept,
    │   │                                 # data.exit_allowed=true (never for network/auth)
    │   ├─ Big output (>10 MB)          → add --output-file PATH
    │   │                                 # streams straight to disk; envelope carries
    │   │                                 # data.output_file {path, bytes, sha256}
    │   ├─ Complicated shell/quotes     → write it locally, then
    │   │                                 ssh_execute.py <alias> --script-file ./x.sh
    │   │                                 # piped into `bash -s`; no quoting left.
    │   │                                 # Defaults to LF+BOM normalisation (--keep-crlf)
    │   ├─ A window of a remote file    → ssh_slice.py <alias> /var/log/x --lines 100-200
    │   │                                 # --stat/--head/--tail/--bytes/--grep too;
    │   │                                 # the text comes back in data.content (and
    │   │                                 # stdout) under --json, with content_sha256 /
    │   │                                 # content_truncated; data.range.next_offset
    │   │                                 # resumes, --append writes
    │   ├─ Password host by bare IP     → add --user root
    │   │                                 # without it: user_required (115), not a fake auth
    │   ├─ Network-device CLI / paging / [Y/N]
    │   │                               → SSH_PASSWORD=... ssh_session.py <alias> \
    │   │                                   --disable-paging "screen-length 0 temporary" \
    │   │                                   --command "display version" --command ...
    │   ├─ Many calls to the same host  → ssh_daemon.py start <alias> ; ssh_daemon.py run <alias> "<cmd>"
    │   └─ Many hosts                   → ssh_cluster.py "<cmd>" --hosts a,b,c
    │                                      # or select from ~/.ssh/config metadata:
    │                                      --tags web,prod --environment production
    │                                      # (tags are AND-combined; concurrency
    │                                      #  is --max-workers — --parallel is a
    │                                      #  compatibility no-op)
    │                                      # health: --health-check
    │                                      #   --health-check-command 'systemctl is-active nginx'
    │                                      #   --probe tcp:22 --probe port:3306  (no ssh needed)
    │                                      # a skipped host (skipped_reason) never
    │                                      # fails the batch: all-ok-or-skipped is
    │                                      # exit 0; data.ok_hosts / failed_hosts /
    │                                      # skipped_hosts name the buckets
    │
    ├─ Move bytes
    │   ├─ Local → Remote → ssh_upload.py      [--transport auto|sftp|scp|openssh]
    │   │                     # default is REFUSE TO CLOBBER: an existing target
    │   │                     # fails with 168 remote_path_exists and is left
    │   │                     # untouched unless you pass --overwrite
    │   │                     # (--resume and --delete imply it; --recursive is
    │   │                     # exempt because it copies *into* a directory)
    │   ├─ Remote → Local → ssh_download.py    [--transport auto|sftp|scp|openssh]
    │   │                     # `openssh` is the scp backend: no paramiko needed,
    │   │                     # sha256-verified; --resume/--chmod/--bwlimit only
    │   │                     # exist on sftp and are refused (166) elsewhere
    │   │                     # --recursive lands the *contents* of the remote
    │   │                     # directory under the local path on every backend
    │   ├─ Keep timestamps → add --preserve (both directions: SFTP, or `scp -p`)
    │   ├─ Make the destination a mirror → add --recursive --delete --yes
    │   │                     # prunes extra entries only after a clean copy;
    │   │                     # needs the SFTP transport, refused (166) otherwise
    │   └─ Remote → Remote → ssh_server_transfer.py
    │                          --mode auto|direct|stream|hybrid   (all implemented)
    │                          # direct = tar/rsync between the two hosts (no paramiko)
    │                          # stream = SFTP through this host (paramiko)
    │                          # auto   = stream if paramiko, else direct
    │                          # data.bytes / data.files_transferred are measured
    │                          # in every mode, not estimated
    │                          # Unknown host key → --accept-new-hostkey (opt-in)
    │
    ├─ Forward a port → ssh_tunnel.py start <alias>
    │                     --type local   --remote-port 3306 --local-port 13306   # -L
    │                     --type remote  --remote-port 2222 --local-port 22      # -R
    │                                   --remote-host 127.0.0.1                 # target on this side
    │                     --type dynamic --local-port 1080                       # -D (SOCKS5)
    │                     # `--reverse` / `--dynamic` are aliases for --type
    │                     # --idle-timeout N stops a tunnel after N idle seconds (0 = never)
    │                     # Password-only host: --auth password (askpass helper)
    │
    ├─ Manage hosts → ssh_config_manager.py
    │                   create | update | delete | list | find | restore | validate
    │                   # inventory export: list --format ansible|yaml|json|ssh-config
    │
    └─ Review the trail → audit.py summarize --since 24h
                          # prune --older-than 30d / rotate --max-bytes 10MB / list

    Bootstrap pubkey on a new host → deploy_pubkey.py <alias>
                                       [--accept-new-hostkey]
                                       # password-only host, no terminal:
                                       #   SSH_PASSWORD=... deploy_pubkey.py <alias> \
                                       #     --password [--promote-auth]

    Old device, algorithm negotiation dies → add -o, one family per attempt.
        Each failure names only the first missing family, so work down:
        1 kex      -o KexAlgorithms=+diffie-hellman-group-exchange-sha1
        2 hostkey  -o HostKeyAlgorithms=+ssh-rsa
        3 cipher   -o Ciphers=+aes256-cbc,aes128-cbc
        4 mac      -o MACs=+hmac-sha1
        Accepted by ssh_execute.py, ssh_session.py, ssh_cluster.py,
        ssh_tunnel.py start and ssh_daemon.py start.
```

### Device tasks: which entry point

Every `ssh_session.py` row reads the password from `$SSH_PASSWORD`; export it
first (`SSH_PASSWORD='...'`). `ssh_execute.py` uses the same variable when the
alias is password auth.

| Task | Entry point | Why |
|---|---|---|
| Single read-only `display ...` | `ssh_session.py <alias> --disable-paging "screen-length 0 temporary" --command "display version"` | the CLI needs a pty, and paging must be off before the first long output |
| Batch read-only (several commands) | `ssh_session.py <alias> --command ... --command ...` | one login; view state and the paging setting survive across commands |
| Config write / `save` | `ssh_session.py <alias> ... --confirm yes` | the device asks `[Y/N]`; the default `--confirm ask` refuses to guess |
| Quick one-liner from a Linux host | `SSH_PASSWORD='...' ssh_execute.py <alias> "display clock"` on an alias whose config says `# device-type: network` | one command per login over a forced tty, for devices that stay up long enough to print the result and their prompt; no paging control, no view state |

When the two disagree about a device, believe `ssh_session.py`: it drives the
device CLI. A healthy device echoes prompt and result and only then disconnects —
`ssh_execute.py` reads that transcript as success (output plus its prompt, whatever
ssh's exit status says) — while a `Received disconnect` with no output before it
stays `failure_class: auth`. `ssh_session.py` is password-only by construction;
`ssh_execute.py` reads `# auth:` / `# device-type:` metadata under `--auth auto`.
Details: `references/troubleshooting.md` (cipher/MAC rows, and "a disconnect is
not an auth failure").

## Workflow (every operation)

1. **Discover.** If the user names a host you haven't seen, run `ssh_config_manager.py list` to confirm it exists in `~/.ssh/config`. If it doesn't, ask the user — do not invent connection details. (Ad-hoc `user@host`, `user@host:2222` and `--port`/`--identity` work for one-offs.)
2. **Choose transport.**
   - Key auth: native `ssh` with `BatchMode=yes` and `ConnectTimeout`. ProxyJump
     and ForwardAgent still come from `~/.ssh/config`. `--accept-new-hostkey`
     is honoured on the key route too.
   - Password / keyboard-interactive: set `SSH_PASSWORD` in the environment and either
     pass `--auth password` or mark the host `# auth: password` with
     `ssh_config_manager.py`. The password is never placed in argv, the JSON
     envelope, or the audit log. Missing `SSH_PASSWORD` yields
     `failure_class: "password_required"` (exit 105), not a misleading auth error.
     Transfers take the same switches (`ssh_upload` / `ssh_download` /
     `ssh_server_transfer`), so a password-only host can still move bytes without
     paramiko.
   - Network devices (VRP/Comware/IOS): use `ssh_session.py` for anything beyond a
     single read — an exec channel cannot disable paging, hold view state, or answer
     `[Y/N]`. (`ssh_execute.py` on an alias marked `# device-type: network` opens the
     same `-tt` shell for one command, reports `data.merged_stream: true`, and
     refuses to guess `[Y/N]`.) The "Device tasks: which entry point" table in the
     decision tree above is the short version.
     On that route the greeting/answer boundary is a *race* (both arrive in one
     read), so it is reported rather than guessed: `data.ssh_banner_applied` says
     whether the login greeting was cut, `data.ssh_banner_unusable_reason` says why
     it was kept when it was, `data.ssh_farewell_lines_removed` counts ssh's own
     trailer (whole line or glued to the device's last line), and
     `data.verdict_basis` lists the four inputs to the success verdict. The
     greeting is never cut at the price of the command's result — see
     `references/troubleshooting.md`, "When `stdout` looks different from run to
     run".
3. **Execute.** Pass the alias and the command. Read the JSON result. If `success` is false, read `stderr` and `data.failure_class` before retrying. Only retry when the class is `network` or `timeout` — `--retry N` does exactly that automatically, and it never repeats `auth`/`host_key` failures.
4. **Report.** Show the user the command, the host, the exit code, and the last few lines of stdout/stderr. Host identifiers are already redacted (`<alias>`, `<host>`, `<key>`); keep it that way — never print `IdentityFile` paths or keys. Human mode prints `ssh-core: exit_code=N` on stderr for failures even when the command produced no output.

A remote command's own exit status is passed through unchanged (`exit 42` →
`exit_code: 42`); ssh's own 255 is translated into the shared failure classes
(`auth` 100, `host_key` 101, `network` 102, `kex` 103, `remote_error` 104).
Two classifications are *not* 255 translations and are worth knowing:
`alias_not_found` (4) when ssh could not resolve a bare name — that is a typo
in `~/.ssh/config`, not a DNS outage — and `config_missing` (6) when the
`--ssh-config` path does not exist. `sudo_required` (112) comes from the
`sudo -n true` preflight, never from a command's exit status.

See `references/workflows.md` for the long form and `references/recipes.md` for
ready-made systemd / docker / kubernetes one-liners.

## Quick reference

```bash
# Run a command (key auth; BatchMode + ConnectTimeout)
python scripts/ssh_execute.py prod-web-01 "systemctl status nginx" --json

# Password-only Linux host (password from the environment, never argv)
SSH_PASSWORD='...' python scripts/ssh_execute.py legacy-box "uptime" --auth password --json

# Several commands over ONE connection (works on Windows too); per-command status
python scripts/ssh_execute.py prod-web-01 --commands-file /tmp/cmds.txt --json

# Ad-hoc host, no ~/.ssh/config entry needed
python scripts/ssh_execute.py 'root@192.168.30.240:2222' "uptime" --json

# A command that starts with a dash
python scripts/ssh_execute.py lab-240 -- "-v" --json

# Need root? Non-interactive sudo with a preflight, so it can never hang
python scripts/ssh_execute.py prod-web-01 --sudo -- "systemctl restart nginx" --json
python scripts/ssh_execute.py prod-web-01 --sudo-user deploy -- "id -u" --json
# no NOPASSWD rule? opt into stdin password (never argv, never the envelope)
SSH_SUDO_PASSWORD='...' python scripts/ssh_execute.py prod-web-01 --sudo \
    --sudo-password-env SSH_SUDO_PASSWORD -- "id -u" --json

# Watch a log live (Ctrl-C still returns a valid envelope, exit 130)
python scripts/ssh_execute.py prod-web-01 --follow "journalctl -fu nginx --no-pager" --json
# --tail wraps the command in `stdbuf -oL ... | tail -n 50 -f`, so a slow log
# appears immediately; do not combine it with a command that is already
# `tail -f` (the inner tail never flushes through a pipe)
python scripts/ssh_execute.py prod-web-01 --follow --tail 50 "tail -f /var/log/nginx/error.log"

# Transient noise (sshd restarting, flaky link): retry network/timeout only
python scripts/ssh_execute.py prod-web-01 "uptime" --retry 3 --retry-backoff 2 --json

# Keep one session open and reuse it (≈3x faster per call than a fresh handshake)
python scripts/ssh_daemon.py start lab-240 --idle-timeout 600
python scripts/ssh_daemon.py run   lab-240 "systemctl is-active nginx" --json

# Opt into OpenSSH ControlMaster reuse (POSIX only; refused on Windows)
python scripts/ssh_execute.py prod-web-01 "uptime" --control-master auto --json

# Interactive network-device session: paging off, several commands, save answered
# (each command's transcript comes back in data.results[].output;
#  --no-capture-output keeps only the byte counts)
SSH_PASSWORD='...' python scripts/ssh_session.py sw-core-01 \
    --disable-paging "screen-length 0 temporary" \
    --command "system-view" --command "vlan batch 50" \
    --command "save" --confirm yes --json

# Old device whose algorithms OpenSSH 8.8+ no longer offers: all four families.
# Add them one at a time (kex → hostkey → cipher → mac): the device names only
# the family that failed, so a missing third or fourth group is a separate run.
# Key auth as shown; for a password-only device prepend SSH_PASSWORD='...' and
# add --auth password (plus --user USER when the alias has no User block).
python scripts/ssh_execute.py old-switch "display clock" \
    -o KexAlgorithms=+diffie-hellman-group-exchange-sha1 \
    -o HostKeyAlgorithms=+ssh-rsa \
    -o Ciphers=+aes256-cbc,aes128-cbc \
    -o MACs=+hmac-sha1
# Same device driven interactively (paging off, several commands, one login)
SSH_PASSWORD='...' python scripts/ssh_session.py old-switch \
    --disable-paging "screen-length 0 temporary" --command "display version" \
    -o KexAlgorithms=+diffie-hellman-group-exchange-sha1 \
    -o HostKeyAlgorithms=+ssh-rsa \
    -o Ciphers=+aes256-cbc,aes128-cbc \
    -o MACs=+hmac-sha1

# Upload / download (SFTP with paramiko, scp/tar without it — data.transport says which)
python scripts/ssh_upload.py prod-web-01 ./release.tar.gz /srv/releases/ --resume
python scripts/ssh_download.py prod-web-01 /var/log/nginx/error.log ./error.log
# keep mtime + permissions (cp -p semantics); `--preserve` maps to `scp -p`
# without paramiko and data.preserved says whether it worked
python scripts/ssh_upload.py prod-web-01 ./release.tar.gz /srv/releases/ --preserve
# MIRROR: prune remote entries that no longer exist locally (needs --yes AND
# paramiko — scp/tar cannot prune, so the call is refused with 166 instead of
# pretending it mirrored anything)
python scripts/ssh_upload.py prod-web-01 ./public/ /srv/www/ --recursive --delete --yes --json

# Server-to-server transfer (snapshot prod → backup, no local staging, no paramiko)
python scripts/ssh_server_transfer.py prod-db /var/backup/db.dump backup-host /restore/ \
    --mode direct --json
# incremental re-sync instead of a full copy (rsync runs ON the source host:
# both endpoints are remote, so rsync cannot be started from here)
python scripts/ssh_server_transfer.py prod-db /var/backup/ backup-host /restore/ \
    --mode direct --use-rsync --recursive --json

# Broadcast to a fleet selected by ~/.ssh/config metadata (AND across tags)
python scripts/ssh_cluster.py "uptime" --tags vpp,ipsec --environment production --max-workers 8
# or an explicit list (v1.0 form, still supported)
python scripts/ssh_cluster.py "uptime" --hosts web-1,web-2,web-3 --max-workers 8
# --ssh-config is used BOTH to pick hosts and to connect (forwarded as ssh -F),
# so the host that is selected is the host that gets contacted.
python scripts/ssh_cluster.py "uptime" --tags web --ssh-config ./test-network-config
# Same bytes on every host: each worker opens its own handle (the caller's
# stdin is never shared — that used to be a race that reported all-green).
python scripts/ssh_cluster.py "bash -s" --hosts web-1,web-2 --stdin-file ./setup.sh

# Health check that can actually fail: a real command, plus transport probes
python scripts/ssh_cluster.py "systemctl restart nginx" --hosts web-1,web-2 \
    --health-check --health-check-command "systemctl is-active nginx" \
    --probe tcp:22 --probe port:3306 --retry 2 --json
# "which host failed most in the last day?"
python scripts/audit.py summarize --since 24h

# Tunnels: local (-L), remote (-R) and SOCKS (-D)
python scripts/ssh_tunnel.py start prod-db --remote-port 3306 --local-port 13306
# -R: bastion listens on 2222, forwards back to this host's 127.0.0.1:22
python scripts/ssh_tunnel.py start bastion --type remote --remote-port 2222 \
    --local-port 22 --remote-host 127.0.0.1
python scripts/ssh_tunnel.py start bastion --type dynamic --local-port 1080 --idle-timeout 0

# Tunnel to a password-only host (detached ssh has no tty, so the password
# is answered through a 0600 SSH_ASKPASS helper — never argv, never env)
SSH_PASSWORD='...' python scripts/ssh_tunnel.py start legacy-db \
    --remote-port 3306 --local-port 13306 --auth password --accept-new-hostkey

# Inspect / edit hosts. An empty value deletes a field; every write is locked,
# validated with `ssh -G`, backed up under a unique name, and reversible.
python scripts/ssh_config_manager.py list --environment production
python scripts/ssh_config_manager.py create --alias prod-web-02 --host 10.0.1.12 --user deploy --key ~/.ssh/id_ed25519 --environment production --tags web,nginx
python scripts/ssh_config_manager.py update --alias prod-web-02 --user ''   # delete User
python scripts/ssh_config_manager.py update --alias prod-web-02 --port 2222 --dry-run --diff
python scripts/ssh_config_manager.py restore --list-backups
python scripts/ssh_config_manager.py restore --from latest --diff
python scripts/ssh_config_manager.py validate

# Inventory export: feed Ansible, a YAML pipeline, or another ssh_config consumer
python scripts/ssh_config_manager.py list --format ansible > inventory.json
python scripts/ssh_config_manager.py list --format yaml
python scripts/ssh_config_manager.py list --format ssh-config > clean_config

# Bootstrap your pubkey on a fresh host (no ssh-copy-id needed, works on Windows)
python scripts/deploy_pubkey.py prod-web-02 --accept-new-hostkey
SSH_PASSWORD='...' python scripts/deploy_pubkey.py legacy-box \
    --password --promote-auth --accept-new-hostkey --json

# "Can this machine run the skill at all?" — one dependency-free command
python scripts/selfcheck.py
```

## Storage model

Single source of truth: **`~/.ssh/config`**. We use OpenSSH-standard `Host` blocks plus comment-line metadata so native `ssh` still works without us:

```sshconfig
# ===== prod-web-01 =====
# description: 生产环境 Web 服务器
# environment: production
# tags: web,nginx,production
# location: aliyun-beijing
# auth: key
# device-type: server
Host prod-web-01
    HostName 10.0.1.11
    User deploy
    IdentityFile ~/.ssh/id_ed25519
    ProxyJump bastion
```

```sshconfig
# ===== sw-core-01 =====
# environment: production
# tags: switch,access
# auth: password
# device-type: network
Host sw-core-01
    HostName 10.0.0.3
    User admin
```

Metadata keys are `description`, `environment`, `tags`, `location`, `auth`
(`key` | `password` | `interactive`), `device-type` (`server` | `network`).
They are consumed by `ssh_config_manager.py`, `ssh_cluster.py`
(`--tags` / `--environment`) and `ssh_execute.py` (`--auth auto`). Native
OpenSSH ignores comments, so `ssh prod-web-01` from any terminal still works.

`Host` matching follows OpenSSH: case-insensitive and glob-aware. Aliases are
compared with `casefold()`, so `WIN-PROD-01` cannot become a second entry next
to `win-prod-01`; wildcard blocks (`Host *`, `Host web-*`) are shown by
`list --include-wildcards` but refuse `create`/`update`/`delete` unless
`--allow-wildcard` is passed.

No proprietary database. No lock-in.

## Daemon model

**Default: no daemon.** Each `ssh_execute` call is a normal `ssh` subprocess
with `BatchMode=yes` — key auth. No custom socket protocol. With no extra
flags, per-call cost is one SSH handshake (~200-300 ms).

**Option 1 — batch:** `ssh_execute.py --commands-file cmds.txt` runs N
commands inside one connection (`sh -s` + a marked script) and reports each
command's own `stdout`, `stderr` and exit status in `data.results[]`. Each
command runs as `( cmd ) < /dev/null`, so a stdin reader cannot eat the rest of
the script and a stray `exit` only ends its own subshell; a batch that ends
early reports `data.batch_incomplete` + `data.missing_indexes` and
`success: false`. Its top-level `exit_code` is the first failing command's own
status — the same number the single-command route returns — and `104` only
means "the batch itself did not finish or could not be parsed". Portable, no
state, no sockets. Preferred for anything scripted.

**Option 2 — daemon:** `ssh_daemon.py start <alias>` keeps one `ssh -T sh`
child alive and answers `run` requests over a loopback socket protected by a
0600 bearer token, exiting after `--idle-timeout` (floor 30 s). Measured 5 calls
≈ 0.68 s vs ≈ 1.53 s with a fresh handshake each time. `run` returns the
command's **stdout and stderr separately**, and a command that kills the
session reports `daemon_not_running` instead of a bare timeout.

**Option 3 — ControlMaster:** `ssh_execute.py --control-master auto` enables
OpenSSH ControlMaster reuse additively (hashed `ControlPath` under a private
temp dir, `ControlPersist`). POSIX only — on Windows it refuses with
`dependency_missing` and points at options 1/2.

**Password transport:** the password never needs a terminal. `SSH_ASKPASS` +
`SSH_ASKPASS_REQUIRE=force` answers the prompt, so the command channel stays a
normal **exec** channel — no `-tt` — and every output guarantee below applies
unchanged (byte-exact stdout/stderr, real remote exit codes, banner filtering,
`124` on timeout, truncation metadata, live human-mode relay). Only an alias
marked `# device-type: network` gets a real `-tt`/pty session, because a
VRP/Comware/IOS CLI needs a tty to hold view state; that route reports
`data.merged_stream: true` and keeps `data.raw_stdout`, since a merged stream
cannot be split after the fact.

## Output handling

- `ssh_execute` streams stdout/stderr into a bounded buffer: **10 MB per stream, counted in bytes**. Bytes past the cap are dropped, a `... [truncated, N bytes elided]` marker is appended *outside* the cap, and `data.truncated` / `data.bytes_elided` / `data.bytes_captured` report it on **every** exit path (success, non-zero, timeout). The child is not killed early, so remote behavior is unchanged. `ssh_session` and the daemon share the same 10 MB cap and report the same two keys.
- The pipes are **binary**: output is decoded once, as UTF-8 with `errors="replace"`. A binary payload (`head -c 200 /dev/urandom`, a GBK file name) can no longer empty `stdout`, and `\r\n` / a lone `\r` reach you unchanged. `data.decode_replaced > 0` and `data.lossy_output: true` say that some bytes were not valid UTF-8 (SSHCORE-015/019).
- `stdout_bytes_captured` is a **byte** count, not a character count, and `--timeout` / `--connect-timeout` below 1 are refused with `failure_class: "invalid_option"` (exit 107) instead of pretending to time out (SSHCORE-021).
- sshd's login banner (`Banner /etc/issue.net`) is peeled off `stderr` and reported as `data.ssh_banner_lines`, so `stderr == ""` really means "the command wrote nothing". A real diagnostic is never removed; the client keeps its default log level for that reason — `-o LogLevel=ERROR` was measured to drop `Connection refused` (9.x) and `Unable to negotiate ...` (all versions), which would undo the `network`/`kex` classification (SSHCORE-017). On the merged `-tt` device route the same greeting is cut from `stdout` only when the remainder still proves the command ran; when it cannot be cut safely the envelope says `data.ssh_banner_applied: false` plus a reason, and `data.raw_stdout` keeps every byte.
- On timeout the **local** `ssh` client is killed and everything already captured is returned: `failure_class: "timeout"`, `data.partial_output: true`, exit 124. The **remote** command is not necessarily terminated — `ssh` dies without sending `SSH_MSG_CHANNEL_CLOSE`, so a long `grep`/`mysqldump` can keep running on the target. On the `local` route the real command is killed. Wrap the command in `timeout N` yourself when the target must stop (SSHCORE-020).
- A child killed by a signal is reported as `128 + N` (`143` for SIGTERM) with `data.terminated_by_signal: 15`, matching every shell, instead of a negative number (SSHCORE-023).
- Human mode (no `--json`) relays the command's bytes to the terminal **while it runs**, keeping stdout/stderr interleaved in the order they were produced, and writes exactly what the command wrote (no added trailing newline). A failure still appends `ssh-core: exit_code=N` (plus `failure_class` when the command produced no output at all) to stderr (SSHCORE-025).
- `--json` works before *or* after the verb, and **argument errors also produce the envelope** (`failure_class: "invalid_arguments"`, exit 2) so one parser handles every case.
- `--version` / `-V` is available on every script (also with `--json`).
- `stdout`/`stderr`/`data.raw_stderr`/`data.ssh_argv` are redacted: `user@host`, `HostName`, `Port` and `IdentityFile` become `<alias>` / `<host>` / `<port>` / `<key>`. A name alias such as `lab-240` stays readable; an **address literal** used as the host (`192.168.30.240`) becomes `<host>` everywhere, argv included. `--no-redact` disables that for debugging.
- `ssh_cluster` never inherits your stdin: N concurrent workers on one descriptor meant one host read the payload and the rest got EOF while all of them reported success. Feed every host the same bytes with `--stdin-file PATH` (each worker opens its own handle); without it, stdin is an empty closed pipe (SSHCORE-024).
- A password host gets all of the above: the exec channel is the same one the key route uses (the prompt is answered by `SSH_ASKPASS`, not by a pty), so `exit 3` really is 3, `\r\n` stays `\r\n`, `--follow` streams and `--timeout` returns 124. `--device-type: network` is the only case that switches to `-tt`, and it says so in `data.merged_stream`.

## Audit trail

- Every script can write JSONL records to
  `$TMPDIR/ssh-core/<md5(alias)>.jsonl` (directory `0700`, file `0600`,
  rotated at **10 MB** keeping **3** generations; override with
  `SSH_CORE_AUDIT_MAX_BYTES` / `SSH_CORE_AUDIT_KEEP`): `ssh_session.py`
  defaults to on, the rest take `--audit-log`, and `SSH_CORE_AUDIT=on|off`
  flips the default for the whole skill. Records hold ts/host/route/command/
  exit_code/failure_class/elapsed_s/bytes — never the password. The `session`
  route is the one exception and carries ts/host/route/command/commands/
  exit_ok/elapsed_s/session (there is no per-command exit code to report).
- `audit.py` reads that trail back: `summarize --since 24h` (calls, failures,
  failure classes, slowest command, last error, worst host), `prune
  --older-than 30d`, `rotate --max-bytes 10MB`, `list`. That is how you answer
  "which host failed most today" without a throwaway `jq` pipeline.
- `--snapshot-cmd <cmd>` on `ssh_session.py` captures the command's output
  before and after the session and returns `data.config_snapshot:
  {"before": path, "after": path}`.
- `ssh_tunnel.py` audits `start`/`stop` (and the idle watchdog writes
  `idle_stop` when `SSH_CORE_AUDIT=on`).

## Safety baseline

- All scripts use `subprocess.run/Popen(argv_list, ...)` — never shell strings. The `local` route refuses `&` `&&` `|` `||` `>` `<` `;` `` ` `` `$(` `^` `%VAR%` `!VAR!`; `bash -c '...'` is the explicit escape hatch.
- The `local` route runs **without a shell**, so shell builtins need an explicit wrapper. On Windows that is `cmd /c ...` (for `echo`, `dir`, `copy`, `set`, …) or `powershell -Command ...` (for cmdlets); both are recognised and name themselves in `data.shell`. `--shell cmd|powershell|sh|none` declares the shell once instead of inline (default `none`). A wrapper that carries a metacharacter is still refused, and `--shell` is the only way to opt out — never an inline guess.
- A local command whose binary does not exist reports the binary's name, and on Windows whether it is a `cmd.exe` builtin, plus the working alternative (`cannot execute 'echo' … use 'cmd /c echo hello'`), instead of a bare `[WinError 2]`.
- All file-transfer commands prefix `MSYS_NO_PATHCONV=1` on Windows
- stdout/stderr capped at 10 MB per stream with an explicit truncation marker
- Credentials never appear in argv — passwords come from an environment variable, never a flag. For detached tunnels the secret is written to a 0600 `SSH_ASKPASS` helper that is deleted as soon as the forward settles. `--sudo-password-env` follows the same rule: the sudo password travels over stdin, never argv.
- Privilege escalation is non-interactive by construction: `sudo -n` with a `sudo -n true` preflight, so a missing NOPASSWD rule fails fast (`sudo_required`, 112) instead of hanging on a prompt that has no terminal.
- `--delete` mirror mode is denied without `--yes`, requires `--recursive`, refuses to prune when the copy itself recorded failures, and is refused outright (`mode_deferred`, 166) when the active transport cannot prune.
- Host keys: strict everywhere (`StrictHostKeyChecking=yes`, paramiko `RejectPolicy`). Unknown hosts require an explicit `--accept-new-hostkey` (one-shot, persisted, still fails on a changed key) — on the exec, tunnel, scp and tar routes alike.
- `-o` passthrough is validated on **every** script, including `ssh_daemon.py`: `ProxyCommand` / `LocalCommand` / `PermitLocalCommand` / `Match` / `HostName` / `KnownHostsCommand` / `RemoteCommand` / `IdentityAgent` are refused with `failure_class: "invalid_option"` (107)
- `~/.ssh/config` writes are locked (cross-process), re-read under the lock, validated with `ssh -F <tmp> -G <alias>`, and arrive with a unique `.bak` that `restore` can roll back. Alias lookups on the write path are **exact** (case-insensitive): a `Host *` block can never capture `create`/`update`/`delete`.
- See `references/safety.md` for the full list

## When something goes wrong

`references/troubleshooting.md` covers the common failures: `Permission denied (publickey)`, `Permission denied (keyboard-interactive,password)` (→ password auth), host-key drift, the four algorithm-negotiation messages an old device produces in turn (`no matching key exchange method` / `host key type` / `cipher` / `MAC`, → `-o`), a device that echoes its output and then disconnects (not an auth failure), `ProxyJump` chain broken, `IdentityFile` permissions, Windows path mangling, `deploy_pubkey` verification failures, transfer integrity mismatches, `sudo_required`, and `--follow` that never returns. Read it before guessing; `references/recipes.md` has copy-paste diagnostic commands for systemd, docker and Kubernetes.

Exit codes and failure classes are one table (`scripts/lib/exit_codes.py`).
The complete, generated list — every class, its code and its aliases, plus the
full per-script option tables — is
[`references/cli-reference.md`](references/cli-reference.md). The short version:
0 ok, 1 internal, 2 argv, 3 dependency, 4 not found, 5 exists, 6 config missing,
7 parse, 8 permission, 9 verify failed, 124 timeout, 100-115 exec/session,
120-129 tunnel, 140-142 deploy, 160-168 transfer, 180-181 config, 200-202 daemon,
220-221 audit. `255` is OpenSSH's own client-failure code and is passed through
unchanged, not re-mapped.

## Development

This directory is the **installed bundle**: `SKILL.md`, `references/` and
`scripts/`. It is self-contained on purpose — there is no checkout here, so
`make`, `tools/` and `tests/` are not present and are not required to use or
verify the skill.

```bash
python scripts/selfcheck.py                 # which tools exist here
python scripts/doctor.py                    # which capabilities actually work
python scripts/doctor.py --host <alias>     # + real login / transfer probes
python scripts/ssh_execute.py --version     # every script answers this
```

Every script runs on Python 3.8+ and imports only the standard library plus
`scripts/lib/` (and, on the SFTP path, the optional `paramiko` extra). `paramiko`
is installed with `pip install 'paramiko>=3.0,<4'`; the transfer scripts degrade
to scp/tar/rsync without it, and say so in `data.transport`. To change
behaviour, edit the script and re-run `python scripts/doctor.py`; to prove the
change against a host, use the `doctor.py --require ...` gate (exit 9 when a
capability regressed).
