#!/usr/bin/env python3
"""tail_log.py - multi-host log tail with line prefixing (v1.0).

Public surface::

    tail_log.py <alias> <path> [--lines N] [--grep <regex>] [--json]
    tail_log.py <alias> --unit nginx [--lines N] [--since '5min ago'] [--json]
    tail_log.py --hosts a,b,c <path> [--lines N] [--grep <regex>] [--json]
        # concurrent pull, each line prefixed "<alias>| "

Behaviour
---------
Shells out to ``ssh-core``'s ``ssh_execute.py`` (NEVER imported - per
CONTRIBUTING.md line 29 the cross-plugin contract is CLI shell-out).
For each host runs one of two remote commands:

    path mode:  ``tail -n <N> <path>``
    unit mode:  ``journalctl -u <unit> -n <N> --no-pager [--since '...']``

Multi-host (``--hosts a,b,c``) runs the per-host fetch concurrently
through a ``ThreadPoolExecutor`` (cap 8). Each output line is prefixed
``<alias>| `` in the combined human stdout; the structured payload at
``data.hosts[<alias>].lines`` preserves the raw per-host lines without
prefix.

``--grep <regex>`` compiles a Python regex and filters lines AFTER they
arrive locally (so the remote command stays cheap). Invalid regex yields
exit 2.

``--since`` is only honoured in ``--unit`` mode (where journalctl
accepts free-form time expressions). In path mode it is silently
ignored with a one-line stderr warning.

``--follow`` is accepted for v0.2 compatibility but DEFERRED to v1.1:
the script writes a one-line stderr warning and falls back to a
``--lines N`` batch fetch.

Exit codes follow ``error-handling.md``::

    0    every host succeeded (remote exit 0)
    1    any host had non-zero remote exit or ssh_execute failure
    2    argv error (bad regex, missing path/unit)
    124  any ssh_execute subprocess timed out

See:
    .trellis/spec/backend/adr-001-ssh-execute.md   ssh_execute contract
    .trellis/spec/backend/error-handling.md         JSON envelope rules
    CONTRIBUTING.md line 29                          no cross-plugin imports
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# --- Constants ---------------------------------------------------------------

FOLLOW_DEFERRED_MSG = "tail_log: --follow deferred to v1.1; falling back to batch fetch\n"
SINCE_WITHOUT_UNIT_MSG = (
    "tail_log: --since is only honoured with --unit (journalctl mode); ignoring\n"
)

# Per-host ssh_execute subprocess budget. Generous: tail/journalctl finish
# fast but we want to absorb the SSH handshake + ConnectTimeout window.
HOST_SUBPROCESS_TIMEOUT_S = 60

# Default number of lines to pull (mirrors v0.2 stub default).
DEFAULT_LINES = 200

# Default SSH ConnectTimeout forwarded to ssh_execute.
DEFAULT_CONNECT_TIMEOUT_S = 8

# Cap multi-host concurrency. Each worker spawns its own ssh subprocess;
# 8 is enough for typical "spot-check 3-5 hosts" workflows without exhausting
# file descriptors or local CPU.
MAX_CONCURRENCY = 8

# Exit codes (subset listed in error-handling.md).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# --- Envelope helpers (inlined; ssh-core's lib cannot be imported across
# plugin boundaries -- CONTRIBUTING.md line 29). ------------------------------


def _envelope(
    success: bool,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    }


def _emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
        if not result["stdout"].endswith("\n"):
            sys.stdout.write("\n")
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
        if not result["stderr"].endswith("\n"):
            sys.stderr.write("\n")


# --- ssh_execute.py discovery -----------------------------------------------


def _ssh_execute_path() -> Path:
    """Locate ssh-core's ssh_execute.py relative to this file."""
    here = Path(__file__).resolve()
    return (
        here.parents[4]
        / "ssh-core"
        / "skills"
        / "ssh-core"
        / "scripts"
        / "ssh_execute.py"
    )


# --- Remote command builders -------------------------------------------------


def _build_remote_command(
    *,
    path: str | None,
    unit: str | None,
    lines: int,
    since: str | None,
) -> str:
    """Pick between tail (path mode) and journalctl (unit mode).

    Inputs are shlex.quoted to keep them as single shell tokens. The
    remote shell still sees a single string; ssh_execute passes it through
    untouched.
    """
    if unit:
        cmd = f"journalctl -u {shlex.quote(unit)} -n {lines} --no-pager"
        if since:
            cmd += f" --since {shlex.quote(since)}"
        return cmd
    # path mode: tail -n N <path>
    assert path is not None, "path required when unit is None"
    return f"tail -n {lines} {shlex.quote(path)}"


# --- Remote fetch -----------------------------------------------------------


