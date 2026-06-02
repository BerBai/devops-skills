#!/usr/bin/env python3
"""ssh_execute.py — run a command on a remote host.

Public surface:
    python ssh_execute.py <host> "<command>" \
        [--timeout N] [--connect-timeout N] [--ssh-config PATH] \
        [--control-master off|auto] [--control-persist N] \
        [--control-path-dir PATH] [--json]

When ``host == "local"`` the command is run via ``shlex.split`` +
``subprocess.run`` with **no shell**. Shell metacharacters
(``|``, ``>``, ``<``, ``;``, ``&&``, ``||``, `` ` ``, ``$(``) are refused
with a clear error so users do not silently get the wrong semantics; pass
``bash -c '<your command>'`` to opt into a shell.

Otherwise we spawn native ``ssh`` via subprocess:
    ssh -o BatchMode=yes -o ConnectTimeout=N [-F config] <host> <command>

By default each call is a fresh SSH subprocess, preserving the v1.0
contract. For v1.1 connection reuse, callers may opt into OpenSSH
ControlMaster with ``--control-master auto``.

The remote command's exit code is propagated to the top-level
``exit_code`` field. ``data.failure_class`` (one of ``None`` / ``network``
/ ``auth`` / ``remote_error`` / ``timeout``) further disambiguates
non-success outcomes for callers.

See:
    .trellis/spec/backend/adr-001-ssh-execute.md   — design contract
    .trellis/spec/backend/error-handling.md         — JSON output rules
"""

from __future__ import annotations

import argparse
import hashlib
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    classify_failure,
    emit,
    filter_ssh_noise,
    json_result,
    msys_safe_env,
)

# Shell metacharacters that the `local` route refuses, per ADR-001 D7.
# Order matters for diagnostics: longer compound operators first so the
# error message names "&&" rather than "&".
SHELL_METACHARS: tuple[str, ...] = ("&&", "||", "$(", "|", ">", "<", ";", "`")
CONTROL_MASTER_DEFAULT = "off"
CONTROL_PERSIST_DEFAULT = 600
CONTROL_PATH_DIR_NAME = "ssh_controlmaster"
CONTROL_PATH_DIGEST_LEN = 24


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssh_execute.py",
        description=(
            "Run a command on a remote host (or 'local'). "
            "Emits the shared JSON contract with --json. See ADR-001."
        ),
    )
    p.add_argument(
        "host",
        help="Host alias from ~/.ssh/config, or the literal 'local'",
    )
    p.add_argument(
        "command",
        help="Command to run on the host (argv string)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Total wall-clock budget in seconds (default: 120)",
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=8,
        help="SSH connect timeout in seconds, applied as -o ConnectTimeout (default: 8)",
    )
    p.add_argument(
        "--ssh-config",
        default=None,
        help="Pass -F <path> to ssh (default: ~/.ssh/config)",
    )
    p.add_argument(
        "--control-master",
        choices=("off", "auto"),
        default=CONTROL_MASTER_DEFAULT,
        help="Opt into OpenSSH ControlMaster reuse for remote hosts (default: off)",
    )
    p.add_argument(
        "--control-persist",
        type=int,
        default=CONTROL_PERSIST_DEFAULT,
        help="ControlPersist seconds when --control-master=auto (default: 600)",
    )
    p.add_argument(
        "--control-path-dir",
        default=None,
        help="Directory for ControlMaster sockets (default: temp ssh_controlmaster dir)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the shared JSON output contract on stdout",
    )
    # v0.2 backwards-compat: --no-daemon was a stub flag; v1.0 has no
    # daemon (ADR-001 D1), so the flag is a silent no-op kept solely to
    # avoid breaking callers that learned the v0.2 argv surface.
    p.add_argument(
        "--no-daemon",
        action="store_true",
        help=argparse.SUPPRESS,
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
    base.chmod(0o700)
    digest_input = f"{host}\0{ssh_config or ''}".encode()
    digest = hashlib.sha256(digest_input).hexdigest()[:CONTROL_PATH_DIGEST_LEN]
    return str(base / digest)


def _build_remote_argv(
    host: str,
    command: str,
    connect_timeout: int,
    ssh_config: str | None,
    *,
    control_master: str = CONTROL_MASTER_DEFAULT,
    control_persist: int = CONTROL_PERSIST_DEFAULT,
    control_path_dir: str | None = None,
) -> list[str]:
    """Build the native ssh argv without spawning a subprocess."""
    argv: list[str] = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    if ssh_config:
        argv += ["-F", ssh_config]
    if control_master == "auto":
        argv += [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPersist={control_persist}",
            "-o",
            f"ControlPath={_control_path(host, ssh_config, control_path_dir)}",
        ]
    argv += [host, command]
    return argv


def _run_local(command: str, timeout: int) -> dict:
    """Local route: shlex split, no shell, refuses shell metacharacters."""
    for meta in SHELL_METACHARS:
        if meta in command:
            return json_result(
                success=False,
                exit_code=1,
                stderr=(
                    f"local route does not support shell metacharacter '{meta}'. "
                    "Use `bash -c '<your command>'` to opt into a shell."
                ),
                data={"route": "local", "refused_metachar": meta},
            )

    try:
        argv = shlex.split(command)
    except ValueError as e:
        return json_result(
            success=False,
            exit_code=1,
            stderr=f"shlex parse error: {e}",
            data={"route": "local"},
        )

    if not argv:
        return json_result(
            success=False,
            exit_code=1,
            stderr="empty command after shlex split",
            data={"route": "local"},
        )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=msys_safe_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return json_result(
            success=False,
            exit_code=124,
            stderr=f"timed out after {timeout}s",
            data={
                "route": "local",
                "shlex_argv": argv,
                "elapsed_s": float(timeout),
                "failure_class": "timeout",
            },
        )
    except FileNotFoundError as e:
        return json_result(
            success=False,
            exit_code=127,
            stderr=f"command not found: {e}",
            data={
                "route": "local",
                "shlex_argv": argv,
                "failure_class": "remote_error",
            },
        )

    elapsed = round(time.monotonic() - start, 3)
    return json_result(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        data={
            "route": "local",
            "shlex_argv": argv,
            "elapsed_s": elapsed,
            "failure_class": None if proc.returncode == 0 else "remote_error",
        },
    )


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
) -> dict:
    """Remote route: subprocess + native ssh, capture everything."""
    argv = _build_remote_argv(
        host,
        command,
        connect_timeout,
        ssh_config,
        control_master=control_master,
        control_persist=control_persist,
        control_path_dir=control_path_dir,
    )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=msys_safe_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return json_result(
            success=False,
            exit_code=124,
            stderr=f"timed out after {timeout}s",
            data={
                "route": "remote",
                "ssh_argv": argv,
                "elapsed_s": float(timeout),
                "failure_class": "timeout",
            },
        )

    elapsed = round(time.monotonic() - start, 3)
    real_stderr, noise = filter_ssh_noise(proc.stderr)
    failure_class = classify_failure(proc.returncode, proc.stderr)
    return json_result(
        success=(proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=real_stderr,
        data={
            "route": "remote",
            "ssh_argv": argv,
            "elapsed_s": elapsed,
            "ssh_noise_lines": noise,
            "failure_class": failure_class,
            "raw_stderr": proc.stderr,
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host == "local":
        result = _run_local(args.command, args.timeout)
    else:
        result = _run_remote(
            args.host,
            args.command,
            args.timeout,
            args.connect_timeout,
            args.ssh_config,
            control_master=args.control_master,
            control_persist=args.control_persist,
            control_path_dir=args.control_path_dir,
        )
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
