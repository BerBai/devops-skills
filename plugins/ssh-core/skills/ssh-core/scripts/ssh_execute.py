#!/usr/bin/env python3
"""ssh_execute.py — run a command on a remote host (v1.4.0).

Public surface::

    python ssh_execute.py <host> [<command>] \
        [--commands-file PATH] [--stdin-text TEXT] \
        [--timeout N] [--connect-timeout N] [--ssh-config PATH] \
        [--control-master off|auto] [--control-persist N] \
        [--control-path-dir PATH] [-o KEY=VALUE ...] \
        [--auth auto|key|password] [--password-env VAR] \
        [--accept-new-hostkey] [--audit-log|--no-audit] \
        [--port N] [--identity PATH] \
        [--sudo] [--sudo-user USER] [--sudo-password-env VAR] \
        [--follow] [--tail N] \
        [--retry N] [--retry-delay S] [--retry-backoff F] \
        [--redact|--no-redact] [--json]

Privilege escalation (DV-14)
----------------------------
``--sudo`` wraps the command as ``sudo -n [-u USER] -- sh -c '<cmd>'`` and
preflights it with ``sudo -n true``, so a missing NOPASSWD rule is reported as
``failure_class: "sudo_required"`` (exit 112) *before* the real command runs —
never as a mysterious failure of the command itself. A preflight that fails for
another reason (unreachable host, rejected key, unresolved alias) keeps its
original class and says ``data.sudo_preflight: "unreachable"`` (FA-15). ``-n``
is what keeps a ttys-less exec channel from hanging forever.
``--sudo-password-env VAR`` is the opt-in alternative (``sudo -S`` with the
password on stdin, never in argv); it cannot be combined with a command source
that already owns stdin. The target user comes from ``--sudo-user``, not from
``--sudo=USER`` — the flag is a store_true (FA-41).

Log following (DV-18)
---------------------
``--follow`` keeps the connection open and echoes output as it arrives until
Ctrl-C, the child exits, or ``--timeout`` expires. Because a live stream cannot
also be a single JSON document, the stream goes to **stderr** when ``--json``
is set and to stdout otherwise; the final envelope (with ``data.follow: true``,
``data.lines`` and the bounded transcript) is written to stdout afterwards, so
``... --follow --json | jq`` still parses on Ctrl-C.

Retry (DV-17)
-------------
``--retry N`` repeats an attempt only when ``failure_class`` is ``network`` or
``timeout``. Auth and host-key failures are never retried — repeating a
rejected credential is how accounts get locked. ``data.attempts`` records how
many tries actually happened.

Local route (``host == "local"``)
---------------------------------
The command is tokenised with :func:`lib.split_command` and executed **without
a shell**. Every shell metacharacter that would otherwise be interpreted by
the platform's implicit shell is refused, and that set now includes the single
``&`` (SSH-01: ``cmd /c echo a & copy /y nul proof`` used to run *two*
commands while reporting ``refused_metachar: null``), plus ``^`` (cmd.exe
escape / line-continuation) and ``%VAR%`` (cmd.exe delayed expansion):

    &  &&  |  ||  >  >>  <  ;  `  $(  ^  %NAME%

The documented escape hatch is unchanged: start the command with a shell
wrapper (``bash -c '...'``, ``sh``/``zsh``/``dash``/``ksh``, absolute paths and
``.exe`` suffixes allowed) and the check is skipped.

Remote route
------------
Native ``ssh`` via ``subprocess``::

    ssh -o BatchMode=yes -o ConnectTimeout=N [-F config] [-o KEY=VALUE] \
        [-p PORT] [-i IDENTITY] <host> <command>

``--accept-new-hostkey`` applies to the key route as well (SSH-06).

Auth routing
------------
* ``--auth key`` — ``BatchMode=yes``.
* ``--auth password`` — on POSIX a real pty (:mod:`lib.session`); on Windows a
  ``ssh -tt`` pipe driven by ``SSH_ASKPASS`` (:mod:`lib.askpass`). The password
  never enters argv, the environment of the child, the envelope or the audit
  log (SSH-08).
* ``--auth auto`` — reads the ``# auth:`` / ``# device-type:`` metadata.

Batch mode (SSH-12)
-------------------
``--commands-file`` / repeated ``--command`` runs N commands over *one*
connection (``sh -s`` + a here-document), which is the Windows-safe
replacement for ``ControlMaster``: 20 commands cost one handshake instead of
20. Each command's own output and exit status is reported.

Redaction (SSH-07)
------------------
``safety.md`` rule 3 is enforced: ``HostName``, ``User``, ``Port`` and
``IdentityFile`` are replaced with ``<host>`` / ``<user>`` / ``<port>`` /
``<key>`` in ``stderr``, ``data.raw_stderr`` and ``data.ssh_argv`` before the
envelope leaves the process (``--no-redact`` opts out for debugging).

stdout/stderr are captured from **binary** pipes into a bounded byte buffer
(10 MB per stream) and decoded once, as UTF-8 with ``errors="replace"``, so a
binary payload can never empty the stream and ``\r\n`` survives untouched
(SSHCORE-015/019). A timeout keeps the partial output and says in the envelope
that only the local client died (SSHCORE-020). ``exit_code`` is normalised to
0-255 (+124) with ``128+N`` for a signal death, so Windows' ``4294967295`` and
POSIX's ``-15`` can never leak into the contract (SSHCORE-023).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    CHILD_TEXT_KWARGS,
    BoundedCapture,
    alias_matches,
    append_audit,
    audit_cli_flags,
    audit_enabled,
    classify_failure,
    emit,
    filter_ssh_noise,
    is_windows,
    json_result,
    merged_ssh_config,
    msys_safe_env,
    normalized_exit_code,
    read_ssh_config_metadata,
    split_command,
    split_login_banner,
    ssh_config_path_problem,
    validate_ssh_option,
)
from lib.askpass import AskpassUnavailable, askpass_env  # noqa: E402
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_CONFIG_MISSING,
    EXIT_DEPENDENCY_MISSING,
    EXIT_HOST_KEY,
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_INVALID_OPTION,
    EXIT_KEX,
    EXIT_NETWORK,
    EXIT_NO_TTY,
    EXIT_OK,
    EXIT_PASSWORD_REQUIRED,
    EXIT_REMOTE_ERROR,
    EXIT_SUDO_REQUIRED,
    EXIT_TIMEOUT,
    EXIT_USER_REQUIRED,
    exit_code_for,
)
from lib.redact import redactor_for  # noqa: E402
from lib.retry import cli_flags as retry_cli_flags  # noqa: E402
from lib.retry import policy_from_args, retrying  # noqa: E402
from lib.session import closed_by_device, make_session  # noqa: E402
from lib.sudo import SudoSpec, classify as classify_sudo  # noqa: E402
from lib.sudo import parse_sudo, probe_command, remediation, wrap as sudo_wrap  # noqa: E402

# Shell metacharacters the `local` route refuses. Order matters for
# diagnostics: longer compound operators first so the error message names
# "&&" rather than "&". A lone "&" is a command separator in cmd.exe AND in
# POSIX shells, so it must be in the list (SSH-01).
SHELL_METACHARS: tuple[str, ...] = (
    "&&", "||", "$(", "|", ">>", ">", "<", ";", "`", "&", "^",
)
# `%NAME%` expansion is cmd.exe-only but harmless to refuse everywhere.
CMD_PERCENT_RE = r"%[A-Za-z_][A-Za-z0-9_]*%"
# `!NAME!` is cmd.exe delayed expansion (enabled with `cmd /v:on`). Refused as a
# *pattern*, exactly like `%NAME%`: a bare `!` is ordinary text in many commands
# (`echo done!`, `grep -v '!#'`) and refusing it would be a false positive, while
# `!PATH!` really does expand to a local path.
CMD_BANG_RE = r"![A-Za-z_][A-Za-z0-9_]*!"
# Shells whose `-c` form is the documented escape hatch (SSHCORE-008).
# `.exe` is normalised away so `"C:\Program Files\Git\usr\bin\bash.exe" -c ...`
# is recognised on Windows (SSH-26).
SHELL_WRAPPERS: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "dash", "ksh", "fish", "busybox"}
)
CONTROL_MASTER_DEFAULT = "off"
CONTROL_PERSIST_DEFAULT = 600
CONTROL_PATH_DIR_NAME = "ssh_controlmaster"
CONTROL_PATH_DIGEST_LEN = 24

# Exit codes used by this script come from lib.exit_codes (SSH-19).
EXIT_OK_ = EXIT_OK
EXIT_ARGS = EXIT_INVALID_ARGUMENTS
EXIT_FAIL = EXIT_INTERNAL
EXIT_DEPENDENCY = EXIT_DEPENDENCY_MISSING
EXIT_NO_COMMAND = 127  # shell convention: command not found

#: ``failure_class`` → exit code lives in one table (FA-30).
#:
#: A local copy used to exist here and had already drifted (it lacked
#: ``internal``, ``sudo_auth_failed`` and ``alias_not_found``), so
#: ``_CLASS_CODES.get(fclass, int(rc))`` could leak a raw 255. Everything now
#: goes through :func:`exit_code_for`, and the few names below are kept only as
#: documentation of which classes this script can emit.
_CLASS_CODES = {
    name: exit_code_for(name)
    for name in (
        "auth",
        "host_key",
        "kex",
        "network",
        "alias_not_found",
        "remote_error",
        "password_required",
        "dependency_missing",
        "invalid_option",
        "timeout",
        "no_tty_for_password",
        "sudo_required",
        "sudo_auth_failed",
        "internal",
    )
}


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_execute.py",
        description=(
            "Run a command on a remote host (or 'local'). Emits the shared "
            "JSON contract with --json. Local commands run without a shell; "
            "use `bash -c '...'` as the explicit escape hatch."
        ),
    )
    p.add_argument(
        "host",
        help=(
            "Host alias from ~/.ssh/config, user@host, user@host:port, or the "
            "literal 'local'"
        ),
    )
    p.add_argument(
        "command",
        nargs="?",
        default=None,
        help=(
            "Command to run on the host (argv string). Omit it when using "
            "--commands-file/--stdin-text/--script-file, or to open a login "
            "shell's stdin. Prefix with '--' when the command starts with '-'."
        ),
    )
    p.add_argument(
        "--user", "-l", default=None, metavar="USER",
        help=(
            "Remote login user. Without it, a bare host/IP whose config block "
            "has no `User` is rejected for password auth (failure_class="
            "user_required, exit 115) instead of silently logging in as the "
            "local Windows account (review B1)"
        ),
    )
    p.add_argument(
        "--command",
        action="append",
        dest="commands",
        default=[],
        help=(
            "Command to run in a batch over ONE connection; repeatable "
            "(SSH-12: the Windows-safe replacement for ControlMaster)"
        ),
    )
    p.add_argument(
        "--commands-file",
        default=None,
        help="File with one command per line ('#' comments and blanks skipped)",
    )
    p.add_argument(
        "--stdin-text",
        default=None,
        help="Text piped to the remote command's stdin (single connection)",
    )
    p.add_argument(
        "--stdin-file",
        default=None,
        metavar="PATH",
        help=(
            "File whose bytes are piped to the remote command's stdin; unlike "
            "--stdin-text the content never enters argv (or the audit log). "
            "ssh_cluster forwards this per worker (SSHCORE-024). "
            "Bytes are passed through verbatim; add --normalize-lf for a file "
            "produced by PowerShell (BOM + CRLF) (review D4)"
        ),
    )
    p.add_argument(
        "--script-file",
        default=None,
        metavar="PATH",
        help=(
            "Run a local script ON THE REMOTE HOST as `bash -s` (the command "
            "itself is then optional). This sidesteps the three-level quoting "
            "problem entirely: write the script locally, pass it here, and "
            "never fight `$`, quotes or awk programs through pwsh -> ssh -> sh "
            "(review D3). Implies --normalize-lf unless you pass --keep-crlf"
        ),
    )
    p.add_argument(
        "--normalize-lf", action="store_true",
        help=(
            "Strip a UTF-8/UTF-16 BOM and convert CRLF (and lone CR) to LF in "
            "--stdin-file / --script-file. Off by default: the transport is "
            "byte-exact, and PowerShell's Set-Content/Out-File output would "
            "otherwise reach bash as '\\ufeffhostname: command not found' "
            "(review D4)"
        ),
    )
    p.add_argument(
        "--keep-crlf", action="store_true",
        help=(
            "Do not normalise line endings even for --script-file (which "
            "normalises by default). Use when the payload is not a shell script"
        ),
    )
    p.add_argument(
        "--output-file", default=None, metavar="PATH",
        help=(
            "Write the command's stdout to PATH in full, bypassing the 10 MB "
            "in-envelope cap. The envelope then carries only path/bytes/sha256/"
            "truncation flags (data.output_file) instead of the transcript. "
            "This is the answer to 'journalctl dumped 235 MB and the envelope "
            "kept 10 MB' (review D1)"
        ),
    )
    p.add_argument(
        "--allow-exit", "--expect-exit", dest="allow_exit", default=None,
        metavar="CODES",
        help=(
            "Comma-separated exit codes that count as success, e.g. `0,1` for "
            "grep/diff or `0,2` for curl. A command exiting with an allowed "
            "code reports success=true, data.exit_allowed=true and keeps the "
            "real exit_code (review C1 — the single most requested flag)"
        ),
    )
    p.add_argument(
        "--redact-host", dest="redact_host", action="store_true", default=True,
        help=(
            "Hide the host identity in the envelope (default). data.host_id "
            "stays available either way (review C4)"
        ),
    )
    p.add_argument(
        "--no-redact-host", dest="redact_host", action="store_false",
        help="Keep the raw host string in data.host_id (still not the HostName)",
    )
    p.add_argument(
        "--timeout", type=int, default=120,
        help="Total wall-clock budget in seconds (default: 120)",
    )
    p.add_argument(
        "--connect-timeout", type=int, default=8,
        help="SSH connect timeout, applied as -o ConnectTimeout (default: 8)",
    )
    p.add_argument(
        "--ssh-config", default=None,
        help="Pass -F <path> to ssh (default: ~/.ssh/config)",
    )
    p.add_argument(
        "--port", "-p", type=int, default=None,
        help="Remote port (-p); overrides Port from the config (SSH-23)",
    )
    p.add_argument(
        "--identity", "-i", default=None,
        help="Private key path (-i -o IdentitiesOnly=yes) (SSH-23)",
    )
    p.add_argument(
        "--control-master", choices=("off", "auto"), default=CONTROL_MASTER_DEFAULT,
        help=(
            "Opt into OpenSSH ControlMaster reuse (POSIX only; on Windows use "
            "--commands-file instead - SSH-12)"
        ),
    )
    p.add_argument(
        "--control-persist", type=int, default=CONTROL_PERSIST_DEFAULT,
        help="ControlPersist seconds when --control-master=auto (default: 600)",
    )
    p.add_argument(
        "--control-path-dir", default=None,
        help="Directory for ControlMaster sockets (default: temp dir)",
    )
    p.add_argument(
        "-o", "--ssh-option", action="append", default=[], metavar="KEY=VALUE",
        help=(
            "Extra `ssh -o` option; repeatable. Example: "
            "-o KexAlgorithms=+diffie-hellman-group-exchange-sha1. "
            "ProxyCommand / LocalCommand / Match and friends are refused."
        ),
    )
    p.add_argument(
        "--auth", choices=("auto", "key", "password"), default="auto",
        help=(
            "Auth routing: 'key' forces BatchMode publickey; 'password' uses "
            "the pty (POSIX) or SSH_ASKPASS over pipes (Windows); 'auto' reads "
            "# auth: metadata (default: auto)"
        ),
    )
    p.add_argument(
        "--password-env", default="SSH_PASSWORD",
        help="Environment variable holding the password (default: SSH_PASSWORD)",
    )
    p.add_argument(
        "--accept-new-hostkey", action="store_true",
        help=(
            "Trust an as-yet-unknown host key via "
            "StrictHostKeyChecking=accept-new on both the key and password "
            "routes (SSH-06). This is NOT strictly one-shot: the key is "
            "appended to known_hosts and every later call trusts it silently. "
            "A *changed* key still fails with host_key (review B3)"
        ),
    )
    p.add_argument(
        "--sudo", action="store_true",
        help=(
            "Run the command under sudo, non-interactively: "
            "`sudo -n [-u USER] -- sh -c '<cmd>'`. A missing NOPASSWD rule "
            "fails fast with failure_class=sudo_required (exit 112) instead of "
            "hanging on a prompt. Pick the target user with --sudo-user USER "
            "(DV-14); `--sudo=USER` is not accepted"
        ),
    )
    p.add_argument(
        "--sudo-user", default="root", metavar="USER",
        help="Target user for --sudo (default: root)",
    )
    p.add_argument(
        "--sudo-preserve-env", action="store_true",
        help="Pass -E to sudo so the remote command keeps its environment",
    )
    p.add_argument(
        "--sudo-password-env", default=None, metavar="VAR",
        help=(
            "Feed the sudo password from this environment variable over stdin "
            "(`sudo -S`). Opt-in; a NOPASSWD sudoers rule is the better answer. "
            "Needs a single command (no --commands-file/--stdin-text)"
        ),
    )
    p.add_argument(
        "--follow", "-f", action="store_true",
        help=(
            "Keep streaming output until Ctrl-C or --timeout, like `tail -f`. "
            "The live transcript goes to stderr with --json and to stdout "
            "otherwise; the final envelope is still valid JSON (DV-18)"
        ),
    )
    p.add_argument(
        "--tail", type=int, default=None, metavar="N",
        help=(
            "With --follow: start from the last N lines (wraps the command in "
            "`stdbuf -oL sh -c ... | tail -n N -f`). Ignored without --follow."
        ),
    )
    p.add_argument(
        "--redact", dest="redact", action="store_true", default=True,
        help="Hide HostName/User/Port/IdentityFile in the envelope (default: on)",
    )
    p.add_argument(
        "--no-redact", dest="redact", action="store_false",
        help="Debug: keep raw host identifiers in the envelope",
    )
    audit_cli_flags(p)
    retry_cli_flags(p)
    p.add_argument(
        "--json", action="store_true",
        help="Emit the shared JSON output contract on stdout",
    )
    p.add_argument(
        "--version", "-V", action="store_true", help="Print the ssh-core version",
    )
    return p


def _control_path(host: str, ssh_config: str | None, control_path_dir: str | None) -> str:
    """Return a deterministic, private OpenSSH ControlPath for this route."""
    base = (
        Path(control_path_dir)
        if control_path_dir
        else Path(tempfile.gettempdir()) / CONTROL_PATH_DIR_NAME
    )
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        base.chmod(0o700)
    digest_input = f"{host}\0{ssh_config or ''}".encode()
    digest = hashlib.sha256(digest_input).hexdigest()[:CONTROL_PATH_DIGEST_LEN]
    return str(base / digest)


def _validate_ssh_options(options: list[str]) -> str | None:
    for option in options:
        reason = validate_ssh_option(option)
        if reason is not None:
            return reason
    return None


def split_target(host: str) -> tuple[str, int | None]:
    """Split ``user@host:port`` / ``[v6]:port`` into ``(target, port)``.

    OpenSSH itself does *not* understand ``host:port`` in its destination
    argument, which is why ``ssh_execute root@10.0.0.5:2222`` used to die with
    "Could not resolve hostname 10.0.0.5:2222" (SSH-23). We accept it as
    sugar and translate it into ``-p``.
    """
    if host == "local":
        return host, None
    if host.startswith("[") and "]:" in host:
        closing = host.index("]")
        port_text = host[closing + 2:]
        if port_text.isdigit():
            return host[1:closing], int(port_text)
        return host, None
    if host.count(":") == 1:
        head, _, tail = host.partition(":")
        if tail.isdigit():
            return head, int(tail)
    return host, None


def _build_remote_argv(
    host: str,
    command: str,
    connect_timeout: int,
    ssh_config: str | None,
    *,
    control_master: str = CONTROL_MASTER_DEFAULT,
    control_persist: int = CONTROL_PERSIST_DEFAULT,
    control_path_dir: str | None = None,
    ssh_options: list[str] | None = None,
    port: int | None = None,
    identity: str | None = None,
    accept_new_hostkey: bool = False,
    password_auth: bool = False,
    force_tty: bool = False,
    user: str | None = None,
) -> list[str]:
    """Build the native ssh argv without spawning a subprocess."""
    argv: list[str] = ["ssh"]
    if password_auth:
        # BatchMode aborts at the first prompt; pin the methods instead.
        argv += [
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "UpdateHostKeys=no",
        ]
    else:
        argv += ["-o", "BatchMode=yes"]
    argv += ["-o", f"ConnectTimeout={connect_timeout}"]
    if accept_new_hostkey:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password_auth:
        argv += ["-o", "StrictHostKeyChecking=yes"]
    # NOTE: do not add `-o LogLevel=ERROR` here. It is the obvious way to keep
    # sshd's login banner out of stderr (SSHCORE-017), but it also silences
    # `Connection refused` on OpenSSH 9.x and `Unable to negotiate ...` on
    # every version — the two diagnostics that classify `network` and `kex`.
    # Losing them re-opens SSHCORE-003. The banner is removed from the captured
    # text by `lib.split_login_banner` instead, which cannot affect exit codes.
    ssh_config_path = merged_ssh_config(ssh_config)
    if ssh_config_path:
        argv += ["-F", ssh_config_path]
    if port is not None:
        argv += ["-p", str(port)]
    if identity:
        argv += ["-i", str(Path(identity).expanduser()), "-o", "IdentitiesOnly=yes"]
    # `-l USER` beats both the config's User and the local account name, and
    # keeps the host token intact (review B1: `--user root lab-box`).
    if user:
        argv += ["-l", user]
    if control_master == "auto":
        argv += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPersist={control_persist}",
            "-o", f"ControlPath={_control_path(host, ssh_config, control_path_dir)}",
        ]
    if force_tty:
        argv += ["-tt"]
    for option in ssh_options or []:
        argv += ["-o", option]
    # `--` makes a command that starts with '-' reachable (SSH-24). ssh stops
    # option parsing at the destination, so this works for both forms.
    argv += ["--", host, command]
    return argv


class _StreamTee:
    """Relay a child's output to the terminal while it is produced (SSHCORE-025).

    Human mode used to print the transcript only after the command finished, so
    a long ``grep``/``tar``/``systemctl status`` produced nothing for minutes,
    and the final print reordered everything (stderr first, then stdout).

    Bytes are decoded incrementally and written to the matching stream as
    complete lines, so stdout/stderr keep the order the remote command produced
    them in. Redaction happens *per complete line* (a hostname can never be
    split across two writes and leak through).

    ``suppress_login_banner`` is set for the stderr side: sshd's greeting is
    transport noise and must not reach the terminal either (SSHCORE-017). The
    rule mirrors :func:`lib.split_login_banner` — a leading blank line followed
    by exactly one line that is not an ssh diagnostic is the banner — and it
    only ever looks at the first few lines, after which the tee is a plain
    relay.
    """

    #: How many lines the banner decision may look at before giving up and
    #: relaying everything (bounded so a silent command still streams).
    HEAD_LINES = 4

    def __init__(
        self,
        stream: Any,
        redactor: Any = None,
        lock: Any = None,
        *,
        suppress_login_banner: bool = False,
    ) -> None:
        import codecs

        self._stream = stream
        self._redactor = redactor
        self._lock = lock
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._head: list[str] | None = [] if suppress_login_banner else None

    # -- feeding ---------------------------------------------------------
    def feed(self, chunk: Any) -> None:
        if not chunk:
            return
        text = chunk if isinstance(chunk, str) else self._decoder.decode(chunk)
        if not text:
            return
        buffer = self._pending + text
        *lines, self._pending = buffer.split("\n")
        if not lines:
            return
        if self._head is None:
            self._write("\n".join(lines) + "\n")
            return
        head = self._head + lines
        decided, payload = self._decide(head)
        if not decided:
            self._head = head[-self.HEAD_LINES:]
            return
        self._head = None
        if payload:
            self._write("\n".join(payload) + "\n")

    def _decide(self, lines: list[str]) -> tuple[bool, list[str]]:
        """Return ``(decision_made, lines_to_write_now)``."""
        first = next((i for i, line in enumerate(lines) if line.strip()), None)
        if first is None:
            # Only blank lines so far — keep waiting, but never forever.
            return (len(lines) >= self.HEAD_LINES, [])
        if first == 0:
            # No leading blank line: this host has no banner.
            return (True, lines)
        from lib import SSH_CLIENT_DIAGNOSTIC_RE

        if SSH_CLIENT_DIAGNOSTIC_RE.match(lines[first].strip()):
            return (True, lines)
        # Leading blank(s) + one non-diagnostic line: that line is the banner.
        rest = lines[first + 1:]
        if rest or len(lines) > first + 1:
            return (True, rest)
        return (len(lines) >= self.HEAD_LINES, [])

    # -- writing ---------------------------------------------------------
    def _write(self, text: str) -> None:
        if not text.strip():
            return
        if self._redactor is not None:
            text = self._redactor.text(text)
        stream = self._stream
        if stream is None:
            return
        # A text stream (capsys, StringIO) gets ``str`` and does its own
        # newline translation; a real terminal *buffer* gets UTF-8 bytes
        # exactly as the child produced them — a second "\n" → "\r\n" pass
        # would turn ssh's CRLF into CRCRLF.
        payload: Any = text
        if _is_binary_stream(stream):
            payload = text.encode("utf-8", "replace")
        guard = self._lock or _NULL_LOCK
        with guard:
            if self._stream is None:
                return
            try:
                self._stream.write(payload)
                self._stream.flush()
            except (OSError, ValueError):
                self._stream = None

    def close(self) -> None:
        tail = self._decoder.decode(b"", final=True)
        if tail:
            self._pending += tail
        pending, self._pending = self._pending, ""
        if self._head is not None:
            lines = self._head + ([pending] if pending else [])
            self._head = None
            _decided, payload = self._decide(lines)
            if payload:
                self._write("\n".join(payload))
        elif pending:
            self._write(pending)
        self._stream = None


_NULL_LOCK = threading.Lock()


def _is_binary_stream(stream: Any) -> bool:
    """True for a byte sink (``sys.stdout.buffer``), False for a text sink."""
    if isinstance(stream, (io.TextIOBase, io.StringIO)):
        return False
    return isinstance(stream, (io.BufferedIOBase, io.RawIOBase, io.BytesIO)) or (
        getattr(stream, "encoding", "missing") is None
        and hasattr(stream, "write")
        and not hasattr(stream, "line_buffering")
    )


class _RawSink:
    """Stream child bytes straight to a file, with count + sha256 (review D1).

    ``--output-file`` exists because the envelope caps each stream at 10 MB,
    which is right for a JSON document and wrong for "fetch this 235 MB log".
    Before it, the only way to read the whole thing was a second ``scp``.

    The sink never buffers: it hashes and writes each chunk as it arrives, so
    memory stays flat regardless of the transfer size, and the sha256 in the
    envelope describes exactly the bytes on disk.

    Failures are sticky and reported through :attr:`error` instead of raising
    inside a reader thread (which would silently truncate the file and still
    look successful).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.bytes = 0
        self.error: str | None = None
        self._hash = hashlib.sha256()
        self._handle: Any = None
        self._lock = threading.Lock()
        try:
            self._handle = open(path, "wb")  # noqa: SIM115 — closed in close()
        except OSError as exc:
            self.error = f"cannot open {path}: {exc}"

    def write(self, chunk: bytes) -> None:
        if self.error is not None or not chunk:
            return
        with self._lock:
            try:
                self._handle.write(chunk)
            except OSError as exc:  # disk full, quota, AV lock
                self.error = f"write failed at {self.bytes} bytes: {exc}"
                return
            self._hash.update(chunk)
            self.bytes += len(chunk)

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            with contextlib.suppress(OSError):
                self._handle.flush()
                self._handle.close()
            self._handle = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(self.path),
            "bytes": self.bytes,
            "sha256": self.sha256,
        }
        if self.error:
            payload["error"] = self.error
        return payload


