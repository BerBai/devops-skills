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
and runs one probe per (source, target, port) cell. A **remote** probe is a
single shell command that tries ``nc -zv -w T`` first and falls back to
``timeout T bash -c "exec 3<>/dev/tcp/<host>/<port>"`` when ``nc`` is
missing.

A **local** source is probed natively with ``socket.connect_ex`` (see
``_local_exec``), not by piping that POSIX one-liner through the local
shell. ``cmd.exe`` cannot parse it, and the resulting non-zero exit used to
be classified as ``filtered`` -- indistinguishable from a real firewall drop
(LS-01).

The probe output is classified into a status:

    open         remote exit 0, no refusal marker in output
    refused      "Connection refused" in stdout/stderr
    filtered     "timed out", "Operation timed out", or timeout(1) exit 124
    host-error   "Name or service not known" / getaddrinfo failure
    source-error the probe never ran (auth/network/broken, missing shell
                 binary, or a shell syntax error) -- NOT an answer

``--udp`` runs a UDP probe. UDP has no handshake, so silence is reported as
``open-or-filtered`` rather than guessed at.

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
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import _local_exec
import _ssh_core_locator as _locator
from _version import add_version_flag, handle_version

# --- Constants ---------------------------------------------------------------

#: What the UDP probe returns when the target has no netcat. Reported as a
#: source error, not as "port unreachable".
_NC_MISSING_EXIT = 127

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

#: A local/remote shell that could not parse the probe (``cmd.exe``:
#: ``-v was unexpected at this time``) or could not find the binary. Both mean
#: the probe **never ran**, so the exit code is not a reachability answer and
#: must not be classified as ``filtered`` (LS-01: it silently was).
SHELL_FAILURE_CLASSES = frozenset({"local_shell_error", "dependency_missing"})

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
    """Locate ssh-core's ssh_execute.py (honours DEVOPS_SSH_CORE_DIR).

    The path is returned even when it does not exist; callers check
    ``.exists()`` and emit a precondition envelope.
    """
    return _locator.ssh_core_script("ssh_execute.py")


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


def _build_probe_command(
    target: str, port: int, port_timeout: int, *, udp: bool = False
) -> str:
    """Build a single remote-shell command: nc preferred, /dev/tcp fallback.

    ``target`` is shell-quoted to keep it a single token even if exotic.
    ``port`` and ``port_timeout`` are ints, safe to format.

    UDP has no handshake, so the only observable signal is *silence*: a closed
    port answers with ICMP port-unreachable, and ``nc -zu`` turns that into a
    non-zero exit. No answer therefore means "open **or** filtered by a
    firewall" — the caller reports that as ``open-or-filtered`` instead of
    pretending it knows which.
    """
    qt = shlex.quote(target)
    if udp:
        # `nc -zu` is the only portable form: /dev/udp exists in bash but its
        # write never fails, which would report every port as reachable.
        return (
            f"if command -v nc >/dev/null 2>&1; then "
            f"nc -zu -w {port_timeout} {qt} {port} 2>&1; "
            f"else "
            f'echo "nc not available: UDP probing needs netcat" >&2; exit 127; '
            f"fi"
        )
    return (
        f"if command -v nc >/dev/null 2>&1; then "
        f"nc -zv -w {port_timeout} {qt} {port} 2>&1; "
        f"else "
        f'timeout {port_timeout} bash -c "exec 3<>/dev/tcp/{qt}/{port}" 2>&1; '
        f"fi"
    )


def _run_local_command(name: str, command: str, timeout: int) -> dict[str, Any]:
    """Run a POSIX one-liner on *this* host through the platform shell.

    ``host == "local"`` cannot go through ssh-core: its local route runs
    without a shell and refuses ``;``/``|``, which these probe one-liners use.

    The port probe itself does **not** go through here any more — see
    :func:`_probe_cell`, which uses a native socket so the "cmd.exe cannot
    parse a POSIX one-liner" class of false ``filtered`` is gone. This helper
    remains for callers that genuinely want a shell one-liner locally, and it
    now reports the "never ran" failures instead of returning a bare non-zero
    exit the classifier could mistake for an answer.
    """
    return _local_exec.run_shell(name, command, timeout)


