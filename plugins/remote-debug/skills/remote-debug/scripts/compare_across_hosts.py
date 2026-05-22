#!/usr/bin/env python3
"""compare_across_hosts.py - diff state across N hosts (v1.0).

The highest-leverage tool when "it works on the other one". Pulls the
same files / runs the same commands on each host, then diffs against a
baseline (the first host in the argument list).

Public surface::

    compare_across_hosts.py <alias1> <alias2> [<alias3> ...]
        [--files /etc/nginx/nginx.conf,/etc/sysctl.conf]
        [--commands "uname -r" "nginx -V" "systemctl list-units --state=failed"]
        [--packages]           # diff installed pkg lists (dpkg/rpm autodetect)
        [--context 3]          # unified diff context lines
        [--json]

Behaviour
---------
Shells out to ``ssh-core``'s ``ssh_execute.py`` (NEVER imported - per
CONTRIBUTING.md line 29). For each comparison kind (file / command /
packages) the script:

  1. Pulls the baseline content from ``aliases[0]``.
  2. Pulls the same content from each ``aliases[i]`` (i >= 1).
  3. Computes ``difflib.unified_diff`` against the baseline.
  4. Annotates the result with ``differs: bool`` and pulls failed
     cells into ``error: {side, remote_exit_code, remote_stderr}``.

Cross-distro package comparisons (one host runs dpkg, the other rpm)
are NOT normalised; the script marks ``differs=true`` and writes
``unified_diff = "(distro mismatch: dpkg vs rpm)"``. Normalising pkg
names across ecosystems is a v1.1+ topic.

Exit codes follow ``error-handling.md``::

    0    every comparison succeeded AND differs=false
    1    any comparison differs OR any cell errored
    2    argv error (<2 aliases, no comparison mode selected)
    124  any ssh_execute subprocess timed out

See:
    .trellis/spec/backend/adr-001-ssh-execute.md   ssh_execute contract
    .trellis/spec/backend/error-handling.md         JSON envelope rules
    CONTRIBUTING.md line 29                          no cross-plugin imports
"""

from __future__ import annotations

import argparse
import difflib
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

# --- Constants ---------------------------------------------------------------

# Per-cell ssh_execute subprocess budget. File reads and package list pulls
# can be slow on busy hosts; we give them more headroom than port_check.
CELL_SUBPROCESS_TIMEOUT_S = 60

# Default SSH ConnectTimeout forwarded to ssh_execute.
DEFAULT_CONNECT_TIMEOUT_S = 8

# Default unified-diff context lines (matches the v0.2 stub).
DEFAULT_CONTEXT = 3

# Distro detection sentinel: the remote autodetect script exits 99 when
# neither dpkg nor rpm is present. Picked to be distinct from common
# command exit codes (1, 2, 126, 127, 128+N) but still <= 255.
NO_PKG_MANAGER_EXIT_CODE = 99

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


def _cmd_cat_file(path: str) -> str:
    return f"cat {shlex.quote(path)}"


# A self-contained remote shell pipeline that autodetects the pkg manager,
# emits a deterministic ``name=version`` list (sorted), and exits 99 when
# neither dpkg nor rpm is available. Each branch ends with `exit` to make
# the overall remote exit code unambiguous.
_PKG_REMOTE_CMD = (
    "if command -v dpkg >/dev/null 2>&1; then "
    "echo '__PKG_MANAGER=dpkg'; "
    "dpkg -l | awk '/^ii/ {print $2\"=\"$3}' | sort; "
    "elif command -v rpm >/dev/null 2>&1; then "
    "echo '__PKG_MANAGER=rpm'; "
    "rpm -qa --queryformat '%{NAME}=%{VERSION}\\n' | sort; "
    f"else echo '__PKG_MANAGER=none' >&2; exit {NO_PKG_MANAGER_EXIT_CODE}; "
    "fi"
)


def _cmd_packages() -> str:
    return _PKG_REMOTE_CMD