#: ``--stdin-file`` / ``--script-file`` normalisation (review D4).
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"


def normalize_script_bytes(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Strip a BOM and convert CRLF/CR to LF; report what was changed.

    ``--stdin-file`` and ``ssh_upload`` are byte-exact by design, which is
    right until a Windows user writes the payload with ``Set-Content`` or
    ``Out-File``: the file then starts with ``EF BB BF`` and every line ends
    ``\\r\\n``, so bash on the far side answers
    ``\\ufeffhostname: command not found`` and then
    ``syntax error: unexpected end of file``.

    Handing the user a flag (``--normalize-lf``) rather than typing into the
    transport keeps the byte-exact promise for everyone else — see review D4:
    "this should be a documented, opt-in convenience, not a silent rewrite".
    """
    notes: dict[str, Any] = {"bom_stripped": False, "crlf_converted": 0}
    out = data
    if out.startswith(_BOM_UTF8):
        out = out[len(_BOM_UTF8):]
        notes["bom_stripped"] = True
    elif out.startswith((_BOM_UTF16_LE, _BOM_UTF16_BE)):
        # A UTF-16 script is not runnable by `bash -s` at all; decode it
        # properly instead of leaving NUL bytes in the stream.
        with contextlib.suppress(UnicodeDecodeError):
            text = out.decode("utf-16")
            notes["bom_stripped"] = True
            notes["decoded_from"] = "utf-16"
            out = text.encode("utf-8")
    crlf = out.count(b"\r\n")
    if crlf:
        out = out.replace(b"\r\n", b"\n")
        notes["crlf_converted"] = crlf
    lone_cr = out.count(b"\r")
    if lone_cr:
        out = out.replace(b"\r", b"\n")
        notes["cr_converted"] = lone_cr
    notes["bytes_before"] = len(data)
    notes["bytes_after"] = len(out)
    notes["normalized"] = bool(notes["bom_stripped"] or crlf or lone_cr)
    return out, notes


def _capture_process(
    argv: list[str],
    timeout: int,
    *,
    env: dict[str, str] | None = None,
    stdin_text: str | bytes | None = None,
    tee_stdout: Any = None,
    tee_stderr: Any = None,
    redactor: Any = None,
    raw_sink: Any = None,
    stderr_sink: Any = None,
) -> tuple[int, BoundedCapture, BoundedCapture, float, bool]:
    """Run ``argv`` with bounded streaming capture.

    Returns ``(returncode, stdout_capture, stderr_capture, elapsed_s,
    timed_out)``. On timeout the child is killed but everything already read is
    preserved; bytes past the 10 MB cap are dropped and counted.

    The pipes are **binary** (SSHCORE-015/019): no locale decoding can abort a
    reader thread (which used to empty ``stdout`` while still reporting
    success), and no universal-newline translation quietly rewrites ``\\r\\n`` /
    ``\\r`` into ``\\n``. Decoding happens once, in :meth:`BoundedCapture.text`.

    ``tee_stdout`` / ``tee_stderr`` (binary or text streams) receive the bytes
    as they arrive, which is what makes human mode live (SSHCORE-025).

    ``raw_sink`` / ``stderr_sink`` are the ``--output-file`` route (review D1):
    every byte of the corresponding stream is written to disk as it arrives, so
    the 10 MB envelope cap no longer decides whether the transcript survives.
    The sink is a :class:`_RawSink`; when given, the corresponding capture is
    still filled (for ``stderr`` reporting and truncation metadata) but the
    sink's own byte count / sha256 is what the envelope reports.

    stdin is always a pipe that is closed immediately when there is nothing to
    send. ``stdin=DEVNULL`` looks equivalent but is not: on Windows OpenSSH an
    inherited ``NUL`` handle is treated as "no EOF yet", so ``ssh host cat``
    blocked until the timeout (measured: 10 s+, versus 0.2 s with a closed
    pipe). A closed pipe is an EOF the client can actually observe, on every
    platform.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env=env if env is not None else msys_safe_env(),
    )
    if proc.stdin is not None:
        payload = (
            b""
            if stdin_text is None
            else (
                stdin_text.encode("utf-8")
                if isinstance(stdin_text, str)
                else stdin_text
            )
        )

        def _feed() -> None:
            try:
                # Write bytes, not text: a text-mode TextIOWrapper translates
                # "\n" to os.linesep, which turns a POSIX shell script into
                # CRLF line noise ("$'\r': command not found").
                if payload:
                    proc.stdin.write(payload)  # type: ignore[union-attr]
                proc.stdin.close()  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass

        threading.Thread(target=_feed, daemon=True).start()

    out_sink = BoundedCapture()
    err_sink = BoundedCapture()
    lock = threading.Lock()
    out_tee = (
        _StreamTee(tee_stdout, redactor, lock) if tee_stdout is not None else None
    )
    err_tee = (
        _StreamTee(
            tee_stderr, redactor, lock, suppress_login_banner=True
        )
        if tee_stderr is not None
        else None
    )
    readers = [
        threading.Thread(
            target=_drain,
            args=(proc.stdout, out_sink, out_tee, raw_sink),
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(proc.stderr, err_sink, err_tee, stderr_sink),
            daemon=True,
        ),
    ]
    for t in readers:
        t.start()

    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        returncode = EXIT_TIMEOUT

    for t in readers:
        t.join(timeout=5)
    for tee in (out_tee, err_tee):
        if tee is not None:
            tee.close()
    for sink in (raw_sink, stderr_sink):
        if sink is not None:
            sink.close()
    elapsed = round(time.monotonic() - start, 3)
    return returncode, out_sink, err_sink, elapsed, timed_out


