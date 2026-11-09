#!/usr/bin/env python3
"""diagnose_host.py - 60-second health snapshot for a remote host (v1.0).

Public surface:
    python diagnose_host.py <host> [--json] [--check uptime,load,disk,mem,zombie]

Behaviour
---------
Shells out to ssh-core's ``ssh_execute.py`` (NOT imported - per
CONTRIBUTING.md line 29 the cross-plugin contract is CLI shell-out, never
a Python import) and runs a small catalogue of read-only probes:

    uptime  -- ``uptime`` (free-form text, kept as raw stdout)
    load    -- ``nproc; cat /proc/loadavg`` (parsed into cores + load_*m)
    disk    -- ``df -P / | tail -1`` (parsed into ``use_pct``)
    mem     -- ``free -m | head -2 | tail -1`` (parsed into total/used/free MB)
    zombie  -- ``ps -eo stat | grep -c '^Z' || true`` (parsed into ``count``)

Each probe's result is captured under ``data.probes[<name>]`` along with
the structured ``parsed`` payload. A coarse severity is computed from
load/disk/zombie thresholds:

    load_1m  > cores         -> warn   (> 2 * cores -> crit)
    disk_pct >= 80           -> warn   (>= 95       -> crit)
    zombies  >  5            -> warn

Top-level ``success`` is True iff every probe exited 0 AND severity is
not ``crit``. The script never raises out to the user; missing ``ssh_execute.py``
returns an envelope with ``data.failure_class = "precondition"``.

See:
    .trellis/spec/backend/adr-001-ssh-execute.md   - ssh_execute contract
    .trellis/spec/backend/error-handling.md         - JSON envelope rules
    CONTRIBUTING.md line 29                          - no cross-plugin imports
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import _local_exec
import _ssh_core_locator as _locator
from _version import add_version_flag, handle_version

# --- Probe catalogue ---------------------------------------------------------
# Each value is a single command run on the remote host through ssh_execute.
# Pipes inside these strings are evaluated by the *remote* shell -- they are
# not local subprocess pipes, so the ADR-001 D7 metachar refusal does not
# apply here.
#
# Two families exist because the probes are not portable: a Windows target has
# no ``nproc``, ``free``, ``df`` or ``/proc``. ``--target-os auto`` (the
# default) runs a one-line detector first and then picks the matching family.

PROBES: dict[str, str] = {
    "uptime": "uptime",
    "load": "nproc; cat /proc/loadavg",
    "disk": "df -P / | tail -1",
    "mem": "free -m | head -2 | tail -1",
    "zombie": "ps -eo stat | grep -c '^Z' || true",
}

# PowerShell is present on every supported Windows (5.1 ships with 10/2016+).
#
# ``-EncodedCommand`` takes base64(UTF-16LE) and reads no further arguments, so
# the whole probe is one token with no quotes, no ``$``, and no ``|``. That
# matters twice over: the local route hands the string to ``cmd /c`` (which
# eats quotes) and the remote route hands it to a POSIX shell (where ``$o``
# would be expanded by the *wrong* interpreter). Encoding sidesteps both.
#
# ``$ProgressPreference='SilentlyContinue'`` is load-bearing: without it
# ``Get-CimInstance`` writes CLIXML progress records to stderr, and every
# consumer of this envelope treats non-empty stderr as a problem worth showing.
#
# ``[Console]::OutputEncoding`` is load-bearing for the same reason one level
# down: a Chinese Windows PowerShell writes GBK by default, and the capturing
# side decodes UTF-8. Without this line ``LastBootUpTime`` came back as
# replacement characters and the whole probe looked like garbage.
_PROGRESS_OFF = (
    "$ProgressPreference='SilentlyContinue'; "
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
)

_WINDOWS_SCRIPTS: dict[str, str] = {
    "uptime": "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime",
    "load": (
        "(Get-CimInstance Win32_Processor "
        "| Measure-Object -Property LoadPercentage -Average).Average"
    ),
    "disk": (
        "$d = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' "
        "| Select-Object -First 1; "
        "if ($d) { $d | Select-Object DeviceID,Size,FreeSpace | ConvertTo-Json -Compress }"
    ),
    "mem": (
        "$o = Get-CimInstance Win32_OperatingSystem; "
        "'{0} {1}' -f [int]($o.TotalVisibleMemorySize/1KB),"
        "[int](($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)/1KB)"
    ),
    "zombie": "@(Get-Process | Where-Object { -not $_.Responding }).Count",
}


def _encoded_command(script: str) -> str:
    """Return a PowerShell ``-EncodedCommand`` invocation for ``script``."""
    payload = _PROGRESS_OFF + script
    encoded = base64.b64encode(payload.encode("utf-16-le")).decode("ascii")
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}"


PROBES_WINDOWS: dict[str, str] = {
    name: _encoded_command(script) for name, script in _WINDOWS_SCRIPTS.items()
}

#: Detector: prints ``linux`` or ``windows``, nothing else.
OS_DETECT_CMD = (
    'uname -s 2>/dev/null || echo windows'
)

OS_LINUX = "linux"
OS_WINDOWS = "windows"
TARGET_OS_CHOICES = ("auto", "linux", "windows")


# --- Severity thresholds (intentionally not flags) ---------------------------
LOAD_WARN_MULT = 1
LOAD_CRIT_MULT = 2
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 95
ZOMBIE_WARN_COUNT = 5
# Windows has no load average; the CPU percentage is the equivalent signal.
CPU_WARN_PCT = 80
CPU_CRIT_PCT = 95

# Per-probe ssh_execute subprocess budget. Generous: each probe is a tiny
# command, but the SSH handshake plus connect-timeout can take a few seconds.
PROBE_TIMEOUT_S = 30

# /proc/loadavg parsing layout: float load_1m, load_5m, load_15m, running/threads.
LOADAVG_MIN_FLOATS = 3
LOADAVG_RUNNING_THREADS_IDX = 3
LOAD_OUTPUT_MIN_LINES = 2  # ``nproc`` then ``/proc/loadavg``

# ``free -m`` columns we care about: total, used, free.
MEM_NUMS_MIN_FOR_USED = 2
MEM_NUMS_MIN_FOR_FREE = 3

# Exit codes used by this script (mirrors the small set in error-handling.md).
EXIT_OK = 0
EXIT_FAIL = 1
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
    """Locate ssh-core's ssh_execute.py.

    Delegates to ``_ssh_core_locator`` so the lookup honours
    ``$DEVOPS_SSH_CORE_DIR`` / ``$DEVOPS_SSH_CORE_SCRIPTS`` and keeps working
    when the plugins are installed into separate directories (the marketplace
    layout). Returns the candidate Path unconditionally; callers must check
    ``.exists()`` and emit a precondition envelope if it is missing.
    """
    return _locator.ssh_core_script("ssh_execute.py")


# --- Probe runner ------------------------------------------------------------


def _run_local_probe(probe_cmd: str) -> dict[str, Any]:
    """Run one probe on *this* host through the platform shell.

    ssh-core's local route deliberately runs without a shell, so it refuses
    ``;`` and ``|``. The probe one-liners are written for a POSIX shell, which
    is exactly what a local diagnostic wants; on Windows ``cmd /c`` supplies a
    shell so the commands are still interpreted rather than exec'd verbatim.

    Windows targets take the PowerShell family instead (``PROBES_WINDOWS``
    are ``-EncodedCommand`` payloads), so this only ever sees a POSIX
    one-liner when the caller forced ``--target-os linux`` on a Windows box --
    in which case the failure is now reported as a shell error rather than a
    probe that ran and returned nothing (LS-01).
    """
    return _local_exec.run_shell("diagnose_host", probe_cmd, PROBE_TIMEOUT_S)


def _run_probe(ssh_exec: Path, host: str, probe_cmd: str) -> dict[str, Any]:
    """Run one probe via ssh_execute.py. Return its JSON envelope.

    Never raises; any unexpected failure (ssh_execute crashes, stdout is
    not JSON, the subprocess itself times out) is mapped to a synthesised
    failure envelope so the caller can keep aggregating.

    ``host == "local"`` is handled here rather than delegated: ssh-core's local
    route runs without a shell and therefore refuses the ``;``/``|`` these
    probe one-liners need. Running the probe through ``sh -c`` locally keeps a
    single command shape while still letting the shell interpret the pipeline
    on the machine being diagnosed.
    """
    if host == "local":
        return _run_local_probe(probe_cmd)

    argv = [
        sys.executable,
        str(ssh_exec),
        host,
        probe_cmd,
        "--json",
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            success=False,
            exit_code=EXIT_TIMEOUT,
            stderr=(
                "diagnose_host: ssh_execute subprocess timed out after "
                f"{PROBE_TIMEOUT_S}s"
            ),
            data={"failure_class": "timeout"},
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _envelope(
            success=False,
            exit_code=proc.returncode if proc.returncode != 0 else EXIT_FAIL,
            stdout=proc.stdout,
            stderr=(
                "diagnose_host: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Parsers (robust by construction; bad input -> {}) ----------------------


def _parse_uptime(_stdout: str) -> dict[str, Any]:
    """``uptime`` output is free-form. We keep the raw text and parse nothing."""
    return {}


def _parse_load(stdout: str) -> dict[str, Any]:
    """Parse two lines emitted by ``nproc; cat /proc/loadavg``.

    Line 1: integer core count from ``nproc``.
    Line 2: ``/proc/loadavg`` -- ``0.01 0.05 0.10 1/234 5678``.
    """
    parsed: dict[str, Any] = {}
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if lines:
        with contextlib.suppress(ValueError):
            parsed["cores"] = int(lines[0])
    if len(lines) >= LOAD_OUTPUT_MIN_LINES:
        fields = lines[1].split()
        if len(fields) >= LOADAVG_MIN_FLOATS:
            try:
                parsed["load_1m"] = float(fields[0])
                parsed["load_5m"] = float(fields[1])
                parsed["load_15m"] = float(fields[2])
            except ValueError:
                pass
        if (
            len(fields) > LOADAVG_RUNNING_THREADS_IDX
            and "/" in fields[LOADAVG_RUNNING_THREADS_IDX]
        ):
            running_str, _, threads_str = fields[
                LOADAVG_RUNNING_THREADS_IDX
            ].partition("/")
            try:
                parsed["running"] = int(running_str)
                parsed["threads"] = int(threads_str)
            except ValueError:
                pass
    return parsed


def _parse_disk(stdout: str) -> dict[str, Any]:
    """Parse one ``df -P`` row. Capacity column ends with ``%``."""
    parsed: dict[str, Any] = {}
    for chunk in stdout.split():
        if chunk.endswith("%"):
            try:
                parsed["use_pct"] = int(chunk.rstrip("%"))
                break
            except ValueError:
                continue
    return parsed


def _parse_disk_windows(stdout: str) -> dict[str, Any]:
    """Parse the ``ConvertTo-Json`` row emitted by the Windows disk probe."""
    try:
        row = json.loads(stdout.strip() or "null")
    except json.JSONDecodeError:
        return {}
    if isinstance(row, list):  # single-drive hosts still come back as an array
        row = row[0] if row else None
    if not isinstance(row, dict):
        return {}
    size = row.get("Size")
    free = row.get("FreeSpace")
    if not isinstance(size, int) or not isinstance(free, int) or size <= 0:
        return {}
    parsed: dict[str, Any] = {
        "total_mb": size // (1024 * 1024),
        "free_mb": free // (1024 * 1024),
    }
    parsed["use_pct"] = int(round((size - free) * 100 / size))
    if isinstance(row.get("DeviceID"), str):
        parsed["mount"] = row["DeviceID"]
    return parsed


def _parse_cpu_windows(stdout: str) -> dict[str, Any]:
    """CPU load percentage, the Windows stand-in for a load average."""
    stripped = stdout.strip()
    if not stripped:
        return {}
    try:
        return {"cpu_pct": int(float(stripped))}
    except ValueError:
        return {}


def _parse_mem(stdout: str) -> dict[str, Any]:
    """Parse one ``free -m`` data row.

    Modern procps layout: ``Mem: total used free shared buff/cache available``.
    We extract the first three integers, which are total/used/free across
    every layout we've seen on Linux.
    """
    parsed: dict[str, Any] = {}
    nums: list[int] = []
    for chunk in stdout.split():
        try:
            nums.append(int(chunk))
        except ValueError:
            continue
    if nums:
        parsed["total_mb"] = nums[0]
    if len(nums) >= MEM_NUMS_MIN_FOR_USED:
        parsed["used_mb"] = nums[1]
    if len(nums) >= MEM_NUMS_MIN_FOR_FREE:
        parsed["free_mb"] = nums[2]
    return parsed


def _parse_mem_windows(stdout: str) -> dict[str, Any]:
    """``"<total_mb> <used_mb>"`` as emitted by the Windows memory probe."""
    nums: list[int] = []
    for chunk in stdout.split():
        try:
            nums.append(int(chunk))
        except ValueError:
            continue
    if len(nums) < MEM_NUMS_MIN_FOR_USED:
        return {}
    return {"total_mb": nums[0], "used_mb": nums[1]}


def _parse_zombie(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    try:
        return {"count": int(stripped)}
    except ValueError:
        return {}


#: Per-OS parser table. ``host_os`` selects one; keys match ``PROBES_*``.
PARSERS_BY_OS: dict[str, dict[str, Callable[[str], dict[str, Any]]]] = {
    OS_LINUX: {
        "uptime": _parse_uptime,
        "load": _parse_load,
        "disk": _parse_disk,
        "mem": _parse_mem,
        "zombie": _parse_zombie,
    },
    OS_WINDOWS: {
        "uptime": _parse_uptime,
        "load": _parse_cpu_windows,
        "disk": _parse_disk_windows,
        "mem": _parse_mem_windows,
        "zombie": _parse_zombie,
    },
}

PROBES_BY_OS: dict[str, dict[str, str]] = {
    OS_LINUX: PROBES,
    OS_WINDOWS: PROBES_WINDOWS,
}

#: Kept for backwards compatibility with the v1.0 tests and any caller that
#: imports it; the dispatch now goes through ``PARSERS_BY_OS``.
PARSERS: dict[str, Callable[[str], dict[str, Any]]] = PARSERS_BY_OS[OS_LINUX]


# --- Severity scoring --------------------------------------------------------


def _score(probes: dict[str, dict[str, Any]]) -> str:
    """Score the parsed signals as ok / warn / crit / unknown.

    ``unknown`` means no probe produced a usable answer, so there is no signal
    to score. Reporting ``ok`` in that case was actively misleading: a host
    where every probe failed (wrong alias, no shell, missing coreutils) looked
    healthy in the summary while the envelope's ``success`` was already false.
    """
    if probes and not any(block.get("success") for block in probes.values()):
        return "unknown"

    sev = "ok"

    load_parsed = probes.get("load", {}).get("parsed", {})
    load_1m = load_parsed.get("load_1m")
    cores = load_parsed.get("cores")
    if load_1m is not None and cores is not None and cores > 0:
        if load_1m > LOAD_CRIT_MULT * cores:
            sev = "crit"
        elif load_1m > LOAD_WARN_MULT * cores and sev != "crit":
            sev = "warn"

    # Windows reports a CPU percentage instead of a load average; same
    # thresholds, different scale.
    cpu_pct = load_parsed.get("cpu_pct")
    if cpu_pct is not None:
        if cpu_pct >= CPU_CRIT_PCT:
            sev = "crit"
        elif cpu_pct >= CPU_WARN_PCT and sev != "crit":
            sev = "warn"

    disk_pct = probes.get("disk", {}).get("parsed", {}).get("use_pct")
    if disk_pct is not None:
        if disk_pct >= DISK_CRIT_PCT:
            sev = "crit"
        elif disk_pct >= DISK_WARN_PCT and sev != "crit":
            sev = "warn"

    zombies = probes.get("zombie", {}).get("parsed", {}).get("count")
    if zombies is not None and zombies > ZOMBIE_WARN_COUNT and sev != "crit":
        sev = "warn"

    return sev


# --- Argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="diagnose_host.py",
        description=(
            "60-second remote host health snapshot. Shells out to "
            "ssh-core's ssh_execute.py for every probe. Read-only."
        ),
    )
    p.add_argument(
        "host",
        help="Host alias from ~/.ssh/config, or 'local'",
    )
    p.add_argument(
        "--check",
        default=None,
        help=(
            "Comma-separated probe names to run. Known probes: "
            f"{','.join(PROBES)}. Default: all of them."
        ),
    )
    p.add_argument(
        "--target-os",
        choices=TARGET_OS_CHOICES,
        default="auto",
        help=(
            "Which probe family to use. 'auto' (default) detects the target "
            "with a one-line `uname -s` probe; a Windows target gets the "
            "PowerShell family (CPU %% instead of load average, no /proc)."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the JSON output envelope on stdout",
    )
    # v0.2 backwards-compat: --since was a stub flag with no behaviour in
    # v1.0 (the probes are point-in-time snapshots). Kept hidden so existing
    # callers don't break on argparse rejection.
    p.add_argument(
        "--since",
        type=int,
        default=60,
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--skip",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )
    add_version_flag(p)
    return p


def _select_probes(
    check: str | None, probes: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """Return (selected, unknown). Unknown is empty unless --check named a
    probe that's not in the chosen probe family."""
    table = PROBES if probes is None else probes
    if not check:
        return list(table), []
    requested = [name.strip() for name in check.split(",") if name.strip()]
    unknown = [n for n in requested if n not in table]
    return requested, unknown