# --- Remote fetch -----------------------------------------------------------


def _run_remote(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
) -> dict[str, Any]:
    """Run a command on `alias` through ssh_execute.py; return its envelope."""
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
            timeout=CELL_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "compare_across_hosts: ssh_execute subprocess timed out "
                f"after {CELL_SUBPROCESS_TIMEOUT_S}s"
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
                "compare_across_hosts: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Cell-level fetching ----------------------------------------------------


def _fetch_cell(
    ssh_exec: Path,
    alias: str,
    command: str,
    connect_timeout: int,
) -> tuple[str | None, dict[str, Any]]:
    """Pull content from one host.

    Returns ``(content_or_none, info_dict)``. ``content`` is the remote
    stdout when the remote command succeeded; ``None`` otherwise. The
    info dict carries ``remote_exit_code``, ``failure_class`` (if any),
    and ``remote_stderr`` for caller diagnostics.
    """
    env = _run_remote(ssh_exec, alias, command, connect_timeout)
    remote_exit = env.get("exit_code", EXIT_FAIL)
    failure_class = (env.get("data") or {}).get("failure_class")
    info = {
        "remote_exit_code": remote_exit,
        "failure_class": failure_class,
        "remote_stderr": env.get("stderr") or "",
    }
    if env.get("success") is True and remote_exit == 0 and failure_class is None:
        return env.get("stdout") or "", info
    return None, info