def _drain(
    stream: Any,
    sink: BoundedCapture,
    tee: _StreamTee | None,
    file_sink: Any = None,
) -> None:
    """Read ``stream`` into ``sink`` and, when live, relay it to ``tee``.

    ``file_sink`` (a :class:`_RawSink`) additionally receives every byte, so
    ``--output-file`` can keep a transcript that is larger than the envelope
    cap (review D1). The sink is closed by the caller, after both readers have
    joined, so a partial last chunk is never lost.
    """
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            sink.feed(chunk)
            if tee is not None:
                tee.feed(chunk)
            if file_sink is not None:
                file_sink.write(chunk)
    except (OSError, ValueError):
        return
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _shell_name(token: str) -> str:
    """Basename of ``token`` without a Windows ``.exe`` suffix (SSH-26)."""
    name = Path(token).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _is_shell_wrapper(argv: list[str]) -> bool:
    """True iff argv is ``<shell> -c <script>`` (absolute paths allowed)."""
    if len(argv) < 3:
        return False
    return _shell_name(argv[0]) in SHELL_WRAPPERS and argv[1] == "-c"


def _refused_metachar(command: str) -> str | None:
    """Return the first metacharacter that would need a shell, if any."""
    import re

    for meta in SHELL_METACHARS:
        if meta in command:
            return meta
    if re.search(CMD_PERCENT_RE, command):
        return "%%"
    if re.search(CMD_BANG_RE, command):
        return "!!"
    return None


def _run_local(
    command: str,
    timeout: int,
    *,
    stdin_text: str | None = None,
    live: bool = False,
    redactor: Any = None,
) -> dict:
    """Local route: no shell unless a wrapper is used explicitly.

    ``live`` streams the child's output to the terminal as it is produced
    (SSHCORE-025), exactly like the remote route in human mode.
    """
    try:
        argv = split_command(command)
    except ValueError as e:
        return json_result(
            success=False, exit_code=EXIT_FAIL,
            stderr=f"shlex parse error: {e}", data={"route": "local"},
        )

    if not argv:
        return json_result(
            success=False, exit_code=EXIT_FAIL,
            stderr="empty command after shlex split", data={"route": "local"},
        )

    if not _is_shell_wrapper(argv):
        meta = _refused_metachar(command)
        if meta is not None:
            return json_result(
                success=False,
                exit_code=EXIT_INVALID_ARGUMENTS,
                stderr=(
                    f"local route runs without a shell; metacharacter '{meta}' "
                    "would be interpreted by an implicit shell. Prefix with "
                    "`bash -c '...'` (quoted) to opt into a shell - for "
                    "example:\n    bash -c 'cd /tmp && grep foo *'\n"
                ),
                data={
                    "route": "local",
                    "refused_metachar": meta,
                    "failure_class": "invalid_arguments",
                },
            )

    tee_out, tee_err, streamed = _relay_targets(live, redactor)
    try:
        returncode, out, err, elapsed, timed_out = _capture_process(
            argv, timeout, stdin_text=stdin_text,
            tee_stdout=tee_out, tee_stderr=tee_err, redactor=redactor,
        )
    except FileNotFoundError as e:
        return json_result(
            success=False, exit_code=EXIT_NO_COMMAND,
            stderr=f"command not found: {e}",
            data={
                "route": "local", "shlex_argv": argv,
                "failure_class": "remote_error",
            },
        )
    except OSError as e:
        return json_result(
            success=False, exit_code=EXIT_FAIL,
            stderr=f"failed to spawn: {e}",
            data={
                "route": "local", "shlex_argv": argv,
                "failure_class": "remote_error",
            },
        )

    data: dict = {
        "route": "local",
        "shlex_argv": argv,
        "elapsed_s": elapsed,
        **({"streamed": True} if streamed else {}),
    }
    exit_code, signal_number = normalized_exit_code(returncode)
    if signal_number is not None:
        data["terminated_by_signal"] = signal_number
    _attach_capture_meta(data, out, err)
    out_text, _replaced = out.decoded()
    err_text, _err_replaced = err.decoded()
    if timed_out:
        data.update({
            "failure_class": "timeout",
            "partial_output": True,
            "stdout_bytes_captured": out.bytes_kept,
            # The local route *does* kill the real process, unlike the remote
            # route where only the ssh client dies (SSHCORE-020).
            "local_client_killed": True,
            "remote_command_may_still_run": False,
        })
        stderr_text = f"timed out after {timeout}s"
        if err_text:
            stderr_text += f"\n--- partial stderr ---\n{err_text}"
        return json_result(
            success=False, exit_code=EXIT_TIMEOUT,
            stdout=out_text, stderr=stderr_text, data=data,
        )

    data["failure_class"] = None if exit_code == 0 else "remote_error"
    return json_result(
        success=(exit_code == 0), exit_code=exit_code,
        stdout=out_text, stderr=err_text, data=data,
    )