def _probe_cell(
    ssh_exec: Path,
    source: str,
    target: str,
    port: int,
    port_timeout: int,
    connect_timeout: int,
    *,
    udp: bool = False,
) -> dict[str, Any]:
    """Probe one (source, target, port) cell and return its envelope.

    ``local`` is served by a *native* socket probe rather than by piping a
    POSIX ``nc`` one-liner through ``cmd.exe``. The envelope is shaped like
    ``nc`` output, so :func:`_classify_status` stays the single classifier for
    both routes.
    """
    if source == _local_exec.LOCAL_HOST:
        return _local_exec.port_probe(target, port, port_timeout, udp=udp)
    command = _build_probe_command(target, port, port_timeout, udp=udp)
    return _run_remote_probe(ssh_exec, source, command, connect_timeout)



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

    The local route is **not** handled here: it is served by
    :func:`_probe_cell` with a native socket probe, precisely so that no
    POSIX one-liner is ever handed to ``cmd.exe``.
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


def _classify_status(envelope: dict[str, Any], *, udp: bool = False) -> str:
    """Map a probe envelope to one of the cell statuses.

    For UDP the vocabulary is narrower: a reply means closed, silence means
    ``open-or-filtered`` (a firewall drop and an open socket are
    indistinguishable without application-level payloads), and a missing
    ``nc`` is a source error rather than an answer.

    A probe that **never ran** is a source error, never an answer. Before
    LS-01 a ``cmd.exe`` syntax error fell through to the ``filtered`` catch-all
    and a Windows host reported every port as firewalled.
    """
    failure = (envelope.get("data") or {}).get("failure_class")
    if failure in SHELL_FAILURE_CLASSES:
        return "source-error"
    if failure in {"auth", "network", "ssh_execute_broken"}:
        return "source-error"
    if failure == "timeout":
        return "filtered"

    remote_exit = envelope.get("exit_code")
    combined = (envelope.get("stdout") or "") + "\n" + (envelope.get("stderr") or "")

    if udp:
        if "nc not available" in combined or remote_exit == _NC_MISSING_EXIT:
            return "source-error"
        if _match_any(combined, REFUSED_PATTERNS) or "port unreachable" in combined.lower():
            return "refused"
        if _match_any(combined, HOST_ERROR_PATTERNS):
            return "host-error"
        if remote_exit == 0:
            return "open-or-filtered"
        return "open-or-filtered"

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
    *,
    udp: bool = False,
) -> list[dict[str, Any]]:
    """Run every (source, target, port) cell serially. Return the matrix.

    Serial by design (see design.md S 2.3): port_check's bottleneck is the
    SSH handshake; correctness first, parallelism is a v1.0.1 optimisation.
    """
    matrix: list[dict[str, Any]] = []
    for src in sources:
        for tgt in targets:
            for prt in ports:
                t0 = time.monotonic()
                env = _probe_cell(
                    ssh_exec, src, tgt, prt, port_timeout, connect_timeout,
                    udp=udp,
                )
                elapsed_ms = int(round((time.monotonic() - t0) * 1000))
                status = _classify_status(env, udp=udp)
                cell: dict[str, Any] = {
                    "source": src,
                    "target": tgt,
                    "port": prt,
                    "status": status,
                    "elapsed_ms": elapsed_ms,
                    "remote_exit_code": env.get("exit_code"),
                }
                if udp:
                    cell["protocol"] = "udp"
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
    # v0.2 scaffolding accepted --udp and did nothing; v1.1 runs the probe.
    p.add_argument(
        "--udp",
        action="store_true",
        help=(
            "Probe with UDP instead of TCP. UDP has no handshake, so a silent "
            "port is reported as 'open-or-filtered' — a firewall drop and an "
            "open socket cannot be told apart without an application payload"
        ),
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
    add_version_flag(p)
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
    early = handle_version("port_check.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

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
        udp=args.udp,
    )

    if args.udp:
        # Silence is the success signal for UDP, so "all reachable" means
        # "nothing answered with a refusal".
        ok_statuses = {"open-or-filtered"}
        ok_count = sum(1 for c in matrix if c["status"] in ok_statuses)
    else:
        ok_count = sum(1 for c in matrix if c["status"] == "open")
    total = len(matrix)
    all_open = total > 0 and ok_count == total
    any_source_error = any(c["status"] == "source-error" for c in matrix)
    overall_exit = EXIT_OK if all_open else EXIT_FAIL

    summary = {
        "open": ok_count,
        "total": total,
        "any_source_error": any_source_error,
    }
    if args.udp:
        summary["protocol"] = "udp"
        summary["note"] = (
            "UDP probes cannot distinguish an open socket from a silent "
            "firewall drop; cells are reported as open-or-filtered"
        )

    result = _envelope(
        all_open,
        overall_exit,
        stdout=_format_human(matrix),
        data={
            "sources": sources,
            "targets": targets,
            "ports": ports,
            "protocol": "udp" if args.udp else "tcp",
            "matrix": matrix,
            "summary": summary,
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