def _run_remote_fetch(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
) -> dict[str, Any]:
    """Run a remote tail/journalctl through ssh_execute.py; return its envelope.

    Never raises; any unexpected failure synthesises a failure envelope.
    """
    argv = [
        sys.executable,
        str(ssh_exec),
        alias,
        command,
        "--json",
        "--connect-timeout",
        str(connect_timeout),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=HOST_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "tail_log: ssh_execute subprocess timed out after "
                f"{HOST_SUBPROCESS_TIMEOUT_S}s"
            ),
            data={"failure_class": "timeout"},
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _envelope(
            False,
            proc.returncode if proc.returncode != 0 else EXIT_FAIL,
            stdout=proc.stdout,
            stderr=(
                "tail_log: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Local filtering --------------------------------------------------------


def _apply_grep(lines: list[str], pattern: re.Pattern[str] | None) -> list[str]:
    """Filter lines by a compiled regex (search semantics)."""
    if pattern is None:
        return lines
    return [ln for ln in lines if pattern.search(ln)]


def _split_lines(raw_text: str) -> list[str]:
    """Split into lines, dropping a single trailing newline if present.

    `tail` / `journalctl` always emit a trailing newline; we don't want
    an empty string at the end of the list.
    """
    if raw_text.endswith("\n"):
        raw_text = raw_text[:-1]
    if not raw_text:
        return []
    return raw_text.split("\n")


# --- Per-host worker --------------------------------------------------------


def _fetch_one_host(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
    grep_pattern: re.Pattern[str] | None,
) -> dict[str, Any]:
    """Pull one host. Return a structured per-host record."""
    env = _run_remote_fetch(ssh_exec, alias, command, connect_timeout)
    raw_text = env.get("stdout") or ""
    raw_lines = _split_lines(raw_text)
    matched_lines = _apply_grep(raw_lines, grep_pattern)
    remote_exit = env.get("exit_code", EXIT_FAIL)
    failure_class = (env.get("data") or {}).get("failure_class")
    success = (
        env.get("success") is True
        and remote_exit == 0
        and failure_class is None
    )
    return {
        "alias": alias,
        "success": success,
        "remote_exit_code": remote_exit,
        "failure_class": failure_class,
        "raw_text": raw_text,
        "lines": matched_lines,
        "total_lines_before_grep": len(raw_lines),
        "matched_lines": len(matched_lines),
        "remote_stderr": env.get("stderr") or "",
    }


def _gather_multi_host(
    ssh_exec: Path,
    hosts: list[str],
    command: str,
    connect_timeout: int,
    grep_pattern: re.Pattern[str] | None,
) -> dict[str, dict[str, Any]]:
    """Concurrent fetch across hosts; return dict keyed by alias."""
    results: dict[str, dict[str, Any]] = {}
    workers = min(MAX_CONCURRENCY, len(hosts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _fetch_one_host,
                ssh_exec,
                host,
                command,
                connect_timeout,
                grep_pattern,
            ): host
            for host in hosts
        }
        for fut in as_completed(futures):
            host = futures[fut]
            results[host] = fut.result()
    return results


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tail_log.py",
        description=(
            "Multi-host log tail. Pulls `tail -n N` (path mode) or "
            "`journalctl -u UNIT -n N` (unit mode), optionally filtered "
            "by a Python regex. Emits the shared JSON contract with --json."
        ),
    )
    # The v0.2 stub also accepted `alias` as a positional; we keep it for
    # back-compat. The new canonical single-host form is
    # `tail_log.py <alias> <path>`.
    p.add_argument(
        "alias",
        nargs="?",
        default=None,
        help="Host alias (single-host mode)",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Log file path (single-host mode without --unit)",
    )
    p.add_argument(
        "--hosts",
        help="Comma-separated host aliases (multi-host mode)",
    )
    p.add_argument(
        "--unit",
        help="systemd unit to tail via journalctl (mutually exclusive with path)",
    )
    p.add_argument(
        "--lines",
        type=int,
        default=DEFAULT_LINES,
        help=f"Number of lines to fetch per host (default: {DEFAULT_LINES})",
    )
    p.add_argument(
        "--since",
        default=None,
        help=(
            "Time expression passed to `journalctl --since` "
            "(only honoured with --unit). e.g. '5min ago', 'yesterday'."
        ),
    )
    p.add_argument(
        "--grep",
        default=None,
        help="Python regex applied locally (search) after fetch",
    )
    # v0.2 surface accepted --follow; v1.0 defers it.
    p.add_argument(
        "--follow",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT_S,
        help=(
            "SSH ConnectTimeout passed to ssh_execute "
            f"(default: {DEFAULT_CONNECT_TIMEOUT_S})"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the shared JSON output contract on stdout",
    )
    return p


# --- main --------------------------------------------------------------------


def _resolve_hosts(args: argparse.Namespace) -> list[str] | None:
    if args.hosts:
        return [h.strip() for h in args.hosts.split(",") if h.strip()]
    if args.alias:
        return [args.alias]
    return None


def _validate_source(args: argparse.Namespace) -> tuple[str | None, str | None, str | None]:
    """Return (path, unit, error). Either path or unit must be set, not both.

    The path can come from the v0.2 positional `path` slot.
    """
    if args.unit and args.path:
        return None, None, "cannot specify both <path> positional and --unit"
    if args.unit:
        return None, args.unit, None
    if args.path:
        return args.path, None, None
    return None, None, "need either <path> positional or --unit <name>"


def _compose_human_stdout(
    per_host: dict[str, dict[str, Any]],
) -> str:
    """Concatenate prefixed lines for human stdout."""
    out_chunks: list[str] = []
    # Stable order by alias for human reading (concurrent fetch may finish
    # in any order, but the resulting text should be deterministic).
    for alias in sorted(per_host.keys()):
        for ln in per_host[alias]["lines"]:
            out_chunks.append(f"{alias}| {ln}\n")
    return "".join(out_chunks)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.follow:
        sys.stderr.write(FOLLOW_DEFERRED_MSG)

    # When --hosts is set, any leftover positional is meant to be the path
    # (argparse assigns it to the `alias` slot because that's the first
    # positional). Coalesce so downstream logic stays simple.
    if args.hosts and args.alias and not args.path:
        args.path = args.alias
        args.alias = None

    # Resolve hosts.
    hosts = _resolve_hosts(args)
    if not hosts:
        sys.stderr.write("tail_log: need either positional <alias> or --hosts\n")
        return EXIT_ARGS

    # Resolve source (path xor unit).
    path, unit, src_err = _validate_source(args)
    if src_err:
        sys.stderr.write(f"tail_log: {src_err}\n")
        return EXIT_ARGS

    # --since only valid in unit mode.
    since_for_command = args.since
    if args.since and not unit:
        sys.stderr.write(SINCE_WITHOUT_UNIT_MSG)
        since_for_command = None

    # Compile grep regex up-front so we surface argv errors early.
    grep_pattern: re.Pattern[str] | None = None
    if args.grep is not None:
        try:
            grep_pattern = re.compile(args.grep)
        except re.error as e:
            sys.stderr.write(f"tail_log: invalid --grep regex: {e}\n")
            return EXIT_ARGS

    # Build the remote command (same for every host -- only the alias differs).
    command = _build_remote_command(
        path=path,
        unit=unit,
        lines=args.lines,
        since=since_for_command,
    )

    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        result = _envelope(
            False,
            EXIT_FAIL,
            stderr=f"tail_log: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
        _emit(result, args.json)
        return EXIT_FAIL

    # Per-host fetch (concurrent if >1).
    per_host = _gather_multi_host(
        ssh_exec, hosts, command, args.connect_timeout, grep_pattern
    )

    # Aggregate envelope.
    all_success = all(rec["success"] for rec in per_host.values())
    total_lines = sum(rec["matched_lines"] for rec in per_host.values())
    any_timeout = any(
        rec["failure_class"] == "timeout" for rec in per_host.values()
    )
    overall_exit = EXIT_OK if all_success else (EXIT_TIMEOUT if any_timeout else EXIT_FAIL)

    # Single-host envelope shape is slightly different (data.host instead of
    # data.hosts) to match the v0.2 SKILL examples. Multi-host always uses
    # data.hosts.
    data: dict[str, Any] = {
        "source": {
            "kind": "unit" if unit else "path",
            "value": unit if unit else path,
        },
        "filter": {
            "grep": args.grep,
            "total_lines": total_lines,
        },
    }
    if len(hosts) == 1:
        only = per_host[hosts[0]]
        data["host"] = hosts[0]
        data["lines"] = only["lines"]
        data["raw_text"] = only["raw_text"]
        data["remote_exit_code"] = only["remote_exit_code"]
        data["filter"]["matched"] = only["matched_lines"]
        data["filter"]["total"] = only["total_lines_before_grep"]
        if only["failure_class"]:
            data["failure_class"] = only["failure_class"]
            data["remote_stderr"] = only["remote_stderr"]
    else:
        # Multi-host: full per-host map.
        data["hosts"] = per_host
        if any_timeout:
            data["failure_class"] = "timeout"

    result = _envelope(
        all_success,
        overall_exit,
        stdout=_compose_human_stdout(per_host),
        data=data,
    )
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