def _attach_capture_meta(data: dict, out: BoundedCapture, err: BoundedCapture) -> None:
    """Record the truncation / lossiness of both streams (SSHCORE-022).

    Called from every exit path — success, non-zero, timeout — so the field
    set of ``data`` no longer depends on how the command ended. Before this,
    the timeout branch skipped it and a 10 MB transcript arrived with no
    ``truncated`` / ``bytes_elided`` marker at all.
    """
    data["truncated"] = out.truncated or err.truncated
    data["bytes_elided"] = out.bytes_elided + err.bytes_elided
    data["bytes_captured"] = out.bytes_kept + err.bytes_kept
    decode_errors = out.decode_errors + err.decode_errors
    if decode_errors:
        data["decode_errors"] = decode_errors
    _out_text, out_replaced = out.decoded()
    _err_text, err_replaced = err.decoded()
    data["decode_replaced"] = out_replaced + err_replaced
    # SSHCORE-015: the caller must be able to tell "the command printed
    # nothing" from "bytes arrived but could not be represented".
    data["lossy_output"] = bool(data["decode_replaced"] or data["truncated"])


# --- review C1: expected non-zero exit codes ---------------------------------

#: Codes a caller may legitimately expect. 0 is always implicit; anything
#: outside 1..255 is an argv error rather than a silent no-op.
_ALLOW_EXIT_MAX = 255


def parse_allow_exit(value: str | None) -> tuple[set[int], str | None]:
    """Parse ``--allow-exit 0,1`` into a set; return ``(codes, error)``.

    Review C1: `grep -c` with no match exits 1, `diff` exits 1 on a difference,
    `curl` exits 22/28 — all normal answers. Before this, the only workaround
    was appending ``|| true``, which throws the real status away and hides a
    genuine failure of the same command.

    The empty set (flag absent) means "0 only", i.e. the old behaviour.
    """
    if value is None:
        return set(), None
    codes: set[int] = set()
    for raw in value.replace(";", ",").split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            code = int(token, 10)
        except ValueError:
            return set(), (
                f"--allow-exit expects comma-separated integers, got {token!r}"
            )
        if code < 0 or code > _ALLOW_EXIT_MAX:
            return set(), (
                f"--allow-exit code {code} is outside 0-{_ALLOW_EXIT_MAX}"
            )
        codes.add(code)
    return codes, None


def apply_allow_exit(result: dict, allowed: set[int]) -> dict:
    """Turn an allowed non-zero exit into ``success: true`` (review C1).

    The envelope keeps the **real** ``exit_code`` and the **real**
    ``failure_class``-derived classification in ``data.observed_failure_class``;
    it only flips ``success`` and records why:

    ``data.exit_allowed = true``, ``data.allowed_exit_codes = [...]``.

    A timeout, a 255 client failure or an auth/host-key problem is never
    converted: those are not "the command answered with a code", they are
    "the command never ran".
    """
    if not allowed:
        return result
    data = result.setdefault("data", {})
    code = result.get("exit_code")
    if result.get("success") or code is None:
        return result
    observed = data.get("failure_class")
    if observed not in (None, "remote_error"):
        # network/auth/host_key/kex/timeout/sudo/...: the command did not run.
        return result
    if data.get("timed_out") or data.get("partial_output"):
        return result
    if int(code) not in allowed:
        return result
    data["exit_allowed"] = True
    data["allowed_exit_codes"] = sorted(allowed)
    data["observed_failure_class"] = observed
    data["failure_class"] = None
    result["success"] = True
    return result


def _resolve_auth(host: str, requested: str, ssh_config: str | None) -> str:
    if requested != "auto":
        return requested
    meta = read_ssh_config_metadata(host, ssh_config)
    if meta.get("auth", "").lower() == "password":
        return "password"
    if meta.get("device-type", "").lower() == "network":
        return "password"
    return "key"


def resolve_login_user(
    host: str,
    explicit: str | None,
    ssh_config: str | None,
) -> tuple[str | None, str]:
    """Work out which remote account ssh will actually use (review B1).

    Returns ``(user_or_None, source)`` where ``source`` is one of ``flag``
    (``--user``), ``target`` (``user@host``), ``config`` (``User`` in the
    alias's block) or ``none``.

    Why this exists: with password auth and no user anywhere, ssh logs in as
    the **local** account name — on a Windows client that is
    ``PCFM-084``, not ``root`` — and the failure reads
    ``Permission denied (keyboard-interactive,password)``, which points at the
    password instead of at the missing username. The review hit this once per
    host and lost time on it, and ``ssh_cluster`` reported the same misleading
    ``auth`` for the whole fleet.

    ``ssh -G`` is deliberately *not* used as the authority here: it always
    prints a ``user`` line (falling back to the local account), so it cannot
    distinguish "configured" from "guessed". The config is parsed for a real
    ``User`` directive instead.
    """
    if explicit:
        return explicit, "flag"
    target, _port = split_target(host)
    if "@" in target:
        return target.split("@", 1)[0], "target"
    configured = _configured_user(target, ssh_config)
    if configured:
        return configured, "config"
    return None, "none"


def _configured_user(alias: str, ssh_config: str | None) -> str | None:
    """First ``User`` directive in the block OpenSSH would match for ``alias``.

    Follows OpenSSH precedence: the first matching ``Host`` block wins, and a
    later block that also matches does not override an already-set ``User``.
    """
    path = Path(ssh_config or "~/.ssh/config").expanduser()
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None
    matched = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""
        if key == "host":
            patterns = value.split()
            matched = any(alias_matches(p, alias) for p in patterns)
            continue
        if key == "match":
            matched = False
            continue
        if matched and key == "user" and value:
            return value
    return None


def user_required_result(
    host: str,
    *,
    ssh_config: str | None = None,
    auth_method: str = "password",
) -> dict:
    """The ``user_required`` envelope (exit 115) for a missing login user."""
    return json_result(
        success=False,
        exit_code=EXIT_USER_REQUIRED,
        stderr=(
            f"ssh_execute: {host} needs password auth but no login user could "
            "be determined, so ssh would try the local account name and fail "
            "with a misleading 'Permission denied'.\n"
            "  Fix one of:\n"
            f"    --user root {host} \"<command>\"\n"
            f"    {host} \u2192 'root@{host}' (ad-hoc target syntax)\n"
            "    ssh_config_manager.py update --alias <alias> --user root\n"
            f"  (checked: --user, user@host, and {ssh_config or '~/.ssh/config'})\n"
        ),
        data={
            "route": "remote",
            "auth_method": auth_method,
            "failure_class": "user_required",
            "user_source": "none",
            "hint": "--user USER",
        },
    )


def _is_network_device(host: str, ssh_config: str | None) -> bool:
    """True when the alias is marked ``# device-type: network``."""
    meta = read_ssh_config_metadata(host, ssh_config)
    return meta.get("device-type", "").lower() == "network"


def _run_password_session(
    host: str,
    command: str,
    timeout: int,
    connect_timeout: int,
    ssh_config: str | None,
    *,
    ssh_options: list[str],
    accept_new_hostkey: bool,
    port: int | None,
    identity: str | None,
    password: str,
    stdin_text: str | bytes | None = None,
    user: str | None = None,
) -> dict:
    """Interactive (``-tt``/pty) password route for network-device CLIs.

    A VRP/Comware/IOS session needs a real tty: the device keeps view state and
    pages its output, and an exec channel cannot do either. Everything a tty
    costs (merged streams, CRLF, no exit status) is reported honestly here
    instead of being papered over: ``data.merged_stream`` says so, and
    ``-tt``'s missing exit status is only accepted when the device said goodbye.
    """
    session: Any = None
    started = time.monotonic()
    argv = _build_remote_argv(
        host, command, connect_timeout, ssh_config,
        ssh_options=ssh_options,
        port=port,
        identity=identity,
        accept_new_hostkey=accept_new_hostkey,
        password_auth=True,
        force_tty=True,
        user=user,
    )
    login_data: dict = {
        "route": "remote",
        "auth_method": "password",
        "transport": "pipe",
        "ssh_argv": argv,
        "elapsed_s": 0.0,
        "merged_stream": True,
    }
    try:
        session = make_session(argv, password)
        started = time.monotonic()
        session.start()
    except AskpassUnavailable as exc:
        # Review A2: password auth is impossible here (no writable scratch
        # directory). Report it as its own class, never as `auth`.
        _attach_capture_meta(login_data, BoundedCapture(), BoundedCapture())
        login_data.update(exc.as_data())
        return json_result(
            success=False, exit_code=exc.exit_code,
            stderr=str(exc) + "\n", data=login_data,
        )
    except OSError as e:
        _attach_capture_meta(login_data, BoundedCapture(), BoundedCapture())
        login_data["failure_class"] = "dependency_missing"
        return json_result(
            success=False, exit_code=EXIT_DEPENDENCY,
            stderr=f"ssh_execute: failed to spawn ssh: {e}\n",
            data=login_data,
        )
    timed_out = False
    try:
        from lib.session import authenticate

        try:
            authenticate(
                session,
                timeout=min(timeout, connect_timeout + 30),
                accept_new_hostkey=accept_new_hostkey,
                host=host,
            )
        except Exception as e:  # noqa: BLE001 — SessionError carries the class
            fclass = getattr(e, "failure_class", "remote_error")
            # Same field set as every other route (SSHCORE-022): a login that
            # failed still has an elapsed time and a (possibly empty) bounded
            # capture, and consumers index these keys unconditionally.
            failed_capture = BoundedCapture()
            failed_capture.feed(str(e).encode("utf-8", "replace"))
            login_data.update({
                "transport": getattr(session, "kind", "pipe"),
                "elapsed_s": round(time.monotonic() - started, 3),
                "failure_class": fclass,
            })
            _attach_capture_meta(login_data, failed_capture, BoundedCapture())
            return json_result(
                success=False,
                exit_code=getattr(e, "exit_code", _CLASS_CODES.get(fclass, EXIT_FAIL)),
                stderr=_scrubbed(str(e), password) + "\n",
                data=login_data,
            )
        if stdin_text is not None and hasattr(session, "send"):
            session.send(
                stdin_text.decode("utf-8", "replace")
                if isinstance(stdin_text, bytes)
                else stdin_text
            )
        if hasattr(session, "close_stdin"):
            session.close_stdin()
        deadline = started + timeout
        while not session.is_closed() and time.monotonic() < deadline:
            session.read_available(0.2)
        timed_out = time.monotonic() >= deadline and not session.is_closed()
        output = _scrubbed(session.consume(), password)
    finally:
        rc = session.close() if session is not None else 1

    elapsed = round(time.monotonic() - started, 3)
    # `-tt` merges the child's streams, so the login banner arrives right where
    # the command's output does (SSHCORE-017). The login driver recorded
    # exactly what had arrived before authentication finished, which is the
    # only reliable boundary on a merged stream.
    clean_output, banner = _strip_recorded_banner(
        output, getattr(session, "login_banner", "")
    )
    capture = BoundedCapture()
    capture.feed(clean_output.encode("utf-8", "replace"))
    # The session buffer keeps the *tail* and counts what it dropped; report
    # that through the same fields the exec path uses (SSHCORE-022).
    capture.note_elided(int(getattr(session, "_buf", None).bytes_elided)
                        if getattr(session, "_buf", None) is not None else 0)
    data: dict = {
        "route": "remote",
        "auth_method": "password",
        "transport": getattr(session, "kind", "pty"),
        "ssh_argv": argv,
        "elapsed_s": elapsed,
        "merged_stream": True,
        "raw_stdout": output,
    }
    _attach_capture_meta(data, capture, BoundedCapture())
    if banner:
        data["ssh_banner_lines"] = banner
    if timed_out:
        data.update({
            "failure_class": "timeout",
            "partial_output": True,
            "stdout_bytes_captured": capture.bytes_kept,
            "local_client_killed": True,
            "remote_command_may_still_run": True,
        })
        return json_result(
            success=False, exit_code=EXIT_TIMEOUT,
            stdout=_strip_farewell(clean_output), stderr=f"timed out after {timeout}s",
            data=data,
        )
    # A clean logout over a forced tty ends with "Connection to <host> closed."
    # and ssh's exit status 255 because the channel had no exit-status. With
    # output present that is a success, not a client failure.
    device_closed = bool(output.strip()) and closed_by_device(output)
    if rc in (0, None) or (rc == 255 and device_closed):
        data["failure_class"] = None
        if rc == 255:
            data["device_closed_after_output"] = True
        return json_result(
            success=True,
            exit_code=EXIT_OK,
            stdout=_strip_farewell(clean_output),
            data=data,
        )
    fclass = classify_failure(int(rc), output) or "remote_error"
    data["failure_class"] = fclass
    return json_result(
        success=False,
        exit_code=EXIT_AUTH if fclass == "auth" else _CLASS_CODES.get(fclass, int(rc)),
        stdout="" if fclass in ("auth", "host_key", "network") else _strip_farewell(clean_output),
        stderr=clean_output if fclass in ("auth", "host_key", "network") else f"ssh exited {rc}\n",
        data=data,
    )