def _fetch_packages_cell(
    ssh_exec: Path,
    alias: str,
    connect_timeout: int,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Pull package list from one host, also returning the detected manager.

    Returns ``(content_or_none, manager_or_none, info_dict)``. The
    manager is parsed from the leading ``__PKG_MANAGER=<name>`` marker
    written by ``_PKG_REMOTE_CMD``. ``None`` content means the host had
    no pkg manager (or ssh failed); ``None`` manager means we never saw
    the marker.
    """
    content, info = _fetch_cell(ssh_exec, alias, _cmd_packages(), connect_timeout)
    if content is None:
        # Distinguish "no pkg manager" (remote exit 99) from real failures.
        if info["remote_exit_code"] == NO_PKG_MANAGER_EXIT_CODE:
            info["pkg_manager"] = "none"
        return None, None, info

    # Strip the marker line (first line) and return the rest.
    marker = "__PKG_MANAGER="
    lines = content.split("\n")
    if lines and lines[0].startswith(marker):
        manager = lines[0][len(marker):].strip()
        remaining = "\n".join(lines[1:])
        return remaining, manager, info
    # No marker -- treat as broken/old output.
    return content, None, info


# --- Diff -------------------------------------------------------------------


def _unified(
    a: str,
    b: str,
    a_label: str,
    b_label: str,
    context: int,
) -> str:
    """Return a unified diff (empty string when identical)."""
    a_lines = a.splitlines(keepends=True)
    b_lines = b.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            a_lines, b_lines, fromfile=a_label, tofile=b_label, n=context
        )
    )


# --- Comparison builders ----------------------------------------------------


def _build_file_comparison(
    ssh_exec: Path,
    baseline_host: str,
    other_host: str,
    path: str,
    connect_timeout: int,
    context: int,
) -> dict[str, Any]:
    base_content, base_info = _fetch_cell(
        ssh_exec, baseline_host, _cmd_cat_file(path), connect_timeout
    )
    other_content, other_info = _fetch_cell(
        ssh_exec, other_host, _cmd_cat_file(path), connect_timeout
    )
    return _assemble_comparison(
        kind="file",
        target=path,
        baseline_host=baseline_host,
        other_host=other_host,
        base_content=base_content,
        other_content=other_content,
        base_info=base_info,
        other_info=other_info,
        context=context,
    )


def _build_command_comparison(
    ssh_exec: Path,
    baseline_host: str,
    other_host: str,
    command: str,
    connect_timeout: int,
    context: int,
) -> dict[str, Any]:
    base_content, base_info = _fetch_cell(
        ssh_exec, baseline_host, command, connect_timeout
    )
    other_content, other_info = _fetch_cell(
        ssh_exec, other_host, command, connect_timeout
    )
    return _assemble_comparison(
        kind="command",
        target=command,
        baseline_host=baseline_host,
        other_host=other_host,
        base_content=base_content,
        other_content=other_content,
        base_info=base_info,
        other_info=other_info,
        context=context,
    )


def _build_packages_comparison(
    ssh_exec: Path,
    baseline_host: str,
    other_host: str,
    connect_timeout: int,
    context: int,
) -> dict[str, Any]:
    base_content, base_mgr, base_info = _fetch_packages_cell(
        ssh_exec, baseline_host, connect_timeout
    )
    other_content, other_mgr, other_info = _fetch_packages_cell(
        ssh_exec, other_host, connect_timeout
    )
    cell: dict[str, Any] = {
        "kind": "packages",
        "target": "(autodetect)",
        "baseline_host": baseline_host,
        "other_host": other_host,
        "baseline_pkg_manager": base_mgr,
        "other_pkg_manager": other_mgr,
    }

    # Both successful but distros differ -> mark differs, don't try to
    # normalise names across ecosystems (design.md §4.2 / §4.5).
    if (
        base_content is not None
        and other_content is not None
        and base_mgr is not None
        and other_mgr is not None
        and base_mgr != other_mgr
    ):
        cell["differs"] = True
        cell["unified_diff"] = f"(distro mismatch: {base_mgr} vs {other_mgr})"
        return cell

    # Otherwise fall through to the generic assembler.
    cell.update(
        _assemble_comparison(
            kind="packages",
            target="(autodetect)",
            baseline_host=baseline_host,
            other_host=other_host,
            base_content=base_content,
            other_content=other_content,
            base_info=base_info,
            other_info=other_info,
            context=context,
        )
    )
    # _assemble_comparison overwrote our pkg_manager fields; restore them.
    cell["baseline_pkg_manager"] = base_mgr
    cell["other_pkg_manager"] = other_mgr
    return cell


def _assemble_comparison(
    kind: str,
    target: str,
    baseline_host: str,
    other_host: str,
    base_content: str | None,
    other_content: str | None,
    base_info: dict[str, Any],
    other_info: dict[str, Any],
    context: int,
) -> dict[str, Any]:
    """Common assembler: handle error-side detection + diff computation."""
    cell: dict[str, Any] = {
        "kind": kind,
        "target": target,
        "baseline_host": baseline_host,
        "other_host": other_host,
        "baseline_exit_code": base_info["remote_exit_code"],
        "other_exit_code": other_info["remote_exit_code"],
    }

    # Surface the first side that errored. If both did, we report baseline.
    if base_content is None and other_content is None:
        cell["error"] = {
            "side": "both",
            "baseline_stderr": base_info["remote_stderr"],
            "other_stderr": other_info["remote_stderr"],
        }
        cell["differs"] = False
        cell["unified_diff"] = ""
        return cell
    if base_content is None:
        cell["error"] = {
            "side": "baseline",
            "remote_exit_code": base_info["remote_exit_code"],
            "remote_stderr": base_info["remote_stderr"],
            "failure_class": base_info["failure_class"],
        }
        cell["differs"] = False
        cell["unified_diff"] = ""
        return cell
    if other_content is None:
        cell["error"] = {
            "side": "other",
            "remote_exit_code": other_info["remote_exit_code"],
            "remote_stderr": other_info["remote_stderr"],
            "failure_class": other_info["failure_class"],
        }
        cell["differs"] = False
        cell["unified_diff"] = ""
        return cell

    diff = _unified(
        base_content,
        other_content,
        f"{baseline_host}:{target}",
        f"{other_host}:{target}",
        context,
    )
    cell["differs"] = bool(diff)
    cell["unified_diff"] = diff
    return cell


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compare_across_hosts.py",
        description=(
            "Diff configuration / command output / installed packages "
            "across two or more hosts. Emits the shared JSON contract "
            "with --json."
        ),
    )
    p.add_argument(
        "aliases",
        nargs="+",
        help="Two or more host aliases. The first is the baseline.",
    )
    p.add_argument(
        "--files",
        help="Comma-separated remote file paths to diff",
    )
    p.add_argument(
        "--commands",
        nargs="+",
        default=[],
        help='Each command, e.g. --commands "uname -r" "nginx -V"',
    )
    p.add_argument(
        "--packages",
        action="store_true",
        help="Compare installed packages (dpkg/rpm autodetect)",
    )
    p.add_argument(
        "--context",
        type=int,
        default=DEFAULT_CONTEXT,
        help=f"Unified diff context lines (default: {DEFAULT_CONTEXT})",
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


def _parse_file_list(spec: str | None) -> list[str]:
    if not spec:
        return []
    return [s.strip() for s in spec.split(",") if s.strip()]


def _summarise(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    differs_count = sum(1 for c in comparisons if c["differs"])
    by_kind: dict[str, int] = {}
    for c in comparisons:
        by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + (1 if c["differs"] else 0)
    error_count = sum(1 for c in comparisons if c.get("error"))
    return {
        "differs_count": differs_count,
        "total": len(comparisons),
        "by_kind": by_kind,
        "error_count": error_count,
    }


def _format_human(comparisons: list[dict[str, Any]]) -> str:
    """One line per comparison; differs / same / error markers."""
    if not comparisons:
        return ""
    out: list[str] = []
    for c in comparisons:
        label = f"[{c['kind']:8s}] {c['target']}"
        if c.get("error"):
            tag = f"error({c['error']['side']})"
        elif c["differs"]:
            tag = "differs"
        else:
            tag = "same"
        out.append(f"  {tag:<15s}  {label}")
    return "\n".join(out) + "\n"


MIN_ALIASES = 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if len(args.aliases) < MIN_ALIASES:
        sys.stderr.write(
            "compare_across_hosts: need at least two host aliases to compare\n"
        )
        return EXIT_ARGS

    files = _parse_file_list(args.files)
    commands = list(args.commands)
    do_packages = bool(args.packages)
    if not files and not commands and not do_packages:
        sys.stderr.write(
            "compare_across_hosts: need at least one of "
            "--files / --commands / --packages\n"
        )
        return EXIT_ARGS

    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        result = _envelope(
            False,
            EXIT_FAIL,
            stderr=f"compare_across_hosts: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
        _emit(result, args.json)
        return EXIT_FAIL

    baseline = args.aliases[0]
    others = args.aliases[1:]
    comparisons: list[dict[str, Any]] = []

    for other in others:
        for path in files:
            comparisons.append(
                _build_file_comparison(
                    ssh_exec, baseline, other, path,
                    args.connect_timeout, args.context,
                )
            )
        for cmd in commands:
            comparisons.append(
                _build_command_comparison(
                    ssh_exec, baseline, other, cmd,
                    args.connect_timeout, args.context,
                )
            )
        if do_packages:
            comparisons.append(
                _build_packages_comparison(
                    ssh_exec, baseline, other,
                    args.connect_timeout, args.context,
                )
            )

    summary = _summarise(comparisons)
    all_same_no_errors = summary["differs_count"] == 0 and summary["error_count"] == 0

    # Propagate timeout exit if any cell saw one.
    any_timeout = any(
        (c.get("error") or {}).get("failure_class") == "timeout"
        for c in comparisons
    )
    if any_timeout and not all_same_no_errors:
        overall_exit = EXIT_TIMEOUT
    elif all_same_no_errors:
        overall_exit = EXIT_OK
    else:
        overall_exit = EXIT_FAIL

    result = _envelope(
        all_same_no_errors,
        overall_exit,
        stdout=_format_human(comparisons),
        data={
            "baseline": baseline,
            "others": others,
            "comparisons": comparisons,
            "summary": summary,
        },
    )
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
