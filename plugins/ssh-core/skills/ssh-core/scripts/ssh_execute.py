#!/usr/bin/env python3
"""ssh_execute.py — run a command on a remote host (v1.0).

Public surface:
    python ssh_execute.py <host> "<command>" \
        [--timeout N] [--connect-timeout N] [--ssh-config PATH] [--json]

When ``host == "local"`` the command is run via ``shlex.split`` +
``subprocess.run`` with **no shell**. Shell metacharacters
(``|``, ``>``, ``<``, ``;``, ``&&``, ``||``, `` ` ``, ``$(``) are refused
with a clear error so users do not silently get the wrong semantics; pass
``bash -c '<your command>'`` to opt into a shell.

Otherwise we spawn native ``ssh`` via subprocess:
    ssh -o BatchMode=yes -o ConnectTimeout=N [-F config] <host> <command>

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
import shlex
import subprocess
import sys
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
) -> dict:
    """Remote route: subprocess + native ssh, capture everything."""
    argv: list[str] = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
    ]
    if ssh_config:
        argv += ["-F", ssh_config]
    argv += [host, command]

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
        )
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