def _strip_recorded_banner(output: str, banner: str) -> tuple[str, list[str]]:
    """Remove the *known* pre-auth banner prefix from a merged transcript.

    The interactive routes (``-tt`` pipe, pty) merge stdout and stderr, so
    sshd's greeting sits in the same buffer as the command's output and there
    is nothing in the bytes to separate them. The login driver therefore
    records what had arrived before authentication finished
    (``session.login_banner``) — that prefix, and only that prefix, is
    transport noise (SSHCORE-017).

    With no recorded banner the transcript is returned untouched. Guessing from
    shape ("a leading blank line plus one line") is *not* safe on a merged
    stream: the login driver may already have consumed the greeting, and the
    command's own first line is then the one that gets deleted. That is exactly
    what an early version of this fix did to `echo 隧道已建立` on OpenSSH 8.2.
    """
    if not banner:
        return output, []
    marker = banner.strip()
    stripped = output.lstrip("\r\n \t")
    if not marker or not stripped.startswith(marker):
        return output, []
    remainder = stripped[len(marker):]
    # drop the newline that terminated the banner line, keep the rest verbatim
    if remainder.startswith("\r\n"):
        remainder = remainder[2:]
    elif remainder.startswith("\n"):
        remainder = remainder[1:]
    return remainder, [line for line in banner.splitlines() if line.strip()]


def _scrubbed(text: str, secret: str) -> str:
    from lib.askpass import scrub_secret

    return scrub_secret(text, secret)


def _strip_farewell(text: str) -> str:
    """Drop ssh's own "Connection to <host> closed." trailer.

    On a forced tty (``-tt``) ssh appends that line to the command's output.
    It is transport noise, not command output, so it is removed from
    ``stdout`` (the raw text stays available in ``data.raw_stdout``).

    The trailer is only stripped when it stands on its own line. ssh appends it
    *without* a preceding newline when the command's last line did not end in
    one (``printf x`` → ``xConnection to h closed.``); trimming the suffix in
    that case is safe because the phrase cannot be produced by the remote shell
    without ssh adding it, and leaving it in would corrupt the byte count.
    """
    lines = text.splitlines()
    while lines and re.match(
        r"^(Connection to .* closed\.|Shared connection to .* closed\.)$",
        lines[-1].strip(),
    ):
        lines.pop()
    joined = "\n".join(lines)
    joined = re.sub(
        r"(Connection to \S+ closed\.|Shared connection to \S+ closed\.)$",
        "",
        joined.rstrip("\r\n"),
    )
    if not joined:
        return ""
    return joined + ("\n" if text.endswith(("\n", "\r")) or lines != text.splitlines() else "")


def _run_remote(
    host: str,
    command: str,
    timeout: int,
    connect_timeout: int,
    ssh_config: str | None,
    *,
    control_master: str = CONTROL_MASTER_DEFAULT,
    control_persist: int = CONTROL_PERSIST_DEFAULT,
    control_path_dir: str | None = None,
    ssh_options: list[str] | None = None,
    accept_new_hostkey: bool = False,
    port: int | None = None,
    identity: str | None = None,
    user: str | None = None,
    stdin_text: str | None = None,
    live: bool = False,
    redactor: Any = None,
    password: str | None = None,
    output_file: str | None = None,
) -> dict:
    """Remote route: subprocess + native ssh, capture everything.

    ``live`` (human mode, no ``--json``) relays the child's bytes to the
    terminal as they arrive (SSHCORE-025); the envelope is still filled, so the
    caller gets one transcript and one exit code, in the order the remote
    command produced them.

    ``password`` selects the password auth profile and runs the child under
    ``SSH_ASKPASS`` instead of ``BatchMode=yes``. The command channel stays a
    normal exec channel — no ``-tt`` — so the remote exit status, the stderr
    split and the byte-exact capture all behave exactly like the key route.

    ``output_file`` streams stdout to disk instead of relying on the 10 MB
    envelope cap (review D1); see :class:`_RawSink`.
    """
    argv = _build_remote_argv(
        host, command, connect_timeout, ssh_config,
        control_master=control_master,
        control_persist=control_persist,
        control_path_dir=control_path_dir,
        ssh_options=ssh_options,
        port=port,
        identity=identity,
        accept_new_hostkey=accept_new_hostkey,
        password_auth=bool(password),
        user=user,
    )

    sink: _RawSink | None = None
    sink_failure: dict | None = None
    if output_file:
        target = Path(output_file).expanduser()
        if target.parent and not target.parent.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                sink_failure = json_result(
                    success=False, exit_code=EXIT_INVALID_ARGUMENTS,
                    stderr=(
                        f"ssh_execute: cannot create the directory for "
                        f"--output-file {target}: {exc}\n"
                    ),
                    data={
                        "route": "remote",
                        "failure_class": "invalid_arguments",
                        "output_file": str(target),
                    },
                )
        if sink_failure is None:
            sink = _RawSink(target)
            if sink.error is not None:
                sink_failure = json_result(
                    success=False, exit_code=EXIT_INVALID_ARGUMENTS,
                    stderr=f"ssh_execute: {sink.error}\n",
                    data={
                        "route": "remote",
                        "failure_class": "invalid_arguments",
                        "output_file": str(target),
                    },
                )
                sink = None
    if sink_failure is not None:
        return sink_failure

    tee_out, tee_err, streamed = _relay_targets(live, redactor)
    env = None
    askpass = None
    try:
        if password:
            askpass = askpass_env(password)
            env = askpass.__enter__()
        returncode, out, err, elapsed, timed_out = _capture_process(
            argv, timeout, stdin_text=stdin_text,
            tee_stdout=tee_out, tee_stderr=tee_err, redactor=redactor,
            env=env, raw_sink=sink,
        )
    except AskpassUnavailable as exc:
        # Review A2: the one path that used to escape as a traceback.
        return json_result(
            success=False, exit_code=exc.exit_code,
            stderr=str(exc) + "\n",
            data={
                "route": "remote",
                "auth_method": "password",
                "ssh_argv": argv,
                **exc.as_data(),
            },
        )
    except OSError as e:
        return json_result(
            success=False, exit_code=EXIT_DEPENDENCY,
            stderr=f"failed to spawn ssh: {e}",
            data={
                "route": "remote", "ssh_argv": argv,
                "failure_class": "dependency_missing",
            },
        )
    finally:
        if askpass is not None:
            askpass.__exit__(None, None, None)

    data: dict = {
        "route": "remote",
        "ssh_argv": argv,
        "elapsed_s": elapsed,
        **({"streamed": True} if streamed else {}),
        **({"auth_method": "password"} if password else {}),
        **({"user_source": "flag"} if user else {}),
    }
    if sink is not None:
        data["output_file"] = sink.as_dict()
        # The transcript lives on disk; keeping a second copy in the envelope
        # would defeat the point (it is capped at 10 MB anyway, and a caller
        # that asked for a file wants the file).
        out_text = (
            f"<stdout written to {sink.path} ({sink.bytes} bytes, "
            f"sha256 {sink.sha256[:16]}...)>"
        )
        out_replaced = 0
    else:
        out_text, out_replaced = out.decoded()
    err_text, err_replaced = err.decoded()
    exit_code, signal_number = normalized_exit_code(returncode)
    if signal_number is not None:
        # SSHCORE-023: subprocess reports "killed by SIGTERM" as -15; the
        # envelope uses the shell convention (143) and keeps the raw value.
        data["terminated_by_signal"] = signal_number
        data["raw_returncode"] = returncode
    _attach_capture_meta(data, out, err)
    if sink is not None:
        data["output_file_written"] = sink.error is None
        if sink.error is not None:
            # A half-written file is worse than no file: say so and fail.
            data["truncated"] = True
            data["lossy_output"] = True
            data["failure_class"] = "invalid_arguments"
            return json_result(
                success=False, exit_code=EXIT_INVALID_ARGUMENTS,
                stdout=out_text, stderr=f"ssh_execute: {sink.error}\n", data=data,
            )
    if timed_out:
        data.update({
            "failure_class": "timeout",
            "partial_output": True,
            "stdout_bytes_captured": out.bytes_kept,
            "raw_stderr": err_text,
            # SSHCORE-020: killing the local client does not stop the remote
            # command (no SSH_MSG_CHANNEL_CLOSE is sent), and pretending
            # otherwise sends operators hunting for a process that is still
            # running. Say it in the envelope, not just in the docs.
            "local_client_killed": True,
            "remote_command_may_still_run": True,
        })
        if sink is not None:
            # Everything received up to the kill is on disk; that is the
            # documented "partial output survives" promise, now uncapped.
            data["output_file"]["partial"] = True
        stderr_text = f"timed out after {timeout}s"
        partial = split_login_banner(err_text)[0]
        if partial:
            stderr_text += f"\n--- partial stderr ---\n{partial}"
        return json_result(
            success=False, exit_code=EXIT_TIMEOUT,
            stdout=out_text, stderr=stderr_text, data=data,
        )

    # SSHCORE-017: peel sshd's login banner off stderr before anything else
    # looks at it. It shares the channel with the command's own stderr, so
    # without this every success on a "Banner" host looked like a warning.
    real_stderr, banner = split_login_banner(err_text, command_output=out_text)
    real_stderr, noise = filter_ssh_noise(real_stderr)
    if banner:
        data["ssh_banner_lines"] = banner
    if signal_number is not None:
        data["raw_returncode"] = returncode
    failure_class = classify_failure(exit_code, err_text)
    data.update({
        "ssh_noise_lines": noise,
        "failure_class": failure_class,
        "raw_stderr": err_text,
    })
    # A failure_class is derived from the raw stream on purpose: filtering the
    # banner first would be fine too, but classification must never depend on
    # the filtering being correct.
    if returncode == 255 and failure_class in _CLASS_CODES:
        # ssh's own 255 means "the client failed", so it is translated into
        # the single failure_class → exit_code table. Any other non-zero code
        # is the *remote command's* exit status and is passed through
        # untouched: a caller doing `ssh_execute.py h "exit 42"` must still
        # see 42.
        exit_code = _CLASS_CODES[failure_class]
    return json_result(
        success=(exit_code == 0), exit_code=exit_code,
        stdout=out_text, stderr=real_stderr, data=data,
    )


def _relay_targets(live: bool, redactor: Any) -> tuple[Any, Any, bool]:
    """Return ``(stdout_tee, stderr_tee, streaming)`` for the capture path.

    Human mode streams: the bytes must reach the terminal while the command
    runs, and stdout/stderr must keep their relative order. JSON mode never
    tees — its stdout is reserved for the single envelope.

    The streams are the *binary* buffers so the relay is byte-exact (no console
    code-page translation); a caller whose stdout has no ``buffer`` (a plain
    ``StringIO`` in tests) simply gets no tee.
    """
    if not live:
        return None, None, False
    out = getattr(sys.stdout, "buffer", None)
    err = getattr(sys.stderr, "buffer", None)
    if out is None or err is None:
        return None, None, False
    return out, err, True


