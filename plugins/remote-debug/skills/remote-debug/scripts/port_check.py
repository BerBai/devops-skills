#!/usr/bin/env python3
"""port_check.py - TCP reachability probe FROM a remote host (v1.0).

The trick: this runs ``nc -zv`` (or its fallback) on the remote source,
not on your laptop. The question "can the app server reach the
database?" only has the right answer when asked from the right place.

Public surface::

    port_check.py <alias> --target <host> --ports 5432,6379,9092 [--json]
    port_check.py --from a,b,c --to db-1,cache-1 --ports 5432 [--json]
        # produces a matrix: source x (target, port) -> reachable | filtered | refused

Behaviour
---------
Shells out to ``ssh-core``'s ``ssh_execute.py`` (NEVER imported -
per CONTRIBUTING.md line 29 the cross-plugin contract is CLI shell-out)
and runs one probe per (source, target, port) cell. Each probe is a
single shell command that tries ``nc -zv -w T`` first and falls back to
``timeout T bash -c "exec 3<>/dev/tcp/<host>/<port>"`` when ``nc`` is
missing.

The remote stdout is classified into a status:

    open         remote exit 0, no refusal marker in output
    refused      "Connection refused" in stdout/stderr
    filtered     "timed out", "Operation timed out", or timeout(1) exit 124
    host-error   "Name or service not known" / getaddrinfo failure
    source-error ssh_execute itself failed (auth/network/broken)

``--udp`` is accepted for v0.2 compatibility but **deferred to v1.1**;
when set, a single line is written to stderr and the UDP probe is
NOT executed.

Exit codes follow ``error-handling.md``::

    0    every cell is ``open``
    1    any cell is not ``open`` (or source-error)
    2    argv error (bad ports, missing source/target)
    124  any ssh_execute subprocess timed out at the wrapper level

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
import time
from pathlib import Path
from typing import Any

# --- Constants ---------------------------------------------------------------

UDP_DEFERRED_MSG = "port_check: --udp deferred to v1.1\n"

# Per-cell ssh_execute subprocess budget. The cell itself runs an `nc -w T`
# (or `timeout T ...`), so the subprocess shouldn't take much longer than
# T + handshake. 30s is generous; lets ssh ConnectTimeout (8s) win.
PROBE_SUBPROCESS_TIMEOUT_S = 30

# Default per-port probe timeout (passed to `nc -w` / `timeout`).
DEFAULT_PORT_TIMEOUT_S = 2

# Default SSH ConnectTimeout forwarded to ssh_execute.
DEFAULT_CONNECT_TIMEOUT_S = 8

# Status pattern registry (matched against combined stdout+stderr). Order
# inside _classify_status matters: refused > host-error > filtered.
REFUSED_PATTERNS = (re.compile(r"Connection refused", re.IGNORECASE),)
HOST_ERROR_PATTERNS = (
    re.compile(r"Name or service not known"),
    re.compile(r"nodename nor servname provided"),
    re.compile(r"No address associated"),
    re.compile(r"getaddrinfo"),
)
TIMEOUT_PATTERNS = (
    re.compile(r"timed out", re.IGNORECASE),
    re.compile(r"Operation timed out", re.IGNORECASE),
)

# Exit codes (subset listed in error-handling.md).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124

# Port range bounds.
MIN_PORT = 1
MAX_PORT = 65535

# ssh_execute remote-shell "command timed out at the bash -c timeout" exit
# code. GNU coreutils' `timeout(1)` returns 124 on TERM; we treat that as
# 'filtered' so the matrix stays readable.
REMOTE_TIMEOUT_EXIT_CODE = 124


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
    """Locate ssh-core's ssh_execute.py relative to this file.

    Layout::

        plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
        plugins/remote-debug/skills/remote-debug/scripts/port_check.py
                                                                     ^ here

    parents[0]=scripts, [1]=remote-debug (skill), [2]=skills, [3]=remote-debug
    (plugin), [4]=plugins. Caller must check ``.exists()``.
    """
    here = Path(__file__).resolve()
    return (
        here.parents[4]
        / "ssh-core"
        / "skills"
        / "ssh-core"
        / "scripts"
        / "ssh_execute.py"
    )


# --- Port parsing ------------------------------------------------------------


def _parse_ports(spec: str) -> list[int]:
    """Parse '5432,6379' or '8000:8003' (inclusive) or a mix of both.

    Returns the de-duplicated, ordered list. Raises ValueError with a
    human-readable message on any invalid input; callers map that into an
    exit_code=2 envelope.
    """
    ports: list[int] = []
    for raw_chunk in spec.split(","):
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            lo_s, _, hi_s = chunk.partition(":")
            try:
                lo, hi = int(lo_s), int(hi_s)
            except ValueError as e:
                raise ValueError(f"invalid port range: {chunk!r}") from e
            if lo < MIN_PORT or hi > MAX_PORT or lo > hi:
                raise ValueError(f"invalid port range: {chunk!r}")
            ports.extend(range(lo, hi + 1))
        else:
            try:
                p = int(chunk)
            except ValueError as e:
                raise ValueError(f"invalid port: {chunk!r}") from e
            if p < MIN_PORT or p > MAX_PORT:
                raise ValueError(f"invalid port: {chunk!r}")
            ports.append(p)
    # De-dup while preserving order.
    seen: set[int] = set()
    deduped: list[int] = []
    for p in ports:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped


# --- Remote probe runner -----------------------------------------------------


def _build_probe_command(target: str, port: int, port_timeout: int) -> str:
    """Build a single remote-shell command: nc preferred, /dev/tcp fallback.

    ``target`` is shell-quoted to keep it a single token even if exotic.
    ``port`` and ``port_timeout`` are ints, safe to format.
    """
    qt = shlex.quote(target)
    return (
        f"if command -v nc >/dev/null 2>&1; then "
        f"nc -zv -w {port_timeout} {qt} {port} 2>&1; "
        f"else "
        f'timeout {port_timeout} bash -c "exec 3<>/dev/tcp/{qt}/{port}" 2>&1; '
        f"fi"
    )


def _run_remote_probe(
    ssh_exec: Path,
    source: str,
    command: str,
    connect_timeout: int,
) -> dict[str, Any]:
    """Run a single probe through ssh_execute.py; return its JSON envelope.

    Never raises; any unexpected failure (timeout at the subprocess layer,
    non-JSON stdout) is synthesised into a failure envelope so the caller
    can keep walking the matrix.
    """
    argv = [
        sys.executable,
        str(ssh_exec),
        source,
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
            timeout=PROBE_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "port_check: ssh_execute subprocess timed out after "
                f"{PROBE_SUBPROCESS_TIMEOUT_S}s"
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
                "port_check: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Status classification ---------------------------------------------------


def _match_any(combined: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pat.search(combined) for pat in patterns)


def _classify_status(envelope: dict[str, Any]) -> str:
    """Map a probe envelope to one of the five cell statuses."""
    failure = (envelope.get("data") or {}).get("failure_class")
    if failure in {"auth", "network", "ssh_execute_broken"}:
        return "source-error"
    if failure == "timeout":
        return "filtered"

    remote_exit = envelope.get("exit_code")
    combined = (envelope.get("stdout") or "") + "\n" + (envelope.get("stderr") or "")

    # Pattern checks run before the exit-code shortcut: some nc variants
    # report 'Connection refused' but exit 0 in v6+ mode. Order matters:
    # refused > host-error > generic timeout text.
    if _match_any(combined, REFUSED_PATTERNS):
        status = "refused"
    elif _match_any(combined, HOST_ERROR_PATTERNS):
        status = "host-error"
    elif _match_any(combined, TIMEOUT_PATTERNS):
        status = "filtered"
    elif remote_exit == 0:
        status = "open"
    else:
        # Includes REMOTE_TIMEOUT_EXIT_CODE; unknown non-zero leans to
        # 'filtered' (safest for a reachability check).
        status = "filtered"
    return status


# --- Matrix walk -------------------------------------------------------------


def _gather_matrix(
    ssh_exec: Path,
    sources: list[str],
    targets: list[str],
    ports: list[int],
    port_timeout: int,
    connect_timeout: int,
) -> list[dict[str, Any]]:
    """Run every (source, target, port) cell serially. Return the matrix.

    Serial by design (see design.md S 2.3): port_check's bottleneck is the
    SSH handshake; correctness first, parallelism is a v1.0.1 optimisation.
    """
    matrix: list[dict[str, Any]] = []
    for src in sources:
        for tgt in targets:
            for prt in ports:
                cmd = _build_probe_command(tgt, prt, port_timeout)
                t0 = time.monotonic()
                env = _run_remote_probe(ssh_exec, src, cmd, connect_timeout)
                elapsed_ms = int(round((time.monotonic() - t0) * 1000))
                status = _classify_status(env)
                cell: dict[str, Any] = {
                    "source": src,
                    "target": tgt,
                    "port": prt,
                    "status": status,
                    "elapsed_ms": elapsed_ms,
                    "remote_exit_code": env.get("exit_code"),
                }
                if status == "source-error":
                    fc = (env.get("data") or {}).get("failure_class")
                    if fc:
                        cell["failure_class"] = fc
                    cell["error_stderr"] = env.get("stderr") or ""
                matrix.append(cell)
    return matrix


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="port_check.py",
        description=(
            "TCP reachability probe FROM a remote host. Emits the shared "
            "JSON contract with --json."
        ),
    )
    p.add_argument(
        "alias",
        nargs="?",
        help="Single-source mode: host alias from ~/.ssh/config",
    )
    p.add_argument(
        "--from",
        dest="src_hosts",
        help="Multi-source mode: comma-separated host aliases",
    )
    p.add_argument(
        "--target",
        help="Single target hostname/IP (single-source mode)",
    )
    p.add_argument(
        "--to",
        dest="dst_hosts",
        help="Multi-target mode: comma-separated targets",
    )
    p.add_argument(
        "--ports",
        required=True,
        help="Comma-separated TCP ports (or 'a:b' ranges, inclusive)",
    )
    # v0.2 scaffolding accepted --udp; v1.0 defers UDP to v1.1 but keeps the
    # flag accepted (R2) so callers that learned the v0.2 surface don't break.
    p.add_argument(
        "--udp",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_PORT_TIMEOUT_S,
        help=f"Per-port probe timeout in seconds (default: {DEFAULT_PORT_TIMEOUT_S})",
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


def _resolve_sources(args: argparse.Namespace) -> list[str] | None:
    if args.src_hosts:
        return [s.strip() for s in args.src_hosts.split(",") if s.strip()]
    if args.alias:
        return [args.alias]
    return None


def _resolve_targets(args: argparse.Namespace) -> list[str] | None:
    if args.dst_hosts:
        return [t.strip() for t in args.dst_hosts.split(",") if t.strip()]
    if args.target:
        return [args.target]
    return None


def _format_human(matrix: list[dict[str, Any]]) -> str:
    if not matrix:
        return ""
    rows = []
    for c in matrix:
        rows.append(
            f"{c['source']:<20} -> {c['target']}:{c['port']:<5}  "
            f"{c['status']:<13} ({c['elapsed_ms']}ms)"
        )
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.udp:
        sys.stderr.write(UDP_DEFERRED_MSG)

    sources = _resolve_sources(args)
    if not sources:
        sys.stderr.write("port_check: need either positional <alias> or --from\n")
        return EXIT_ARGS

    targets = _resolve_targets(args)
    if not targets:
        sys.stderr.write("port_check: need either --target or --to\n")
        return EXIT_ARGS

    try:
        ports = _parse_ports(args.ports)
    except ValueError as e:
        sys.stderr.write(f"port_check: {e}\n")
        return EXIT_ARGS
    if not ports:
        sys.stderr.write("port_check: --ports parsed to empty list\n")
        return EXIT_ARGS

    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        result = _envelope(
            False,
            EXIT_FAIL,
            stderr=f"port_check: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
        _emit(result, args.json)
        return EXIT_FAIL

    matrix = _gather_matrix(
        ssh_exec,
        sources,
        targets,
        ports,
        args.timeout,
        args.connect_timeout,
    )

    open_count = sum(1 for c in matrix if c["status"] == "open")
    total = len(matrix)
    all_open = total > 0 and open_count == total
    any_source_error = any(c["status"] == "source-error" for c in matrix)
    overall_exit = EXIT_OK if all_open else EXIT_FAIL

    result = _envelope(
        all_open,
        overall_exit,
        stdout=_format_human(matrix),
        data={
            "sources": sources,
            "targets": targets,
            "ports": ports,
            "matrix": matrix,
            "summary": {
                "open": open_count,
                "total": total,
                "any_source_error": any_source_error,
            },
            "failure_class": (
                None if all_open
                else ("source" if any_source_error else "unreachable")
            ),
        },
    )
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
