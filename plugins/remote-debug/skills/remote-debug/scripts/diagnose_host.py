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
import contextlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# --- Probe catalogue ---------------------------------------------------------
# Each value is a single command run on the remote host through ssh_execute.
# Pipes inside these strings are evaluated by the *remote* shell -- they are
# not local subprocess pipes, so the ADR-001 D7 metachar refusal does not
# apply here.

PROBES: dict[str, str] = {
    "uptime": "uptime",
    "load": "nproc; cat /proc/loadavg",
    "disk": "df -P / | tail -1",
    "mem": "free -m | head -2 | tail -1",
    "zombie": "ps -eo stat | grep -c '^Z' || true",
}

# --- Severity thresholds (intentionally not flags) ---------------------------
LOAD_WARN_MULT = 1
LOAD_CRIT_MULT = 2
DISK_WARN_PCT = 80
DISK_CRIT_PCT = 95
ZOMBIE_WARN_COUNT = 5

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

    Layout (parents counted from this script)::

        plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
        plugins/remote-debug/skills/remote-debug/scripts/diagnose_host.py
                                                                       ^ here

    parents[0] = scripts/, parents[1] = remote-debug/ (skill dir),
    parents[2] = skills/,  parents[3] = remote-debug/ (plugin dir),
    parents[4] = plugins/. So the sibling plugin lives at parents[4] /
    "ssh-core" / ... .

    Returns the candidate Path unconditionally; callers must check
    ``.exists()`` and emit a precondition envelope if it is missing.
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


# --- Probe runner ------------------------------------------------------------


def _run_probe(ssh_exec: Path, host: str, probe_cmd: str) -> dict[str, Any]:
    """Run one probe via ssh_execute.py. Return its JSON envelope.

    Never raises; any unexpected failure (ssh_execute crashes, stdout is
    not JSON, the subprocess itself times out) is mapped to a synthesised
    failure envelope so the caller can keep aggregating.
    """
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


def _parse_zombie(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    try:
        return {"count": int(stripped)}
    except ValueError:
        return {}


PARSERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "uptime": _parse_uptime,
    "load": _parse_load,
    "disk": _parse_disk,
    "mem": _parse_mem,
    "zombie": _parse_zombie,
}


# --- Severity scoring --------------------------------------------------------


def _score(probes: dict[str, dict[str, Any]]) -> str:
    sev = "ok"

    load_parsed = probes.get("load", {}).get("parsed", {})
    load_1m = load_parsed.get("load_1m")
    cores = load_parsed.get("cores")
    if load_1m is not None and cores is not None and cores > 0:
        if load_1m > LOAD_CRIT_MULT * cores:
            sev = "crit"
        elif load_1m > LOAD_WARN_MULT * cores and sev != "crit":
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
    return p


def _select_probes(check: str | None) -> tuple[list[str], list[str]]:
    """Return (selected, unknown). Unknown is empty unless --check named a
    probe that's not in PROBES."""
    if not check:
        return list(PROBES), []
    requested = [name.strip() for name in check.split(",") if name.strip()]
    unknown = [n for n in requested if n not in PROBES]
    return requested, unknown


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
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

    selected, unknown = _select_probes(args.check)
    if unknown:
        result = _envelope(
            success=False,
            exit_code=EXIT_FAIL,
            stderr=(
                f"diagnose_host: unknown probe(s) in --check: {unknown}. "
                f"Known probes: {sorted(PROBES)}"
            ),
            data={
                "host": args.host,
                "failure_class": "precondition",
                "unknown_probes": unknown,
            },
        )
        _emit(result, as_json=args.json)
        return int(result["exit_code"])

    probes: dict[str, dict[str, Any]] = {}
    all_ok = True
    for name in selected:
        env = _run_probe(ssh_exec, args.host, PROBES[name])
        probe_stdout = env.get("stdout", "") or ""
        parsed = PARSERS[name](probe_stdout)
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
    overall_success = all_ok and severity != "crit"
    exit_code = EXIT_OK if overall_success else EXIT_FAIL

    ok_count = sum(1 for p in probes.values() if p["success"])
    summary = (
        f"diagnose_host {args.host}: severity={severity}, "
        f"{ok_count}/{len(probes)} probes ok\n"
    )

    result = _envelope(
        success=overall_success,
        exit_code=exit_code,
        stdout=summary,
        data={
            "host": args.host,
            "probes": probes,
            "severity": severity,
            "checks_run": list(probes),
        },
    )
    _emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