def _batch_script(commands: list[str]) -> str:
    """The ``sh -s`` script that brackets each command with markers (SSH-12).

    Two hardening rules from FA-10:

    * ``< /dev/null`` on every command, so a command that reads stdin (``cat``,
      ``read``, ``ssh``, ``sudo -S``) gets EOF instead of swallowing the rest of
      the script — which used to make the whole batch fail with
      ``batch_parse_error``;
    * each command runs in a subshell so ``exit`` only ends that one command,
      not the batch. The count check in :func:`_run_batch` is what turns a
      truncated batch into a failure rather than a green light.

    There is deliberately **no** top-level ``exec 0</dev/null``: the script
    itself arrives on stdin (``sh -s``), so redirecting fd 0 discards the rest
    of the script and the shell exits silently with status 0 — the batch then
    reports ``batch_incomplete`` with every index missing, and whether it
    happens at all depends on how much the shell buffered. The per-command
    redirect does the same job without eating the input.

    Since 1.5 (review C2) each command's **stderr** is captured too: it goes to
    a private file inside the batch's temp directory and is replayed after the
    exit marker between ``__SSHCORE_STDERR__`` fences. Before this, a failing
    command's message landed in the *top-level* ``data.stderr`` with nothing
    saying which index produced it — the review had to infer it from
    ``failed_indexes`` and the message text.

    The temp directory is created by the *first* line of the script (one
    ``mktemp -d`` per batch, removed by a trap), so a batch that dies half-way
    still cleans up.
    """
    parts: list[str] = [
        f'{_BATCH_TMP_INIT}\n',
    ]
    for index, command in enumerate(commands):
        parts.append(
            f"printf '\\n{_MARK}({index})\\n'\n"
            f"( {command}\n) < /dev/null "
            f"2>\"${_BATCH_TMP_VAR}/stderr.{index}\"\n"
            f"printf '\\n{_MARK_END}({index})=%s\\n' \"$?\"\n"
            f"printf '\\n{_MARK_STDERR}({index})\\n'\n"
            f"cat \"${_BATCH_TMP_VAR}/stderr.{index}\" 2>/dev/null\n"
            f"printf '\\n{_MARK_STDERR_END}({index})\\n'\n"
        )
    return "".join(parts)


def _run_batch(
    host: str,
    commands: list[str],
    timeout: int,
    connect_timeout: int,
    ssh_config: str | None,
    *,
    ssh_options: list[str],
    port: int | None,
    identity: str | None,
    accept_new_hostkey: bool,
    password: str | None = None,
    user: str | None = None,
    allow_exit: set[int] | None = None,
) -> dict:
    """Run N commands over one connection (SSH-12).

    The remote side gets ``sh -s`` and reads the script from stdin, so the
    commands never touch argv and quoting is preserved verbatim. Each command
    is bracketed by markers plus its exit status, which is what makes a
    per-command report possible over a single channel.

    ``password`` (SSHCORE-026 follow-up) routes this through the same
    askpass-backed exec channel as the single-command route, so a
    password-only host gets the identical batch semantics instead of a
    separate pty code path.

    ``allow_exit`` (review C1) is applied both per command and to the batch as
    a whole: ``data.results[i].exit_allowed`` says which entries were expected
    to be non-zero, and a batch whose only failures are expected ones keeps
    ``success: true`` / ``exit_code: 0``.

    The top-level exit code mirrors the single-command route (review C3): the
    **first** failing command's own status, so the same ``grep -c`` answers
    ``exit 1`` whether it ran alone or in a batch. ``104`` is kept for the
    cases that are not a command's status at all — an incomplete batch and an
    unparseable transcript.
    """
    allowed = set(allow_exit or ())
    script = _batch_script(commands)
    result = _run_remote(
        host, "sh -s", timeout, connect_timeout, ssh_config,
        ssh_options=ssh_options,
        accept_new_hostkey=accept_new_hostkey,
        port=port,
        identity=identity,
        user=user,
        stdin_text=script,
        password=password,
    )
    data = result.setdefault("data", {})
    data["batch"] = True
    data["batch_commands"] = len(commands)
    transcript = result.get("stdout") or ""
    parsed = _parse_batch_output(transcript)
    data["results"] = parsed
    # FA-10: a batch is successful only when *every* command in it produced a
    # result and every one of them exited 0. Before this, `--command "echo ONE"
    # --command "exit 0" --command "echo THREE"` reported success with
    # `batch_commands: 3` and one result — a silent truncation.
    seen = {item["index"] for item in parsed}
    missing = [index for index in range(len(commands)) if index not in seen]
    failed = [
        item["index"]
        for item in parsed
        if item["exit_code"] != 0 and item["exit_code"] not in allowed
    ]
    expected = [
        item["index"]
        for item in parsed
        if item["exit_code"] != 0 and item["exit_code"] in allowed
    ]
    if allowed:
        for item in parsed:
            if item["exit_code"] in allowed:
                item["exit_allowed"] = True
        data["allowed_exit_codes"] = sorted(allowed)
    if expected:
        data["allowed_indexes"] = expected
    if missing:
        data["batch_incomplete"] = True
        data["missing_indexes"] = missing
    if failed:
        data["failed_indexes"] = failed
    # Top-level stderr is a *summary*, not the per-command text (review C2):
    # results[i].stderr owns the messages now, and leaving them in both places
    # is what made "which command failed" ambiguous.
    stray_stderr = _strip_batch_transcript(result.get("stderr") or "")
    if stray_stderr:
        data["stderr_outside_results"] = stray_stderr
        result["stderr"] = stray_stderr
    else:
        result["stderr"] = ""
    if missing or failed:
        result["success"] = False
        data["failure_class"] = "remote_error"
        if missing:
            # Not a command's status: an incomplete batch stays 104 (review C3
            # keeps the distinction explicit instead of pretending otherwise).
            result["exit_code"] = EXIT_REMOTE_ERROR
        else:
            first = min(failed)
            own = next(
                item["exit_code"] for item in parsed if item["index"] == first
            )
            result["exit_code"] = own
        if missing and not failed:
            result["stderr"] = (
                f"ssh_execute: batch stopped after {len(parsed)} of "
                f"{len(commands)} commands (missing indexes: {missing}); did a "
                "command call `exit`?\n" + (result.get("stderr") or "")
            )
    elif not parsed:
        result["success"] = False
        result["exit_code"] = EXIT_REMOTE_ERROR
        data["failure_class"] = "remote_error"
        data["batch_parse_error"] = True
    else:
        # Every command either exited 0 or an allowed code (review C1/C3).
        result["success"] = True
        result["exit_code"] = EXIT_OK
        data["failure_class"] = None
        nonzero = [item["index"] for item in parsed if item["exit_code"] != 0]
        if nonzero:
            data["observed_nonzero_indexes"] = nonzero
    return result


_BATCH_TMP_VAR = "__SSHCORE_TMP"
_BATCH_TMP_INIT = (
    f"{_BATCH_TMP_VAR}=$(mktemp -d 2>/dev/null || "
    f"printf '/tmp/ssh-core-batch.%s' $$); mkdir -p \"${_BATCH_TMP_VAR}\"; "
    f"trap 'rm -rf \"${_BATCH_TMP_VAR}\"' EXIT"
)
_MARK = "__SSHCORE_BEGIN__"
_MARK_END = "__SSHCORE_EXIT__"
_MARK_STDERR = "__SSHCORE_STDERR__"
_MARK_STDERR_END = "__SSHCORE_STDERR_END__"


