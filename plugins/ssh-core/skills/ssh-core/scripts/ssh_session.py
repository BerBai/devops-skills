#!/usr/bin/env python3
"""ssh_session.py — interactive SSH sessions (password auth + device CLIs).

Why this script exists
----------------------
OpenSSH reads passwords from a **controlling terminal**, never from a pipe.
``subprocess`` + ``PIPE`` therefore cannot log in to password-only hosts
(network devices, fresh servers). Two transports solve that, and this script
now picks whichever the platform offers (SSH-09):

* POSIX → ``pty.fork`` (a real controlling tty).
* Windows / no tty → ``ssh -tt`` with ``SSH_ASKPASS`` (``lib.askpass``).

Network devices (VRP / Comware / IOS) additionally need an *interactive*
session: paging must be disabled once, commands depend on view state, and
``save`` asks ``[Y/N]``.

Public surface::

    ssh_session.py <host> --command "<cmd>" [--command "<cmd>" ...]
        [--command-file PATH]        # one command per line, '#' comments
        [--prompt-regex REGEX]       # prompt to wait for (default: heuristic)
        [--disable-paging CMD]       # e.g. "screen-length 0 temporary"
        [--confirm ask|yes|no]       # answer [Y/N] prompts (default: ask)
        [--connect-timeout N] [--timeout N] [--step-timeout N]
        [--force-pty|--no-pty]       # pick the transport explicitly
        [--ssh-config PATH] [-o KEY=VALUE ...]
        [--password-env VAR]         # default SSH_PASSWORD
        [--accept-new-hostkey]       # StrictHostKeyChecking=accept-new
        [--audit-log | --no-audit]   # default: audit on
        [--snapshot-cmd CMD] [--snapshot-dir DIR]
        [--json] [--version]

Security invariants
-------------------
* The password is read from an environment variable (default
  ``SSH_PASSWORD``); it never enters argv, the JSON envelope, stderr, or the
  audit log. Any accidental terminal echo is scrubbed from captured output.
* Host keys are strict by default (``StrictHostKeyChecking=yes``); opt in to
  first-touch trust with ``--accept-new-hostkey``.
* ``-o`` options go through :func:`lib.validate_ssh_option`.
* ``HostName`` / ``User`` / ``IdentityFile`` are redacted from the envelope
  (SSH-07).

Failure classes: ``password_required``, ``auth``, ``host_key``, ``kex``,
``network``, ``timeout``, ``confirmation_required``, ``prompt_timeout``,
``remote_error``, ``invalid_option``, ``dependency_missing``.

Verified against real hardware (2026-10-30): OpenSSH 8.2 on Kylin 3
(192.168.30.240) for key/password/auth/host_key/kex/network, and a Huawei VRP
switch SSH-1.99 (192.168.30.188) for the chained password prompt.

See:
    references/workflows.md §6b — network-device session recipe
    references/troubleshooting.md — password/paging/confirmation failures
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import time
from pathlib import Path

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    append_audit,
    audit_cli_flags,
    audit_enabled,
    audit_log_path,
    classify_failure,
    emit,
    json_result,
    merged_ssh_config,
    read_ssh_config_metadata,
    read_text_auto,
    validate_ssh_option,
)
from lib.cli import SSHArgumentParser, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_CONFIRMATION_REQUIRED,
    EXIT_DEPENDENCY_MISSING,
    EXIT_HOST_KEY,
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_INVALID_OPTION,
    EXIT_KEX,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_PASSWORD_REQUIRED,
    EXIT_PROMPT_TIMEOUT,
    EXIT_REMOTE_ERROR,
    EXIT_TIMEOUT,
)
from lib.redact import redactor_for  # noqa: E402
from lib.session import (  # noqa: E402
    AUTH_FAILED_RE,
    CONFIRM_PROMPT_RE,
    DEFAULT_PROMPT_RE,
    DEFAULT_STEP_TIMEOUT,
    HOSTKEY_PROMPT_RE,
    PASSWORD_PROMPT_RE,
    GRACEFUL_CLOSE_RE,
    PipeSession,
    PtySession,
    SessionError,
    authenticate,
    closed_by_device,
    leave_session,
    make_session,
    records_show_failure,
    run_script,
    snapshot_path,
    supports_pty,
    wait_for_first_prompt,
)

# Backwards-compatible aliases (older callers imported these names).
PtySessionError = SessionError


def _scrub(text: str, secret: str) -> str:
    from lib.askpass import scrub_secret

    return scrub_secret(text, secret)


def _last_prompt(output: str) -> str:
    from lib.session import last_prompt

    return last_prompt(output)


_closed_by_device = closed_by_device
_leave_session = leave_session
_run_script = run_script
_wait_for_first_prompt = wait_for_first_prompt


def build_ssh_argv(
    host: str,
    command: str | None,
    connect_timeout: int,
    ssh_config: str | None,
    *,
    ssh_options: list[str] | None = None,
    accept_new_hostkey: bool = False,
    interactive: bool = False,
) -> list[str]:
    """Compose the ssh argv for a password-auth session."""
    argv: list[str] = [
        "ssh",
        # BatchMode would abort at the first prompt — exactly what we must
        # avoid here. Password auth is explicit; pubkey is disabled so a
        # stray agent key cannot consume the server's auth attempts.
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "StrictHostKeyChecking=" + ("accept-new" if accept_new_hostkey else "yes"),
        "-o", "UpdateHostKeys=no",
    ]
    if interactive:
        argv += ["-o", "RequestTTY=force"]
    ssh_config_path = merged_ssh_config(ssh_config)
    if ssh_config_path:
        argv += ["-F", ssh_config_path]
    for option in ssh_options or []:
        argv += ["-o", option]
    argv.append("--")
    argv.append(host)
    if command is not None:
        argv.append(command)
    return argv


def _validate_options(options: list[str]) -> str | None:
    for option in options:
        reason = validate_ssh_option(option)
        if reason is not None:
            return reason
    return None


def _public_argv(argv: list[str]) -> list[str]:
    """argv safe for the JSON envelope — no password ever lands here."""
    return list(argv)


# --- one-shot exec through a session ---------------------------------------


def execute_single(
    host: str,
    command: str,
    *,
    timeout: int = 120,
    connect_timeout: int = 8,
    ssh_config: str | None = None,
    ssh_options: list[str] | None = None,
    password_env: str = "SSH_PASSWORD",
    accept_new_hostkey: bool = False,
) -> dict:
    """Run one command through a password-auth session.

    ``device-type: network`` hosts get a forced tty and a prompt wait; Linux
    hosts use the exec channel. On Windows the transport is the ``ssh -tt``
    pipe (:class:`lib.session.PipeSession`), so the whole flow works there too.
    """
    password = os.environ.get(password_env)
    if not password:
        return json_result(
            success=False,
            exit_code=EXIT_PASSWORD_REQUIRED,
            stderr=(
                f"ssh_session: ${password_env} is not set. Export the password "
                "in the environment; never put it on the command line.\n"
            ),
            data={
                "route": "remote",
                "auth_method": "password",
                "failure_class": "password_required",
            },
        )
    meta = read_ssh_config_metadata(host, ssh_config)
    is_network = meta.get("device-type", "").lower() == "network"
    argv = build_ssh_argv(
        host,
        # A network device must be driven through an interactive shell: with a
        # remote command and RequestTTY, ssh runs it and exits immediately, so
        # the session closes before the driver can read the prompt.
        None if is_network else command,
        connect_timeout,
        ssh_config,
        ssh_options=ssh_options,
        accept_new_hostkey=accept_new_hostkey,
        interactive=is_network,
    )
    session = make_session(argv, password, prefer_pty=supports_pty())
    start = time.monotonic()
    records: list[dict] = []
    output = ""
    try:
        session.start()
        authenticate(
            session,
            timeout=min(timeout, connect_timeout + DEFAULT_STEP_TIMEOUT),
            accept_new_hostkey=accept_new_hostkey,
            host=host,
        )
        if is_network:
            wait_for_first_prompt(
                session,
                DEFAULT_PROMPT_RE,
                timeout=min(timeout, DEFAULT_STEP_TIMEOUT),
            )
            session.consume()  # drop banner + echoed login prompt
            records = run_script(
                session,
                [command],
                prompt_re=DEFAULT_PROMPT_RE,
                confirm="ask",  # no --confirm here: never answer [Y/N] blindly
                step_timeout=DEFAULT_STEP_TIMEOUT,
                deadline=time.monotonic() + timeout,
                capture_output=True,
            )
            leave_session(session, timeout=min(5.0, DEFAULT_STEP_TIMEOUT))
            output = records[0].pop("output", "") if records else ""
        else:
            deadline = time.monotonic() + timeout
            while not session.is_closed() and time.monotonic() < deadline:
                session.read_available(0.2)
            output = _scrub(session.consume(), password)
    except SessionError as e:
        elapsed = round(time.monotonic() - start, 3)
        return json_result(
            success=False,
            exit_code=e.exit_code,
            stderr=e.message + "\n",
            data={
                "route": "remote",
                "auth_method": "password",
                "transport": getattr(session, "kind", "pipe"),
                "ssh_argv": _public_argv(argv),
                "elapsed_s": elapsed,
                "failure_class": e.failure_class,
            },
        )
    except OSError as e:
        elapsed = round(time.monotonic() - start, 3)
        return json_result(
            success=False,
            exit_code=EXIT_DEPENDENCY_MISSING,
            stderr=f"ssh_session: failed to spawn ssh: {e}\n",
            data={
                "route": "remote",
                "auth_method": "password",
                "ssh_argv": _public_argv(argv),
                "elapsed_s": elapsed,
                "failure_class": "dependency_missing",
            },
        )
    finally:
        graceful = getattr(session, "graceful_close", False)
        rc = session.close()

    elapsed = round(time.monotonic() - start, 3)
    closed_after_output = False
    if rc != 0 and not is_network and output and closed_by_device(output):
        # The device answered the command and then closed the channel, so ssh
        # reports 255 with no exit status. The output is complete.
        closed_after_output = True
        rc = EXIT_OK
    if rc != 0 and graceful and not records_show_failure(records, is_network=is_network):
        rc = EXIT_OK
    if rc == 0:
        return json_result(
            success=True,
            exit_code=EXIT_OK,
            stdout=output,
            data={
                "route": "remote",
                "auth_method": "password",
                "transport": getattr(session, "kind", "pipe"),
                "device_type": "network" if is_network else "server",
                "ssh_argv": _public_argv(argv),
                "elapsed_s": elapsed,
                "failure_class": None,
                "results": records,
                **({"device_closed_after_output": True} if closed_after_output else {}),
            },
        )
    stderr_tail = output if rc == 255 else ""
    failure_class = classify_failure(rc, output) or "remote_error"
    return json_result(
        success=False,
        exit_code=_class_code(failure_class, rc),
        stdout="" if rc == 255 else output,
        stderr=stderr_tail or f"ssh exited {rc}\n",
        data={
            "route": "remote",
            "auth_method": "password",
            "transport": getattr(session, "kind", "pipe"),
            "device_type": "network" if is_network else "server",
            "ssh_argv": _public_argv(argv),
            "elapsed_s": elapsed,
            "failure_class": failure_class,
        },
    )


def _class_code(failure_class: str | None, rc: int) -> int:
    from lib.exit_codes import exit_code_for

    if rc not in (0, 255):
        return rc  # remote command's own exit status
    return exit_code_for(failure_class, default=rc)


# --- snapshots --------------------------------------------------------------


def _run_snapshot(
    host: str,
    command: str,
    *,
    snapshot_dir: Path,
    tag: str,
    connect_timeout: int,
    step_timeout: float,
    ssh_config: str | None,
    ssh_options: list[str],
    password_env: str,
    accept_new_hostkey: bool,
) -> str | None:
    """Run one capture command in its own session and persist its output."""
    password = os.environ.get(password_env)
    if not password:
        return None
    argv = build_ssh_argv(
        host,
        command,
        connect_timeout,
        ssh_config,
        ssh_options=ssh_options,
        accept_new_hostkey=accept_new_hostkey,
        interactive=False,
    )
    session = make_session(argv, password, prefer_pty=supports_pty())
    try:
        session.start()
        authenticate(
            session,
            timeout=step_timeout + connect_timeout,
            accept_new_hostkey=accept_new_hostkey,
            host=host,
        )
        deadline = time.monotonic() + step_timeout
        while not session.is_closed() and time.monotonic() < deadline:
            session.read_available(0.2)
        output = _scrub(session.consume(), password)
    finally:
        session.close()
    path = snapshot_path(snapshot_dir, host, tag)
    path.write_text(output, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return str(path)


# --- CLI --------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_session.py",
        description=(
            "Interactive SSH session for password-only hosts and network-device "
            "CLIs. Password comes from $SSH_PASSWORD (or --password-env); it is "
            "never placed in argv. Uses a real pty on POSIX and `ssh -tt` + "
            "SSH_ASKPASS elsewhere."
        ),
    )
    p.add_argument("host", help="Host alias from ~/.ssh/config (or user@host)")
    p.add_argument(
        "--command", action="append", default=[], dest="commands",
        help="Command to send, in order; repeatable",
    )
    p.add_argument(
        "--command-file", default=None,
        help="File with one command per line ('#' comments and blanks skipped)",
    )
    p.add_argument(
        "--disable-paging", default=None,
        help='Sent first, e.g. "screen-length 0 temporary" (VRP) or "terminal length 0" (IOS)',
    )
    p.add_argument(
        "--prompt-regex", default=None,
        help="Regex for the device prompt (default: heuristic <...> / [...] / word>$#%%)",
    )
    p.add_argument(
        "--no-capture-output", action="store_true",
        help=(
            "Only report output_bytes per command instead of the transcript "
            "(smaller envelopes; the default returns data.results[].output)"
        ),
    )
    p.add_argument(
        "--confirm", choices=("ask", "yes", "no"), default="ask",
        help=(
            "How to answer [Y/N] prompts (default: ask → fail with "
            "confirmation_required so the user opts in explicitly)"
        ),
    )
    p.add_argument(
        "--connect-timeout", type=int, default=8,
        help="SSH ConnectTimeout in seconds (default: 8)",
    )
    p.add_argument(
        "--timeout", type=int, default=180,
        help="Whole-session budget in seconds (default: 180)",
    )
    p.add_argument(
        "--step-timeout", type=int, default=DEFAULT_STEP_TIMEOUT,
        help=f"Per-command prompt wait in seconds (default: {DEFAULT_STEP_TIMEOUT})",
    )
    p.add_argument(
        "--force-pty", dest="transport", action="store_const", const="pty",
        help="Require a POSIX pty (fails on Windows instead of using pipes)",
    )
    p.add_argument(
        "--no-pty", dest="transport", action="store_const", const="pipe",
        help="Use `ssh -tt` + SSH_ASKPASS even on POSIX",
    )
    p.add_argument("--ssh-config", default=None, help="Pass -F <path> to ssh")
    p.add_argument(
        "-o", "--ssh-option", action="append", default=[], metavar="KEY=VALUE",
        help="Extra `ssh -o` option; repeatable",
    )
    p.add_argument(
        "--password-env", default="SSH_PASSWORD",
        help="Environment variable holding the password (default: SSH_PASSWORD)",
    )
    p.add_argument(
        "--accept-new-hostkey", action="store_true",
        help="Opt-in: StrictHostKeyChecking=accept-new for first-touch trust",
    )
    p.add_argument(
        "--audit-log", dest="audit", action="store_true", default=True,
        help="Append a JSONL audit record per command (default: on)",
    )
    p.add_argument(
        "--no-audit", dest="audit", action="store_false",
        help="Disable the audit log",
    )
    p.add_argument(
        "--snapshot-cmd", default=None,
        help="Capture this command's output before and after the session",
    )
    p.add_argument(
        "--snapshot-dir", default=None,
        help="Where snapshots go (default: $TMPDIR/ssh-core/<md5(alias)>/)",
    )
    p.add_argument("--json", action="store_true", help="Emit the shared JSON contract")
    p.add_argument(
        "--version", "-V", action="store_true", help="Print the ssh-core version",
    )
    return p


def _load_commands(args: argparse.Namespace) -> list[str]:
    commands = list(args.commands)
    if args.command_file:
        # FA-03b: read_text_auto raises OSError on a missing/unreadable file;
        # the envelope must survive that (it used to be a raw traceback).
        try:
            text = read_text_auto(args.command_file)
        except OSError as exc:
            raise CommandFileError(
                f"ssh_session: cannot read --command-file "
                f"{Path(args.command_file).expanduser()}: {exc}"
            ) from exc
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                commands.append(stripped)
    return commands


class CommandFileError(Exception):
    """Raised when ``--command-file`` cannot be read (FA-03)."""


def _snapshot_dir(alias: str, override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    return audit_log_path(alias).parent / audit_log_path(alias).stem


def _make_session(
    argv: list[str], password: str, transport: str | None
) -> PtySession | PipeSession:
    if transport == "pty":
        if not supports_pty():
            raise SessionError(
                "dependency_missing",
                EXIT_DEPENDENCY_MISSING,
                "--force-pty needs POSIX pty support; drop the flag (the pipe "
                "transport drives the same state machine over `ssh -tt`).",
            )
        return PtySession(argv, password=password)
    if transport == "pipe":
        return PipeSession(argv, password=password)
    return make_session(argv, password, prefer_pty=supports_pty())


def _truncation_fields(session: object) -> dict:
    """``truncated`` / ``bytes_elided`` for the interactive routes (FA-18).

    ``lib.session._BoundedBuffer`` keeps the *tail* of the transcript and counts
    what it dropped, but ``ssh_session`` never read that count back — so a
    device that streamed more than 10 MB produced a complete-looking envelope
    while SKILL.md promised both keys. The daemon already reports them; this is
    the same contract for the session route (SSHCORE-022).
    """
    buffer = getattr(session, "_buf", None)
    elided = int(getattr(buffer, "bytes_elided", 0) or 0)
    return {"truncated": elided > 0, "bytes_elided": elided}


def run_session(args: argparse.Namespace) -> dict:
    password = os.environ.get(args.password_env)
    try:
        commands = _load_commands(args)
    except CommandFileError as exc:
        return json_result(
            success=False,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr=f"{exc}\n",
            data={
                "failure_class": "invalid_arguments",
                "command_file": str(args.command_file),
            },
        )
    meta = read_ssh_config_metadata(args.host, args.ssh_config)

    data: dict = {
        "route": "session",
        "auth_method": "password",
        "host": args.host,
        "device_type": meta.get("device-type", "server"),
        "commands": commands,
        "confirm": args.confirm,
    }

    if not commands and not args.disable_paging and not args.snapshot_cmd:
        return json_result(
            success=False,
            exit_code=EXIT_INVALID_ARGUMENTS,
            stderr="ssh_session: no --command, --command-file, or --disable-paging given\n",
            data={**data, "failure_class": "invalid_option"},
        )
    # FA-03c: an invalid --prompt-regex used to raise re.error out of main()
    # before any envelope existed. Compile it up front — *before* the password
    # check, because a malformed argument is the caller's first problem and must
    # not be hidden behind "SSH_PASSWORD is not set".
    if args.prompt_regex:
        try:
            prompt_re = re.compile(args.prompt_regex)
        except re.error as exc:
            return json_result(
                success=False,
                exit_code=EXIT_INVALID_OPTION,
                stderr=(
                    f"ssh_session: --prompt-regex is not a valid regular "
                    f"expression ({exc}): {args.prompt_regex}\n"
                ),
                data={
                    **data,
                    "failure_class": "invalid_option",
                    "prompt_regex": args.prompt_regex,
                },
            )
    else:
        prompt_re = DEFAULT_PROMPT_RE

    if not password:
        return json_result(
            success=False,
            exit_code=EXIT_PASSWORD_REQUIRED,
            stderr=(
                f"ssh_session: ${args.password_env} is not set. Export the password "
                "in the environment; never put it on the command line.\n"
            ),
            data={**data, "failure_class": "password_required"},
        )

    option_refusal = _validate_options(args.ssh_option)
    if option_refusal is not None:
        return json_result(
            success=False,
            exit_code=EXIT_INVALID_OPTION,
            stderr=f"ssh_session: {option_refusal}\n",
            data={**data, "failure_class": "invalid_option"},
        )

    snapshots: dict[str, str] = {}
    if args.snapshot_cmd:
        snap_dir = _snapshot_dir(args.host, args.snapshot_dir)
        before = _run_snapshot(
            args.host, args.snapshot_cmd,
            snapshot_dir=snap_dir, tag="before",
            connect_timeout=args.connect_timeout, step_timeout=args.step_timeout,
            ssh_config=args.ssh_config, ssh_options=args.ssh_option,
            password_env=args.password_env,
            accept_new_hostkey=args.accept_new_hostkey,
        )
        if before:
            snapshots["before"] = before

    argv = build_ssh_argv(
        args.host,
        None,
        args.connect_timeout,
        args.ssh_config,
        ssh_options=args.ssh_option,
        accept_new_hostkey=args.accept_new_hostkey,
        interactive=True,
    )
    # FA-03c: an invalid --prompt-regex used to raise re.error out of main()
    # before any envelope existed. It is compiled above, before the password
    # check, so a malformed argument is reported as such.
    start = time.monotonic()
    deadline = start + args.timeout
    records: list[dict] = []
    audit_on = audit_enabled(args.audit, default=True)

    try:
        session = _make_session(argv, password, getattr(args, "transport", None))
    except SessionError as e:
        return json_result(
            success=False,
            exit_code=e.exit_code,
            stderr=e.message + "\n",
            data={**data, "failure_class": e.failure_class},
        )

    try:
        session.start()
        authenticate(
            session,
            timeout=min(args.timeout, args.connect_timeout + args.step_timeout),
            accept_new_hostkey=args.accept_new_hostkey,
            host=args.host,
        )
        wait_for_first_prompt(
            session, prompt_re, timeout=min(args.step_timeout, args.timeout)
        )
        session.consume()

        script: list[str] = []
        if args.disable_paging:
            script.append(args.disable_paging)
        script.extend(commands)
        records = run_script(
            session, script,
            prompt_re=prompt_re,
            confirm=args.confirm,
            step_timeout=args.step_timeout,
            deadline=deadline,
            capture_output=not args.no_capture_output,
        )
        leave_session(session, timeout=min(5.0, args.step_timeout))
    except SessionError as e:
        elapsed = round(time.monotonic() - start, 3)
        if audit_on:
            append_audit(
                args.host,
                {
                    "host": args.host,
                    "route": "session",
                    "command": "<session>",
                    "exit_ok": False,
                    "failure_class": e.failure_class,
                    "elapsed_s": elapsed,
                    "session": True,
                },
            )
        return json_result(
            success=False,
            exit_code=e.exit_code,
            stderr=e.message + "\n",
            data={
                **data,
                "transport": getattr(session, "kind", "pipe"),
                "ssh_argv": _public_argv(argv),
                "elapsed_s": elapsed,
                "results": records,
                "failure_class": e.failure_class,
                **_truncation_fields(session),
            },
        )
    finally:
        graceful = getattr(session, "graceful_close", False)
        rc = session.close()

    elapsed = round(time.monotonic() - start, 3)
    if rc != 0 and graceful and not records_show_failure(records, is_network=True):
        rc = EXIT_OK

    if audit_on:
        # FA-37: the session record shape is documented as
        # host/route/command/commands/exit_ok/elapsed_s/session — it has no
        # exit_code, failure_class or bytes, and SKILL.md used to claim it did.
        append_audit(
            args.host,
            {
                "host": args.host,
                "route": "session",
                "command": "<session>",
                "commands": len(records),
                "exit_ok": rc == 0,
                "elapsed_s": elapsed,
                "session": True,
            },
        )

    if args.snapshot_cmd:
        snap_dir = _snapshot_dir(args.host, args.snapshot_dir)
        after = _run_snapshot(
            args.host, args.snapshot_cmd,
            snapshot_dir=snap_dir, tag="after",
            connect_timeout=args.connect_timeout, step_timeout=args.step_timeout,
            ssh_config=args.ssh_config, ssh_options=args.ssh_option,
            password_env=args.password_env,
            accept_new_hostkey=args.accept_new_hostkey,
        )
        if after:
            snapshots["after"] = after
    if snapshots:
        data["config_snapshot"] = snapshots

    if rc != 0:
        return json_result(
            success=False,
            exit_code=rc if rc not in (255,) else EXIT_REMOTE_ERROR,
            stderr=f"ssh exited {rc}\n",
            data={
                **data,
                "transport": getattr(session, "kind", "pipe"),
                "ssh_argv": _public_argv(argv),
                "elapsed_s": elapsed,
                "results": records,
                "failure_class": "remote_error",
                **_truncation_fields(session),
            },
        )
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        stdout="",
        data={
            **data,
            "transport": getattr(session, "kind", "pipe"),
            "ssh_argv": _public_argv(argv),
            "elapsed_s": elapsed,
            "results": records,
            "failure_class": None,
            **_truncation_fields(session),
        },
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_session.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    result = run_session(args)
    redactor = redactor_for(args.host, args.ssh_config)
    emit(result, as_json=args.json, redactor=redactor)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
