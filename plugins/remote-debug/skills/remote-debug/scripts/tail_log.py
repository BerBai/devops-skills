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

``--follow`` streams instead of fetching a snapshot: the remote command becomes
``tail -F`` (path mode) or ``journalctl -f`` (unit mode), and each line is
relayed as it arrives, prefixed with the alias when more than one host is
selected. The session ends on Ctrl-C or after ``--follow-timeout`` seconds; the
final envelope still reports what was seen, so ``--follow --json`` produces one
parseable document either way.

``local`` is served natively (no ssh, no ``tail`` binary). A source that route
cannot serve on this platform is a **skip**, not a failure: the per-host record
carries ``skipped: true`` and ``skipped_reason`` (currently only
``"posix_path_on_windows"``), ``failure_class`` stays ``null``, and the host is
left out of the pass/fail decision -- so ``--hosts lab-240,local /etc/hostname``
on Windows is ``success: true`` / exit 0 with ``local`` in ``data.skipped_hosts``.
The same envelope carries ``data.ok_hosts`` / ``data.failed_hosts`` /
``data.skipped_hosts`` arrays alongside the per-host map. A Windows path
(``C:/Windows/win.ini`` or ``file://local/C:/Windows/win.ini``) is a normal
source on the local route.

Exit codes follow ``error-handling.md``::

    0    every non-skipped host succeeded (remote exit 0)
    1    any non-skipped host had non-zero remote exit or ssh_execute failure
    2    argv error (bad regex, missing path/unit)
    124  any ssh_execute subprocess timed out

See:
    .trellis/spec/backend/adr-001-ssh-execute.md   ssh_execute contract
    .trellis/spec/backend/error-handling.md         JSON envelope rules
    CONTRIBUTING.md line 29                          no cross-plugin imports
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import _local_exec
import _ssh_core_locator as _locator
from _version import add_version_flag, handle_version

# --- Constants ---------------------------------------------------------------

SINCE_WITHOUT_UNIT_MSG = (
    "tail_log: --since is only honoured with --unit (journalctl mode); ignoring\n"
)

# Per-host ssh_execute subprocess budget. Generous: tail/journalctl finish
# fast but we want to absorb the SSH handshake + ConnectTimeout window.
HOST_SUBPROCESS_TIMEOUT_S = 60

# A follow session is deliberately long-lived: ssh_execute --follow keeps the
# channel open until Ctrl-C or --follow-timeout. The subprocess timeout is the
# user's budget plus the handshake, not a fixed cap.
FOLLOW_DEFAULT_TIMEOUT_S = 600
FOLLOW_MIN_TIMEOUT_S = 5

# How many lines follow starts from by default (mirrors `tail -f -n N`).
FOLLOW_TAIL_DEFAULT = 10

# Default number of lines to pull (mirrors v0.2 stub default).
DEFAULT_LINES = 200

# Default SSH ConnectTimeout forwarded to ssh_execute.
DEFAULT_CONNECT_TIMEOUT_S = 8

# Cap multi-host concurrency. Each worker spawns its own ssh subprocess;
# 8 is enough for typical "spot-check 3-5 hosts" workflows without exhausting
# file descriptors or local CPU.
MAX_CONCURRENCY = 8

# Cap for the first-line remote_stderr we surface on local stderr when a host
# fails. Mirrors ssh_cluster.py's _STDERR_HEAD_CAP (commit f4c072b): same name,
# same value, same "first non-empty line + '...'-suffix" semantics so users get
# a consistent failure clue without having to re-run with --json.
_STDERR_HEAD_CAP = 200

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
    _local_exec.utf8_stdio()
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
    """Locate ssh-core's ssh_execute.py (honours DEVOPS_SSH_CORE_DIR)."""
    return _locator.ssh_core_script("ssh_execute.py")


# --- Remote command builders -------------------------------------------------


def _build_remote_command(
    *,
    path: str | None,
    unit: str | None,
    lines: int,
    since: str | None,
    follow: bool = False,
) -> str:
    """Pick between tail (path mode) and journalctl (unit mode).

    Inputs are shlex.quoted to keep them as single shell tokens. The
    remote shell still sees a single string; ssh_execute passes it through
    untouched.

    ``follow=True`` switches to the streaming variants: ``tail -F`` (survives
    log rotation) and ``journalctl -f``.
    """
    if unit:
        cmd = f"journalctl -u {shlex.quote(unit)} -n {lines} --no-pager"
        if since:
            cmd += f" --since {shlex.quote(since)}"
        if follow:
            cmd += " -f"
        return cmd
    # path mode: tail -n N <path>
    assert path is not None, "path required when unit is None"
    if follow:
        return f"tail -n {lines} -F {shlex.quote(path)}"
    return f"tail -n {lines} {shlex.quote(path)}"


def _terminate_follow(proc: subprocess.Popen[str]) -> None:
    """Best-effort teardown of a follow child (SIGTERM, then SIGKILL)."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=1)


def _follow_one_host(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
    follow_timeout: int,
    grep_pattern: re.Pattern[str] | None,
    *,
    echo: bool,
    local_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stream one host until the budget expires, then return its record.

    ``ssh_execute --follow --json`` writes the live transcript to stderr and the
    final envelope to stdout, so both pipes are read: stderr line by line, then
    stdout once in ``finally``. ``--tail`` is deliberately **not** passed --
    ssh_execute implements it by wrapping the command in another ``tail -f``,
    which would double-filter what this script already controls.

    ``local`` has no ssh_execute to stream from; it is served by a native
    polling tail bounded by the same budget.
    """
    if alias == "local" and local_spec is not None:
        env = _local_tail(
            str(local_spec.get("kind") or "path"),
            str(local_spec.get("value") or ""),
            int(local_spec.get("lines") or 0),
            follow=True,
            follow_timeout=float(follow_timeout),
        )
        raw = env.get("stdout") or ""
        rows = _split_lines(raw)
        matched = _apply_grep(rows, grep_pattern)
        if echo:
            for row in matched:
                sys.stdout.write(f"{row}\n")
            sys.stdout.flush()
        failure_class = (env.get("data") or {}).get("failure_class")
        skip = _skip_fields(env)
        record: dict[str, Any] = {
            "alias": alias,
            "success": (
                bool(env.get("success"))
                and failure_class is None
                and not skip["skipped"]
            ),
            "remote_exit_code": env.get("exit_code", EXIT_FAIL),
            "failure_class": failure_class,
            "raw_text": raw,
            "lines": matched,
            "total_lines_before_grep": len(rows),
            "matched_lines": len(matched),
            "remote_stderr": env.get("stderr") or "",
            "follow": True,
        }
        record.update(skip)
        return record

    argv = [
        sys.executable,
        str(ssh_exec),
        alias,
        command,
        "--json",
        "--connect-timeout",
        str(connect_timeout),
        "--follow",
        "--timeout",
        str(follow_timeout),
    ]
    lines: list[str] = []
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return {
            "alias": alias,
            "success": False,
            "remote_exit_code": EXIT_FAIL,
            "failure_class": "spawn_failed",
            "raw_text": "",
            "lines": [],
            "total_lines_before_grep": 0,
            "matched_lines": 0,
            "remote_stderr": str(exc),
            "follow": True,
        }

    try:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            lines.append(line)
            if grep_pattern is None or grep_pattern.search(line):
                if echo:
                    sys.stdout.write(f"{alias}| {line}\n")
                    sys.stdout.flush()
    except KeyboardInterrupt:  # pragma: no cover - operator action
        pass
    finally:
        _terminate_follow(proc)

    envelope: dict[str, Any] = {}
    if proc.stdout is not None:
        with contextlib.suppress(Exception):
            envelope = json.loads(proc.stdout.read() or "{}")
    failure_class = (envelope.get("data") or {}).get("failure_class")
    matched = [ln for ln in lines if grep_pattern is None or grep_pattern.search(ln)]
    return {
        "alias": alias,
        "success": bool(envelope.get("success")) and failure_class is None,
        "remote_exit_code": envelope.get("exit_code", proc.returncode),
        "failure_class": failure_class,
        "raw_text": "\n".join(lines) + ("\n" if lines else ""),
        "lines": matched,
        "total_lines_before_grep": len(lines),
        "matched_lines": len(matched),
        "remote_stderr": envelope.get("stderr") or "",
        "follow": True,
    }


def _run_follow_stream(
    ssh_exec: Path,
    hosts: list[str],
    command: str,
    connect_timeout: int,
    follow_timeout: int,
    grep_pattern: re.Pattern[str] | None,
    *,
    echo: bool = True,
    local_spec: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Stream a long-running tail from every host concurrently."""
    results: dict[str, dict[str, Any]] = {}
    workers = min(MAX_CONCURRENCY, len(hosts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _follow_one_host,
                ssh_exec,
                host,
                command,
                connect_timeout,
                follow_timeout,
                grep_pattern,
                echo=echo,
                local_spec=local_spec,
            ): host
            for host in hosts
        }
        try:
            for fut in as_completed(futures):
                host = futures[fut]
                results[host] = fut.result()
        except KeyboardInterrupt:  # pragma: no cover - operator action
            for fut in futures:
                fut.cancel()
    return results

# --- Remote fetch -----------------------------------------------------------


def _run_local_command(name: str, command: str, timeout: int) -> dict[str, Any]:
    """Run a POSIX one-liner on *this* host through the platform shell.

    ``host == "local"`` cannot go through ssh-core: its local route runs
    without a shell and refuses ``;``/``|``. This is the fallback for callers
    that have no structured source; the normal path uses :func:`_local_tail`,
    which reads the file natively and therefore works on Windows.
    """
    return _local_exec.run_shell(name, command, timeout)


def _local_tail(
    kind: str,
    value: str,
    lines: int,
    *,
    follow: bool = False,
    follow_timeout: float = 0,
) -> dict[str, Any]:
    """Serve ``local`` natively: read the file, never shell out.

    ``top``/``journalctl`` do not exist on Windows, and piping a POSIX
    one-liner through ``cmd.exe`` produced ``'tail' is not recognized`` with no
    hint that the *route* was wrong (LS-02). Path mode now reads the file with
    a bounded seek; unit mode -- which has no portable equivalent -- says so
    explicitly instead of failing as ``remote_error``.

    Path mode also normalises the platform spelling: a drive-letter path
    (``C:/Windows/win.ini``) or ``file://local/C:/...`` resolves on Windows,
    and a POSIX-absolute path on Windows comes back as a *skip*
    (``skipped_reason: "posix_path_on_windows"``) rather than as a fabricated
    ``local_not_found`` (OPT-004).
    """
    if kind == "unit":
        return _local_exec.local_unsupported(
            "tail_log",
            f"journalctl -u {value}",
            "journalctl has no Windows/POSIX-neutral equivalent; use a whole "
            "POSIX host as the source, or tail the file the unit writes to",
        )
    if follow:
        return _local_exec.tail_file_follow(value, lines, follow_timeout)
    return _local_exec.tail_file(value, lines)


def _run_remote_fetch(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
    *,
    local_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a remote tail/journalctl through ssh_execute.py; return its envelope.

    Never raises; any unexpected failure synthesises a failure envelope.
    ``local_spec`` carries the structured source (kind/value/lines) so the
    ``local`` route can be served natively instead of via a POSIX one-liner.
    """
    if alias == "local":
        if local_spec is not None:
            return _local_tail(
                str(local_spec.get("kind") or "path"),
                str(local_spec.get("value") or ""),
                int(local_spec.get("lines") or 0),
                follow=bool(local_spec.get("follow")),
                follow_timeout=float(local_spec.get("follow_timeout") or 0),
            )
        return _run_local_command("tail_log", command, HOST_SUBPROCESS_TIMEOUT_S)

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


def _skip_fields(env: dict[str, Any]) -> dict[str, Any]:
    """Extract the platform-skip marker from a local envelope.

    A skip (``data.skipped: true``) is neither ok nor failed: it means the
    local route cannot serve this source on this platform at all, so the host
    must stay out of the pass/fail decision. Keep the two fields in one place
    so the snapshot and ``--follow`` routes agree.
    """
    data = env.get("data") or {}
    skipped = bool(data.get("skipped"))
    return {
        "skipped": skipped,
        "skipped_reason": data.get("skipped_reason") if skipped else None,
    }


def _bucket_hosts(
    per_host: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Split aliases into ``(ok_hosts, failed_hosts, skipped_hosts)``.

    One source of truth for the verdict *and* the reported arrays, mirroring
    ``ssh_cluster._bucket_hosts`` (DEVOPS-BUG-010) so both scripts answer the
    same question the same way:

    * **ok** - the host produced its lines;
    * **skipped** - the local route cannot serve this source on this platform
      (a POSIX path on Windows). Nothing failed, so it must not tint the run;
    * **failed** - everything else, i.e. a real failure.

    The buckets are disjoint, so ``ok + failed + skipped == len(per_host)``.
    """
    ok_hosts: list[str] = []
    failed_hosts: list[str] = []
    skipped_hosts: list[str] = []
    for alias in sorted(per_host.keys()):
        record = per_host[alias]
        if record.get("success"):
            ok_hosts.append(alias)
        elif record.get("skipped"):
            skipped_hosts.append(alias)
        else:
            failed_hosts.append(alias)
    return ok_hosts, failed_hosts, skipped_hosts


def _fetch_one_host(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
    grep_pattern: re.Pattern[str] | None,
    *,
    local_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pull one host. Return a structured per-host record."""
    env = _run_remote_fetch(
        ssh_exec, alias, command, connect_timeout, local_spec=local_spec
    )
    raw_text = env.get("stdout") or ""
    raw_lines = _split_lines(raw_text)
    matched_lines = _apply_grep(raw_lines, grep_pattern)
    remote_exit = env.get("exit_code", EXIT_FAIL)
    failure_class = (env.get("data") or {}).get("failure_class")
    skip = _skip_fields(env)
    success = (
        env.get("success") is True
        and remote_exit == 0
        and failure_class is None
        and not skip["skipped"]
    )
    record: dict[str, Any] = {
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
    record.update(skip)
    return record


def _gather_multi_host(
    ssh_exec: Path,
    hosts: list[str],
    command: str,
    connect_timeout: int,
    grep_pattern: re.Pattern[str] | None,
    *,
    local_spec: dict[str, Any] | None = None,
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
                local_spec=local_spec,
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
    p.add_argument(
        "--follow",
        action="store_true",
        help=(
            "Stream instead of snapshot: `tail -F` (path mode) or "
            "`journalctl -f` (unit mode). Ends on Ctrl-C or after "
            f"--follow-timeout seconds (default {FOLLOW_DEFAULT_TIMEOUT_S})."
        ),
    )
    p.add_argument(
        "--follow-timeout",
        type=int,
        default=FOLLOW_DEFAULT_TIMEOUT_S,
        help=(
            "Wall-clock budget for one --follow session, in seconds "
            f"(default {FOLLOW_DEFAULT_TIMEOUT_S}, minimum {FOLLOW_MIN_TIMEOUT_S})."
        ),
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
    add_version_flag(p)
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


def _compose_human_stderr(
    per_host: dict[str, dict[str, Any]],
) -> str:
    """Surface the first line of each failing host's remote_stderr.

    Mirrors the ssh_cluster.py stderr-surfacing pattern (commit f4c072b) so
    non-JSON callers see *why* a host failed instead of an empty stderr +
    exit 1. Successful hosts contribute nothing. The same string is also
    placed on the envelope so --json consumers receive it via
    ``envelope["stderr"]``. The structured ``data.remote_stderr`` /
    ``data.hosts[<alias>].remote_stderr`` payload is left untouched.

    A skipped host is reported as ``skipped (<reason>)``, not as a failure:
    the run is green, and the note explains why one alias contributed no
    lines.
    """
    lines: list[str] = []
    # Stable alias order for determinism (mirrors _compose_human_stdout).
    for alias in sorted(per_host.keys()):
        rec = per_host[alias]
        if rec.get("success") is True:
            continue
        if rec.get("skipped") and not rec.get("failure_class"):
            reason = rec.get("skipped_reason") or "unsupported_source"
            lines.append(f"tail_log: {alias}: skipped ({reason})\n")
            continue
        message = ""
        for raw_line in (rec.get("remote_stderr") or "").split("\n"):
            stripped = raw_line.strip()
            if stripped:
                message = stripped
                break
        if not message:
            fc = rec.get("failure_class") or "unknown"
            rexit = rec.get("remote_exit_code")
            message = f"failure_class={fc}, remote_exit={rexit}"
        if len(message) > _STDERR_HEAD_CAP:
            message = message[:_STDERR_HEAD_CAP] + "..."
        lines.append(f"tail_log: {alias}: {message}\n")
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    early = handle_version("tail_log.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    if args.follow and args.follow_timeout < FOLLOW_MIN_TIMEOUT_S:
        sys.stderr.write(
            f"tail_log: --follow-timeout must be >= {FOLLOW_MIN_TIMEOUT_S}s\n"
        )
        return EXIT_ARGS

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
        follow=args.follow,
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

    # Per-host fetch (concurrent if >1). --follow streams instead of
    # snapshotting, and echoes each line as it arrives. ``local_spec`` lets the
    # local route be served natively (no `tail`/`journalctl` dependency).
    local_spec = {
        "kind": "unit" if unit else "path",
        "value": unit if unit else path,
        "lines": args.lines,
        "follow": bool(args.follow),
        "follow_timeout": args.follow_timeout,
    }
    if args.follow:
        per_host = _run_follow_stream(
            ssh_exec,
            hosts,
            command,
            args.connect_timeout,
            args.follow_timeout,
            grep_pattern,
            echo=not args.json,
            local_spec=local_spec,
        )
    else:
        per_host = _gather_multi_host(
            ssh_exec,
            hosts,
            command,
            args.connect_timeout,
            grep_pattern,
            local_spec=local_spec,
        )

    # Aggregate envelope. A skipped host is neither ok nor failed: the local
    # route could not serve that source on this platform, so it must not turn
    # a run in which every *servable* host succeeded red (mirrors ssh_cluster's
    # skipped semantics, BUG-010 / OPT-004).
    ok_hosts, failed_hosts, skipped_hosts = _bucket_hosts(per_host)
    all_success = not failed_hosts
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
        "ok_hosts": ok_hosts,
        "failed_hosts": failed_hosts,
        "skipped_hosts": skipped_hosts,
    }
    if len(hosts) == 1:
        only = per_host[hosts[0]]
        data["host"] = hosts[0]
        data["lines"] = only["lines"]
        data["raw_text"] = only["raw_text"]
        data["remote_exit_code"] = only["remote_exit_code"]
        data["filter"]["matched"] = only["matched_lines"]
        data["filter"]["total"] = only["total_lines_before_grep"]
        # Skip markers are only added when the host really was skipped, so a
        # servable run keeps exactly the payload it always had.
        if only.get("skipped"):
            data["skipped"] = True
            data["skipped_reason"] = only.get("skipped_reason")
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
        stderr=_compose_human_stderr(per_host),
        data=data,
    )
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