def _parse_batch_output(text: str) -> list[dict]:
    """Split the marked batch transcript into per-command records.

    Each record is ``{index, stdout, stderr, exit_code}`` — ``stderr`` was
    missing entirely before 1.5 (review C2), so a batch of 20 diagnostics could
    not say which command produced an error message; the text was dumped into
    the top-level ``stderr`` instead.
    """
    import re

    stderr_blocks: dict[int, str] = {}
    stderr_pattern = re.compile(
        rf"{re.escape(_MARK_STDERR)}\((\d+)\)\n(.*?){re.escape(_MARK_STDERR_END)}\((\d+)\)",
        re.DOTALL,
    )
    for match in stderr_pattern.finditer(text):
        stderr_blocks[int(match.group(1))] = match.group(2).rstrip("\n")

    records: list[dict] = []
    pattern = re.compile(
        rf"{re.escape(_MARK)}\((\d+)\)\n(.*?){re.escape(_MARK_END)}\((\d+)\)=(\d+)",
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        index = int(match.group(1))
        records.append({
            "index": index,
            "stdout": match.group(2).rstrip("\n"),
            "stderr": stderr_blocks.get(index, ""),
            "exit_code": int(match.group(4)),
        })
    return records


def _strip_batch_transcript(stderr: str) -> str:
    """Drop the batch's own marker lines from a top-level stderr (review C2).

    With stderr redirected per command, the only text left on the channel is
    what ssh itself printed (a dead connection, a DNS failure) plus the
    ``cat`` of a file that does not exist. Anything that is purely our own
    marker scaffolding is not a diagnostic and is removed.
    """
    if not stderr:
        return ""
    kept: list[str] = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith(
            (
                _MARK, _MARK_END, _MARK_STDERR, _MARK_STDERR_END,
                f"cat: /tmp/ssh-core-batch", f"cat: ${_BATCH_TMP_VAR}",
            )
        ):
            continue
        kept.append(line)
    out = "\n".join(kept).strip("\n")
    return out + "\n" if out else ""


def _audit(args: argparse.Namespace, result: dict) -> None:
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    path = append_audit(
        args.host,
        {
            "host": args.host,
            "route": result.get("data", {}).get("route"),
            "auth_method": result.get("data", {}).get("auth_method", "key"),
            "transport": result.get("data", {}).get("transport"),
            "command": args.command or "; ".join(args.commands),
            "batch": bool(args.commands or args.commands_file),
            "exit_code": result.get("exit_code"),
            "success": result.get("success"),
            "failure_class": result.get("data", {}).get("failure_class"),
            "elapsed_s": result.get("data", {}).get("elapsed_s"),
            "output_bytes": len(result.get("stdout") or ""),
        },
    )
    if path is not None:
        result.setdefault("data", {})["audit_log"] = str(path)


def _load_commands(args: argparse.Namespace) -> list[str]:
    commands = list(args.commands)
    if args.commands_file:
        path = Path(args.commands_file).expanduser()
        # FA-03a: an unreadable command file used to raise FileNotFoundError
        # straight through ``main()`` — no envelope, no exit code, just a
        # traceback on stderr and an empty stdout. The whole point of ``--json``
        # is that a parser never has to special-case the error path.
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CommandFileError(
                f"ssh_execute: cannot read --commands-file {path}: {exc}"
            ) from exc
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                commands.append(stripped)
    return commands


class CommandFileError(Exception):
    """Raised when ``--commands-file`` cannot be read (FA-03)."""


def parse_commands_or_fail(
    args: argparse.Namespace,
) -> tuple[list[str], dict | None]:
    """Return ``(commands, failure_envelope)``; never raises on a bad file."""
    try:
        return _load_commands(args), None
    except CommandFileError as exc:
        return [], json_result(
            success=False,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr=f"{exc}\n",
            data={
                "failure_class": "invalid_arguments",
                "commands_file": str(args.commands_file),
            },
        )


def _known_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Every option string the parser (and its subparsers) accepts."""
    found: set[str] = set(parser._option_string_actions)  # noqa: SLF001
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for sub in action.choices.values():
                found |= _known_option_strings(sub)
    return found


def _option_takes_value(parser: argparse.ArgumentParser) -> dict[str, bool]:
    """Map each option string to "does it consume the next argv token"."""
    table: dict[str, bool] = {}
    for action in parser._actions:  # noqa: SLF001
        takes = action.nargs != 0
        for option in action.option_strings:
            table[option] = takes
    return table


def _separate_command(
    parser: argparse.ArgumentParser, argv: list[str]
) -> tuple[list[str], str | None]:
    """Split ``<flags> <host> <command...>`` into parser argv + command text.

    Needed because the command is a free-form string that may start with ``-``
    (SSH-24: ``ssh_execute.py lab-240 -v`` used to be parsed as an unknown flag
    and rejected with exit 2). argparse cannot express "one positional that may
    look like an option", and ``--`` does not help either: everything after it
    becomes positional, so a trailing ``--json`` would be swallowed into the
    command instead of being honoured.

    Rules:

    * The first token that is not a known option (and not an option's value) is
      the host.
    * After the host, ``--`` forces every remaining token into the command.
    * Otherwise known **boolean** flags (``--json``, ``--accept-new-hostkey``,
      ...) are always lifted back out, known value-taking options are lifted
      while no command token has been seen yet, and everything else becomes the
      command (joined with single spaces, so ``-- -v --json`` keeps ``-v``).
    """
    takes = _option_takes_value(parser)
    known = _known_option_strings(parser)

    host_index: int | None = None
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            # No host yet: the next token is the host.
            if i + 1 < len(argv):
                host_index = i + 1
            break
        if token.startswith("-") and token != "-":
            base = token.split("=", 1)[0]
            if takes.get(base) and "=" not in token:
                i += 2
                continue
            i += 1
            continue
        host_index = i
        break

    if host_index is None:
        return argv, None

    head = argv[: host_index + 1]
    rest = argv[host_index + 1:]
    flags: list[str] = []
    command_tokens: list[str] = []
    forced = False
    j = 0
    while j < len(rest):
        token = rest[j]
        token_base = token.split("=", 1)[0]
        if forced:
            # After `--` everything is the command, *except* known flags that
            # follow at least one command token: `-- -v --json` means the
            # command is `-v` and the caller still wants JSON.
            if command_tokens and token.startswith("-") and token_base in known:
                if takes.get(token_base) and "=" not in token:
                    flags.extend(rest[j:j + 2])
                    j += 2
                    continue
                flags.append(token)
                j += 1
                continue
            command_tokens.append(token)
            j += 1
            continue
        if token == "--":
            forced = True
            j += 1
            continue
        base = token_base
        if token.startswith("-") and token != "-" and base in known:
            if takes.get(base) and "=" not in token:
                flags.extend(rest[j:j + 2])
                j += 2
                continue
            if not takes.get(base) or not command_tokens:
                flags.append(token)
                j += 1
                continue
        command_tokens.append(token)
        j += 1

    command = " ".join(command_tokens).strip() if command_tokens else None
    return head + flags, command


def _read_stdin_file(path: str) -> tuple[bytes | None, dict | None]:
    """Read ``--stdin-file`` once so every worker can be fed identically.

    Returns ``(data, failure_envelope)``. Reading the file here (rather than
    letting each child inherit a shared descriptor) is what makes a cluster
    broadcast deterministic: N concurrent workers on one fd would race, and
    the loser would see EOF while still reporting success (SSHCORE-024).
    """
    target = Path(path).expanduser()
    try:
        data = target.read_bytes()
    except OSError as exc:
        return None, json_result(
            success=False,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr=f"ssh_execute: cannot read --stdin-file {target}: {exc}\n",
            data={
                "failure_class": "invalid_arguments",
                "stdin_file": str(target),
            },
        )
    return data, None


def _validate_timeouts(args: argparse.Namespace) -> dict | None:
    """Reject non-positive budgets instead of faking a timeout (SSHCORE-021).

    ``--timeout -1`` used to be forwarded to ``proc.wait(timeout=-1)``, which
    raises ``TimeoutExpired`` immediately, so a *typo* produced a perfectly
    normal-looking ``failure_class: "timeout"`` envelope with ``elapsed_s:
    -1.0``. ``--connect-timeout -5`` became ``-o ConnectTimeout=-5``, which ssh
    rejects as ``invalid time value`` and ssh-core then classified as
    ``remote_error``.

    Returning ``None`` means "fine"; otherwise the caller gets an
    ``invalid_option`` envelope (exit 2), the same class used for a refused
    ``-o``.
    """
    for flag, value in (("--timeout", args.timeout), ("--connect-timeout", args.connect_timeout)):
        if value is None:
            continue
        if value < 1:
            return json_result(
                success=False,
                exit_code=EXIT_INVALID_OPTION,
                stderr=(
                    f"ssh_execute: {flag} must be at least 1 second (got {value}); "
                    "0 or a negative number used to look like a real timeout.\n"
                ),
                data={
                    "failure_class": "invalid_option",
                    "invalid_option": flag,
                    "value": value,
                },
            )
    return None


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_execute.py", argv)
    if early is not None:
        return early

    parser = build_parser()
    parse_argv, positional_command = _separate_command(parser, argv)
    args = parser.parse_args(parse_argv)
    if positional_command is not None:
        args.command = positional_command
    as_json = args.json
    commands, command_failure = parse_commands_or_fail(args)
    if command_failure is not None:
        emit(command_failure, as_json=as_json)
        return int(command_failure["exit_code"])
    if args.command is None and not commands and args.stdin_text is None \
            and args.stdin_file is None:
        result = json_result(
            success=False,
            exit_code=EXIT_ARGS,
            stderr="ssh_execute: no command given\n",
            data={"failure_class": "invalid_arguments"},
        )
        emit(result, as_json=as_json)
        return EXIT_ARGS

    stdin_bytes: bytes | None = None
    password_value: str | None = None
    if args.stdin_file:
        stdin_bytes, stdin_failure = _read_stdin_file(args.stdin_file)
        if stdin_failure is not None:
            emit(stdin_failure, as_json=as_json)
            return int(stdin_failure["exit_code"])
    elif args.stdin_text is not None:
        stdin_bytes = args.stdin_text.encode("utf-8")
    # One decoded copy for every route that still speaks text; handled in
    # `_run_remote_with_retry` via this attribute so the password/sudo paths
    # can keep their own stdin semantics.
    args._stdin_bytes = stdin_bytes
    # NB: the password is attached *after* auth resolution below (the value is
    # route-dependent, and the key route must never see it).

    refusal = _validate_ssh_options(args.ssh_option)
    if refusal is not None:
        result = json_result(
            success=False,
            exit_code=EXIT_INVALID_OPTION,
            stderr=f"ssh_execute: {refusal}\n",
            data={"failure_class": "invalid_option", "ssh_option_refused": refusal},
        )
        emit(result, as_json=as_json)
        return EXIT_INVALID_OPTION

    # SSHCORE-021: a non-positive budget is an argv error, not a timeout.
    invalid_timeout = _validate_timeouts(args)
    if invalid_timeout is not None:
        emit(invalid_timeout, as_json=as_json)
        return int(invalid_timeout["exit_code"])

    # FA-24: `--tail` only means something with `--follow` (it is implemented by
    # wrapping the command in a streaming `tail -n N -f`). Accepting it and
    # silently ignoring it made a truncated log look like the whole log.
    if args.tail is not None and not args.follow:
        result = json_result(
            success=False,
            exit_code=EXIT_ARGS,
            stderr=(
                "ssh_execute: --tail only applies to --follow; drop one of them "
                "(use `--tail N` with `--follow`, or pipe through `tail -n N` "
                "yourself for a one-shot read)\n"
            ),
            data={
                "route": "remote",
                "failure_class": "invalid_arguments",
                "unsupported_option": "--tail",
                "requires": "--follow",
            },
        )
        emit(result, as_json=as_json)
        return EXIT_ARGS

    # FA-21: a missing --ssh-config is a local configuration problem, not a
    # remote failure. ssh would exit 255 with "Can't open user config file",
    # which used to be classified `remote_error` (104).
    config_problem = ssh_config_path_problem(args.ssh_config)
    if config_problem is not None:
        result = json_result(
            success=False,
            exit_code=EXIT_CONFIG_MISSING,
            stderr=f"ssh_execute: {config_problem}\n",
            data={
                "failure_class": "config_missing",
                "ssh_config": args.ssh_config,
            },
        )
        emit(result, as_json=as_json)
        return EXIT_CONFIG_MISSING

    host_arg, inline_port = split_target(args.host)
    port = args.port if args.port is not None else inline_port

    if host_arg == "local":
        if commands:
            result = json_result(
                success=False,
                exit_code=EXIT_ARGS,
                stderr="ssh_execute: --command / --commands-file need a remote host\n",
                data={"failure_class": "invalid_arguments"},
            )
        elif getattr(args, "sudo", False) or getattr(args, "sudo_password_env", None):
            result = json_result(
                success=False,
                exit_code=EXIT_ARGS,
                stderr=(
                    "ssh_execute: --sudo only applies to the remote route; the "
                    "local route already runs with your own privileges.\n"
                ),
                data={
                    "route": "local",
                    "failure_class": "invalid_arguments",
                    "unsupported_option": "--sudo",
                },
            )
        else:
            result = _run_local(
                args.command or "", args.timeout, stdin_text=stdin_bytes,
                live=not as_json,
            )
    elif args.control_master == "auto" and is_windows():
        result = json_result(
            success=False,
            exit_code=EXIT_DEPENDENCY,
            stderr=(
                "ssh_execute: --control-master is not supported by OpenSSH for "
                "Windows (ControlPath needs a POSIX socket). Drop the flag, or "
                "pass several commands with --commands-file: they already run "
                "over a single connection.\n"
            ),
            data={
                "route": "remote",
                "failure_class": "dependency_missing",
                "unsupported_option": "--control-master",
                "alternative": "--commands-file",
            },
        )
    else:
        auth = _resolve_auth(host_arg, args.auth, args.ssh_config)
        password_value = (
            os.environ.get(args.password_env, "") if auth == "password" else None
        )
        # `_run_remote_with_retry` -> `_run_remote` needs the secret to build an
        # askpass-backed child; it is attached here (after resolution, never for
        # the key route) and never enters argv.
        args._password = password_value
        if auth == "password" and not password_value:
            result = json_result(
                success=False,
                exit_code=EXIT_PASSWORD_REQUIRED,
                stderr=(
                    f"ssh_execute: {host_arg} requires password auth but "
                    f"${args.password_env} is not set. Export it (never pass "
                    "passwords on the command line), or use ssh_session.py for "
                    "interactive sessions.\n"
                ),
                data={
                    "route": "remote",
                    "auth_method": "password",
                    "failure_class": "password_required",
                },
            )
        elif commands:
            result = _run_batch(
                host_arg, commands, args.timeout, args.connect_timeout,
                args.ssh_config,
                ssh_options=list(args.ssh_option),
                port=port,
                identity=args.identity,
                accept_new_hostkey=args.accept_new_hostkey,
                password=password_value,
            )
        elif auth == "password" and _is_network_device(host_arg, args.ssh_config):
            # A device CLI genuinely needs the tty; the exec channel cannot hold
            # view state. It is a separate route with its own (merged-stream)
            # contract rather than the default for every password host.
            result = _run_password_session(
                host_arg, args.command or "", args.timeout,
                args.connect_timeout, args.ssh_config,
                ssh_options=list(args.ssh_option),
                accept_new_hostkey=args.accept_new_hostkey,
                port=port,
                identity=args.identity,
                password=password_value,
                stdin_text=stdin_bytes,
            )
        else:
            result = _run_remote_with_retry(
                host_arg, args.command or "", args, port,
            )

    _audit(args, result)
    redactor = redactor_for(host_arg, args.ssh_config, enabled=args.redact)
    emit(
        result,
        as_json=as_json,
        redactor=redactor,
        # Human mode already relayed the bytes while the command ran
        # (SSHCORE-025); printing them again would duplicate and reorder.
        streamed=bool((result.get("data") or {}).get("streamed")),
    )
    return int(result["exit_code"])


def _run_remote_with_retry(
    host: str,
    command: str,
    args: argparse.Namespace,
    port: int | None,
) -> dict:
    """Single-command remote route: sudo wrapper → follow → retry (DV-14/17/18).

    Ordering matters:

    1. ``--sudo`` rewrites the command and runs its own non-interactive
       preflight *before* anything else, so an escalation problem never looks
       like a broken command and never gets retried (a NOPASSWD misconfig is
       not transient).
    2. ``--follow`` takes over the connection and streams until told to stop.
    3. Otherwise the normal capture path runs, wrapped in the retry policy.

    ``stdin_text`` is inherited by every attempt: the same input feeds the same
    command, so a retried attempt behaves like the first one.
    """
    spec: SudoSpec | None = parse_sudo_root(args)
    # Bound *before* the branch: ``_attempt`` below closes over it, and a name
    # that is only assigned inside an ``if`` raises NameError (not UnboundLocal-
    # Error) from the closure when the branch is skipped — which is every plain
    # invocation without --sudo.
    sudo_password = ""
    if spec is not None and spec.enabled:
        if getattr(args, "sudo_password_env", None):
            sudo_password = os.environ.get(args.sudo_password_env, "")
            if not sudo_password:
                return json_result(
                    success=False,
                    exit_code=EXIT_SUDO_REQUIRED,
                    stderr=(
                        f"ssh_execute: --sudo-password-env "
                        f"{args.sudo_password_env} is not set in this process.\n"
                    ),
                    data={
                        "route": "remote",
                        "failure_class": "sudo_required",
                        "sudo": spec.as_dict(),
                    },
                )
            if args.stdin_text is not None or args.commands:
                return json_result(
                    success=False,
                    exit_code=EXIT_ARGS,
                    stderr=(
                        "ssh_execute: --sudo-password-env cannot be combined "
                        "with --stdin-text or --commands-file: stdin carries "
                        "the sudo password, so it cannot also carry your data.\n"
                    ),
                    data={
                        "route": "remote",
                        "failure_class": "invalid_arguments",
                        "sudo": spec.as_dict(),
                    },
                )
        preflight = _sudo_preflight(host, args, port, spec, sudo_password)
        if preflight is not None:
            return preflight
        command = sudo_wrap(
            command, spec, use_stdin_password=bool(sudo_password)
        )

    if getattr(args, "follow", False):
        result = _run_follow(
            host, command, args, port,
            redactor=redactor_for(
                host, args.ssh_config, enabled=getattr(args, "redact", True)
            ),
            password=getattr(args, "_password", None),
        )
        if spec is not None and spec.enabled:
            result.setdefault("data", {})["sudo"] = spec.as_dict()
        return result

    def _attempt(_n: int) -> dict:
        return _run_remote(
            host, command, args.timeout, args.connect_timeout,
            args.ssh_config,
            control_master=args.control_master,
            control_persist=args.control_persist,
            control_path_dir=args.control_path_dir,
            ssh_options=list(args.ssh_option),
            accept_new_hostkey=args.accept_new_hostkey,
            port=port,
            identity=args.identity,
            stdin_text=(
                (sudo_password + "\n")
                if sudo_password
                else getattr(args, "_stdin_bytes", args.stdin_text)
            ),
            # Human mode relays while the command runs (SSHCORE-025). A retry
            # attempt that fails before producing output stays silent, so the
            # transcript is still written exactly once.
            live=not getattr(args, "json", False),
            redactor=redactor_for(host, args.ssh_config, enabled=getattr(args, "redact", True)),
            password=getattr(args, "_password", None),
        )

    policy = policy_from_args(args)
    result = retrying(_attempt, policy) if policy.enabled else _attempt(1)
    data = result.setdefault("data", {})
    if spec is not None and spec.enabled:
        data["sudo"] = spec.as_dict()
        sudo_class = classify_sudo(result.get("stderr") or "")
        if sudo_class is not None and not result.get("success"):
            data["failure_class"] = sudo_class
            result["exit_code"] = exit_code_for(sudo_class)
            result["stderr"] = (
                (result.get("stderr") or "")
                + ("\n" if result.get("stderr") else "")
                + remediation(spec)
                + "\n"
            )
    return result


def parse_sudo_root(args: argparse.Namespace) -> SudoSpec | None:
    """Build the :class:`SudoSpec` for this call (``None`` when not requested)."""
    if not getattr(args, "sudo", False) and not getattr(
        args, "sudo_password_env", None
    ):
        return None
    return SudoSpec(
        user=getattr(args, "sudo_user", "root") or "root",
        password_env=getattr(args, "sudo_password_env", None),
        preserve_env=bool(getattr(args, "sudo_preserve_env", False)),
    )


def _sudo_preflight(
    host: str,
    args: argparse.Namespace,
    port: int | None,
    spec: SudoSpec,
    sudo_password: str,
) -> dict | None:
    """Prove ``sudo`` can escalate without running the caller's command.

    Returns a failure envelope when it cannot, ``None`` when it can. The probe
    runs the *same* route (key or password auth, same port/identity/options) so
    a preflight success really predicts the real call.
    """
    probe = probe_command(spec, use_stdin_password=bool(sudo_password))
    result = _run_remote(
        host, probe, max(20, args.connect_timeout + 10), args.connect_timeout,
        args.ssh_config,
        ssh_options=list(args.ssh_option),
        accept_new_hostkey=args.accept_new_hostkey,
        port=port,
        identity=args.identity,
        stdin_text=(sudo_password + "\n") if sudo_password else None,
        # The preflight must use the same auth profile as the real call, or a
        # password host answers `batch mode` / publickey and the operator is
        # sent to edit sudoers for a host they cannot even log in to.
        password=getattr(args, "_password", None),
    )
    if result.get("success"):
        return None
    raw = (result.get("data") or {}).get("raw_stderr") or result.get("stderr") or ""
    sudo_class = classify_sudo(raw)
    if sudo_class is None:
        # FA-15: a dead host, a wrong key or an unknown host key is *not* a
        # sudo problem. The old `or "sudo_required"` rewrote `network`(102)
        # and `auth`(100) into 112, sending the operator to edit sudoers for a
        # host that is not even reachable.
        original = (result.get("data") or {}).get("failure_class")
        inherited = json_result(
            success=False,
            exit_code=int(result.get("exit_code") or EXIT_FAIL),
            stderr=(
                f"ssh_execute: sudo preflight could not reach {host} "
                f"({original or 'unclassified'}); the command was not run.\n"
                + (result.get("stderr") or "")
            ),
            data={
                "route": "remote",
                "failure_class": original or "remote_error",
                "sudo": spec.as_dict(),
                "sudo_preflight": "unreachable",
                "sudo_probe": probe,
                "command_not_run": True,
            },
        )
        return inherited
    return json_result(
        success=False,
        exit_code=exit_code_for(sudo_class),
        stderr=(
            f"ssh_execute: sudo preflight failed on {host} "
            f"({sudo_class}).\n"
            + remediation(spec)
            + "\n"
        ),
        data={
            "route": "remote",
            "failure_class": sudo_class,
            "sudo": spec.as_dict(),
            "sudo_preflight": "failed",
            "sudo_probe": probe,
            "command_not_run": True,
        },
    )


def _run_follow(
    host: str,
    command: str,
    args: argparse.Namespace,
    port: int | None,
    *,
    redactor: Any = None,
    password: str | None = None,
) -> dict:
    """Stream the command's output until Ctrl-C, EOF or ``--timeout`` (DV-18).

    The whole point is that output appears *while* it is produced, which rules
    out the usual bounded-capture path. So:

    * the child keeps running and its stdout/stderr are relayed line by line;
    * ``--json`` sends the live transcript to **stderr**, because stdout is
      reserved for the single JSON document that is printed at the end;
    * Ctrl-C tears the child down and still produces that document (bounded by
      ``MAX_STREAM_BYTES``), carrying ``data.interrupted: true`` and
      ``exit_code: 130`` — the shell convention for SIGINT.

    ``--tail N`` is implemented by *wrapping* the command, not by appending a
    pipe (FA-06). ``tail -f file | tail -n 50 -f`` never flushes: the first
    ``tail`` sees a pipe rather than a tty, buffers in 4 KiB blocks, and a slow
    log therefore stays invisible forever. ``stdbuf -oL -eL sh -c '<cmd>'``
    forces line buffering so the last N lines appear immediately.
    """
    tail = getattr(args, "tail", None)
    if tail is not None:
        if tail < 0:
            return json_result(
                success=False,
                exit_code=EXIT_ARGS,
                stderr="ssh_execute: --tail needs a non-negative line count\n",
                data={"failure_class": "invalid_arguments"},
            )
        # `tail -f` never reaches EOF on its own, which is exactly what
        # --follow wants; filtering through it also gives --tail its meaning
        # (start with the last N lines, then keep streaming).
        #
        # `stdbuf -oL -eL` is the fix, not decoration: as a plain pipeline the
        # producer's stdout is a pipe, so libc switches to 4 KiB block
        # buffering and a slow log never appears. The `command -v` guard keeps
        # hosts without coreutils' stdbuf working via the plain fallback.
        inner = shlex.quote(command)
        plain = f"sh -c {inner} | tail -n {int(tail)} -f"
        command = (
            f"if command -v stdbuf >/dev/null 2>&1; then "
            f"stdbuf -oL -eL sh -c {inner} | tail -n {int(tail)} -f; "
            f"else {plain}; fi"
        )

    argv = _build_remote_argv(
        host, command, args.connect_timeout, args.ssh_config,
        control_master=args.control_master,
        control_persist=args.control_persist,
        control_path_dir=args.control_path_dir,
        ssh_options=list(args.ssh_option),
        port=port,
        identity=args.identity,
        accept_new_hostkey=args.accept_new_hostkey,
        # A password host streams through the same askpass-backed exec channel
        # as the capture path (SSHCORE-026 follow-up); `-tt` would merge the
        # streams and there would be nothing left to filter.
        password_auth=bool(password),
    )
    sink = BoundedCapture()
    live = sys.stderr if args.json else sys.stdout
    # SSHCORE-017/025: the live line relay is also the terminal's view, so the
    # login banner has to be suppressed there too, and in human mode the
    # envelope must not print the transcript a second time. Prefer the binary
    # buffer (byte-exact); fall back to the text stream when there is none
    # (a redirected StringIO, e.g. under a test harness).
    live_target = getattr(live, "buffer", None) or live
    live_tee = _StreamTee(live_target, redactor, suppress_login_banner=True)
    started = time.monotonic()
    interrupted = False
    timed_out = False
    returncode = EXIT_OK
    env = msys_safe_env()
    askpass = None
    if password:
        askpass = askpass_env(password)
        env = askpass.__enter__()
    try:
        proc = subprocess.Popen(
            argv,
            # A closed pipe is an EOF the client can observe; NUL/DEVNULL is
            # not (see `_capture_process`).
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **CHILD_TEXT_KWARGS,
            env=env,
        )
    except OSError as exc:
        if askpass is not None:
            askpass.__exit__(None, None, None)
        return json_result(
            success=False,
            exit_code=EXIT_DEPENDENCY,
            stderr=f"failed to spawn ssh: {exc}",
            data={
                "route": "remote",
                "follow": True,
                "ssh_argv": argv,
                "failure_class": "dependency_missing",
            },
        )
    if getattr(proc, "stdin", None) is not None:
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()  # type: ignore[union-attr]

    def _relay() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            sink.feed(line)
            live_tee.feed(line)

    reader = threading.Thread(target=_relay, daemon=True)
    reader.start()
    deadline = started + args.timeout
    try:
        while True:
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                proc.kill()
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        interrupted = True
        with contextlib.suppress(Exception):
            proc.kill()
    finally:
        with contextlib.suppress(Exception):
            returncode = proc.wait(timeout=5)
        reader.join(timeout=5)
        live_tee.close()
        if askpass is not None:
            askpass.__exit__(None, None, None)

    elapsed = round(time.monotonic() - started, 3)
    raw_transcript = sink.text()
    # SSHCORE-017: `--follow` merges stderr into stdout, so sshd's greeting is
    # the first thing in the transcript. It is transport noise, not log content.
    transcript, banner = split_login_banner(raw_transcript)
    data: dict = {
        "route": "remote",
        "ssh_argv": argv,
        "elapsed_s": elapsed,
        "follow": True,
        "lines": transcript.count("\n"),
        "interrupted": interrupted,
        "truncated": sink.truncated,
        "bytes_elided": sink.bytes_elided,
        # The live transcript already reached the terminal in human mode.
        "merged_stream": True,
        **({"streamed": True} if not args.json else {}),
        **({"ssh_banner_lines": banner, "raw_stdout": raw_transcript} if banner else {}),
    }
    if timed_out:
        data["failure_class"] = "timeout"
        return json_result(
            success=False,
            exit_code=EXIT_TIMEOUT,
            stdout=transcript,
            stderr=f"follow timed out after {args.timeout}s",
            data=data,
        )
    if interrupted:
        data["failure_class"] = None
        return json_result(
            success=True,
            exit_code=130,
            stdout=transcript,
            stderr="",
            data=data,
        )
    normalised, signal_number = normalized_exit_code(returncode)
    if normalised != returncode:
        data["raw_returncode"] = returncode
    if signal_number is not None:
        data["terminated_by_signal"] = signal_number
    returncode = normalised
    failure_class = classify_failure(returncode, transcript)
    data["failure_class"] = failure_class
    return json_result(
        success=(returncode == 0),
        exit_code=(
            _CLASS_CODES[failure_class]
            if returncode == 255 and failure_class in _CLASS_CODES
            else returncode
        ),
        stdout=transcript,
        stderr="" if returncode == 0 else transcript.splitlines()[-1]
        if transcript.strip()
        else f"ssh exited {returncode}",
        data=data,
    )


if __name__ == "__main__":
    sys.exit(main())