def _detect_target_os(ssh_exec: Path, host: str) -> str:
    """Return ``linux`` or ``windows`` for ``host``; default to linux.

    Runs one tiny probe through the same route as everything else, so the
    detector works for a remote alias and for ``local`` identically. Any
    failure keeps the historical behaviour (the Linux family): a broken
    detector must not turn a working invocation into an error.
    """
    if host == "local" and os.name == "nt":
        return OS_WINDOWS
    env = _run_probe(ssh_exec, host, OS_DETECT_CMD)
    if not env.get("success"):
        return OS_LINUX
    first = (env.get("stdout") or "").strip().splitlines()
    token = first[0].strip().lower() if first else ""
    if token.startswith("windows") or token.startswith("mingw") or token.startswith("msys"):
        return OS_WINDOWS
    return OS_LINUX


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    early = handle_version("diagnose_host.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        result = _envelope(
            success=False,
            exit_code=EXIT_FAIL,
            stderr=(
                "diagnose_host: ssh-core's ssh_execute.py not found at "
                f"{ssh_exec}. remote-debug requires ssh-core to be installed "
                "alongside it."
            ),
            data={
                "host": args.host,
                "failure_class": "precondition",
                "ssh_execute_path": str(ssh_exec),
            },
        )
        _emit(result, as_json=args.json)
        return int(result["exit_code"])

    target_os = (
        args.target_os
        if args.target_os in PROBES_BY_OS
        else _detect_target_os(ssh_exec, args.host)
    )
    probe_table = PROBES_BY_OS[target_os]
    parser_table = PARSERS_BY_OS[target_os]

    selected, unknown = _select_probes(args.check, probe_table)
    if unknown:
        result = _envelope(
            success=False,
            exit_code=EXIT_FAIL,
            stderr=(
                f"diagnose_host: unknown probe(s) in --check: {unknown}. "
                f"Known probes for a {target_os} target: {sorted(probe_table)}"
            ),
            data={
                "host": args.host,
                "target_os": target_os,
                "failure_class": "precondition",
                "unknown_probes": unknown,
            },
        )
        _emit(result, as_json=args.json)
        return int(result["exit_code"])

    probes: dict[str, dict[str, Any]] = {}
    all_ok = True
    for name in selected:
        env = _run_probe(ssh_exec, args.host, probe_table[name])
        probe_stdout = env.get("stdout", "") or ""
        parsed = parser_table[name](probe_stdout)
        probes[name] = {
            "success": bool(env.get("success", False)),
            "exit_code": int(env.get("exit_code", EXIT_FAIL)),
            "stdout": probe_stdout,
            "stderr": env.get("stderr", "") or "",
            "failure_class": (env.get("data") or {}).get("failure_class"),
            "parsed": parsed,
        }
        if not probes[name]["success"]:
            all_ok = False

    severity = _score(probes)
    overall_success = all_ok and severity not in {"crit", "unknown"}
    exit_code = EXIT_OK if overall_success else EXIT_FAIL

    ok_count = sum(1 for p in probes.values() if p["success"])
    summary = (
        f"diagnose_host {args.host} ({target_os}): severity={severity}, "
        f"{ok_count}/{len(probes)} probes ok\n"
    )

    result = _envelope(
        success=overall_success,
        exit_code=exit_code,
        stdout=summary,
        data={
            "host": args.host,
            "target_os": target_os,
            "probes": probes,
            "severity": severity,
            "checks_run": list(probes),
        },
    )
    _emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
