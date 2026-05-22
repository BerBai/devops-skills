#!/usr/bin/env python3
"""ssh_cluster.py - Concurrent SSH broadcast across a fleet (v1.0).

Public surface::

    ssh_cluster.py <command> --hosts a,b,c
                   [--max-workers N] [--health-check] [--fail-fast]
                   [--timeout T] [--connect-timeout T] [--json]

Behaviour
---------
ssh_cluster does not talk to ssh directly. Each target host is delegated
to a subprocess call of the **same-directory** ``ssh_execute.py``; the
two scripts share a ``lib/`` package for the envelope helpers but never
import each other (see design.md S 7). That keeps failure_class /
connect-timeout / noise filtering single-source in ssh_execute, and
keeps ssh_cluster a pure orchestrator on top.

Per call the workflow is::

    1. resolve targets        --hosts only (v1.0 scope)
    2. health-check           optional probe with `true`
    3. broadcast              ThreadPoolExecutor(max_workers)
    4. aggregate              per-host envelopes + top failure_class

The aggregated envelope follows ``error-handling.md`` and the host loop
contract used by ``tail_log`` / ``compare_across_hosts`` / etc.::

    data.command         echoed command
    data.hosts           resolved aliases, in declaration order
    data.summary         {total, ok, fail, skipped, elapsed_ms}
    data.results         dict[alias, per_host_envelope]
    data.failure_class   null | target_resolution_failed
                              | ssh_execute_missing
                              | partial_failure | all_hosts_failed
    data.fail_fast       echoed flag
    data.health_check    echoed flag

Top-level ``success`` is True iff every host envelope says success.
Otherwise the failure_class differentiates partial vs total.

Exit codes follow ``error-handling.md``::

    0    every host succeeded
    1    one or more host failed
    2    argv error
    124  worker subprocess timed out (per-host only; top-level just
         reflects "fail")

See:
    plugins/ssh-core/skills/ssh-core/SKILL.md           Cluster section
    .trellis/spec/backend/adr-001-ssh-execute.md        ssh_execute contract
    .trellis/spec/backend/error-handling.md             JSON envelope rules
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from lib import emit, json_result  # noqa: E402, I001

# --- Constants ---------------------------------------------------------------

DEFAULT_MAX_WORKERS = 8       # mirrors sshd MaxStartups defaults
DEFAULT_TIMEOUT = 120         # business command budget (seconds)
DEFAULT_CONNECT_TIMEOUT = 8   # SSH ConnectTimeout passed to ssh_execute
HEALTH_CHECK_TIMEOUT = 5      # short probe command budget
WORKER_TIMEOUT_PADDING = 5    # extra seconds on top of ssh_execute timeout

# Exit codes (subset of error-handling.md).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# --- ssh_execute.py discovery (same directory) -------------------------------


def _ssh_execute_path() -> Path:
    """Locate ssh_execute.py inside the SAME directory as this script.

    Unlike cross-plugin scripts (tail_log, inspect_container) which walk
    parents[4], ssh_cluster lives next to ssh_execute and uses the
    relative path.
    """
    return Path(__file__).resolve().parent / "ssh_execute.py"


# --- per-host envelope helpers ----------------------------------------------


def _per_host_env(
    success: bool,
    exit_code: int,
    alias: str,
    *,
    elapsed_ms: int,
    stdout: str = "",
    stderr: str = "",
    failure_class: str | None = None,
    skipped: bool = False,
) -> dict[str, Any]:
    """Build the per-host result envelope with a fixed 8-field schema."""
    return {
        "alias": alias,
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_ms": elapsed_ms,
        "failure_class": failure_class,
        "skipped": skipped,
    }


# --- target resolution -------------------------------------------------------


def _parse_targets(
    hosts_arg: str | None,
    tags_arg: str | None,
    env_arg: str | None,
) -> tuple[list[str] | None, str | None]:
    """Turn --hosts into an ordered, deduped alias list.

    v1.0 ignores --tags / --environment (caller emits a warning).
    Returns (aliases, None) on success, (None, "target_resolution_failed")
    when --hosts is empty or absent.
    """
    if not hosts_arg or not hosts_arg.strip():
        return None, "target_resolution_failed"

    aliases: list[str] = []
    seen: set[str] = set()
    for raw in hosts_arg.split(","):
        cleaned = raw.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        aliases.append(cleaned)

    if not aliases:
        return None, "target_resolution_failed"
    # tags_arg / env_arg accepted but unused; main() warns separately.
    _ = (tags_arg, env_arg)
    return aliases, None


# --- worker: delegate to ssh_execute.py via subprocess -----------------------


def _run_one(
    ssh_exec: Path,
    alias: str,
    command: str,
    timeout: int,
    connect_timeout: int,
) -> dict[str, Any]:
    """Run `command` on `alias` by shelling out to ssh_execute.py.

    Never imports ssh_execute -- argv-list subprocess only. The result is
    normalized into a per-host envelope; failure_class is promoted from
    ssh_execute's envelope when present.
    """
    argv = [
        sys.executable,
        str(ssh_exec),
        alias,
        command,
        "--json",
        "--timeout",
        str(timeout),
        "--connect-timeout",
        str(connect_timeout),
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout + connect_timeout + WORKER_TIMEOUT_PADDING,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _per_host_env(
            False,
            EXIT_TIMEOUT,
            alias,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failure_class="timeout",
            stderr=(
                f"ssh_cluster: worker timed out after "
                f"{timeout + connect_timeout + WORKER_TIMEOUT_PADDING}s"
            ),
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)

    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _per_host_env(
            False,
            proc.returncode if proc.returncode != 0 else EXIT_FAIL,
            alias,
            elapsed_ms=elapsed_ms,
            failure_class="ssh_execute_broken",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    inner_data = env.get("data") or {}
    return _per_host_env(
        bool(env.get("success", False)),
        int(env.get("exit_code", EXIT_FAIL)),
        alias,
        elapsed_ms=elapsed_ms,
        stdout=env.get("stdout", "") or "",
        stderr=env.get("stderr", "") or "",
        failure_class=inner_data.get("failure_class"),
    )


# --- health-check phase ------------------------------------------------------


def _health_check(
    ssh_exec: Path,
    aliases: list[str],
    connect_timeout: int,
    max_workers: int,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Probe each host with `true`. Returns (alive, dead).

    `alive` is the subset that responded ok, preserving input order so
    the subsequent broadcast keeps deterministic alias ordering.
    `dead` is a dict of failed-probe envelopes keyed by alias.
    """
    workers = max(1, min(max_workers, len(aliases)))
    futures: dict[str, Future[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for alias in aliases:
            futures[alias] = pool.submit(
                _run_one, ssh_exec, alias, "true",
                HEALTH_CHECK_TIMEOUT, connect_timeout,
            )
        alive: list[str] = []
        dead: dict[str, dict[str, Any]] = {}
        for alias in aliases:
            env = futures[alias].result()
            if env["success"]:
                alive.append(alias)
            else:
                env["failure_class"] = "health_check_failed"
                env["skipped"] = True
                dead[alias] = env
    return alive, dead


# --- broadcast phase ---------------------------------------------------------


def _broadcast_serial(
    ssh_exec: Path,
    aliases: list[str],
    command: str,
    timeout: int,
    connect_timeout: int,
    fail_fast: bool,
) -> dict[str, dict[str, Any]]:
    """Sequential broadcast. Preserves alias order. Used when
    max_workers <= 1 so tests can assert deterministic ordering."""
    results: dict[str, dict[str, Any]] = {}
    for idx, alias in enumerate(aliases):
        env = _run_one(ssh_exec, alias, command, timeout, connect_timeout)
        results[alias] = env
        if fail_fast and not env["success"]:
            for skip in aliases[idx + 1:]:
                results[skip] = _per_host_env(
                    False, EXIT_FAIL, skip,
                    elapsed_ms=0,
                    failure_class="skipped_fail_fast",
                    skipped=True,
                )
            break
    return results


def _broadcast_parallel(
    ssh_exec: Path,
    aliases: list[str],
    command: str,
    timeout: int,
    connect_timeout: int,
    max_workers: int,
    fail_fast: bool,
) -> dict[str, dict[str, Any]]:
    """Concurrent broadcast via ThreadPoolExecutor.

    fail-fast cancels futures that have NOT yet started; already-started
    futures run to completion (no thread cancellation in stdlib).
    """
    workers = max(1, min(max_workers, len(aliases)))
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: dict[str, Future[dict[str, Any]]] = {
            alias: pool.submit(
                _run_one, ssh_exec, alias, command, timeout, connect_timeout,
            )
            for alias in aliases
        }

        if not fail_fast:
            for alias in aliases:
                results[alias] = futures[alias].result()
            return results

        # fail-fast: collect via as_completed, stop on first failure
        first_fail_seen = False
        for fut in as_completed(futures.values()):
            alias = next(a for a, f in futures.items() if f is fut)
            env = fut.result()
            results[alias] = env
            if not env["success"]:
                first_fail_seen = True
                break

        if first_fail_seen:
            for alias, fut in futures.items():
                if alias in results:
                    continue
                if fut.cancel():
                    results[alias] = _per_host_env(
                        False, EXIT_FAIL, alias,
                        elapsed_ms=0,
                        failure_class="skipped_fail_fast",
                        skipped=True,
                    )
                else:
                    # Already running -- wait it out
                    results[alias] = fut.result()
        else:
            # No failure encountered (defensive)
            for alias, fut in futures.items():
                if alias not in results:
                    results[alias] = fut.result()
    return results


def _broadcast(
    ssh_exec: Path,
    aliases: list[str],
    command: str,
    timeout: int,
    connect_timeout: int,
    max_workers: int,
    fail_fast: bool,
) -> dict[str, dict[str, Any]]:
    if max_workers <= 1 or len(aliases) <= 1:
        return _broadcast_serial(
            ssh_exec, aliases, command, timeout, connect_timeout, fail_fast,
        )
    return _broadcast_parallel(
        ssh_exec, aliases, command, timeout, connect_timeout,
        max_workers, fail_fast,
    )


# --- aggregation -------------------------------------------------------------


def _classify_top(
    results: dict[str, dict[str, Any]],
) -> tuple[bool, str | None]:
    total = len(results)
    if total == 0:
        return False, "target_resolution_failed"
    ok = sum(1 for env in results.values() if env["success"])
    if ok == total:
        return True, None
    if ok == 0:
        return False, "all_hosts_failed"
    return False, "partial_failure"


def _summarize(
    results: dict[str, dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    total = len(results)
    ok = sum(1 for env in results.values() if env["success"])
    skipped = sum(1 for env in results.values() if env.get("skipped"))
    fail = total - ok - skipped
    return {
        "total": total,
        "ok": ok,
        "fail": fail,
        "skipped": skipped,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
    }


# --- human formatter ---------------------------------------------------------


_STDOUT_HEAD_CAP = 80


def _format_human(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    results = data.get("results") or {}
    lines = [
        f"command:  {data.get('command') or ''}",
        f"hosts:    {len(results)}  "
        f"(ok={summary.get('ok', 0)}  fail={summary.get('fail', 0)}  "
        f"skipped={summary.get('skipped', 0)})",
        f"elapsed:  {summary.get('elapsed_ms', 0)} ms",
        "",
    ]
    for alias, env in results.items():
        tag = "ok  " if env.get("success") else "FAIL"
        head = (env.get("stdout") or "").splitlines()[0] if env.get("stdout") else ""
        if len(head) > _STDOUT_HEAD_CAP:
            head = head[:_STDOUT_HEAD_CAP] + "..."
        fc = env.get("failure_class") or "-"
        lines.append(
            f"  [{tag}] {alias:<24s} "
            f"exit={env.get('exit_code', '?'):<3}  "
            f"elapsed={env.get('elapsed_ms', 0):>5}ms  "
            f"fc={fc:<22s} {head}"
        )
    if data.get("failure_class"):
        lines.append("")
        lines.append(f"failure_class: {data['failure_class']}")
    return "\n".join(lines) + "\n"


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssh_cluster.py",
        description=(
            "Broadcast one command across a fleet. Delegates each host "
            "to ssh_execute.py via subprocess. v1.0 only accepts "
            "--hosts; --tags / --environment are reserved for "
            "ssh_config_manager integration in a later cut."
        ),
    )
    p.add_argument("command", help="Remote command string (passed to ssh_execute)")
    p.add_argument("--hosts", help="Comma-separated host aliases")
    p.add_argument(
        "--tags",
        help=(
            "RESERVED for v1.1: ssh-core config metadata tag filter. "
            "v1.0 emits a warning and requires --hosts."
        ),
    )
    p.add_argument(
        "--environment",
        help=(
            "RESERVED for v1.1: ssh-core config metadata environment "
            "filter. v1.0 emits a warning and requires --hosts."
        ),
    )
    p.add_argument(
        "--parallel",
        action="store_true",
        help=(
            "Reserved flag. Concurrency in v1.0 is governed entirely by "
            "--max-workers; passing --parallel is a no-op."
        ),
    )
    p.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=(
            f"Thread pool size (default: {DEFAULT_MAX_WORKERS}). "
            "Set to 1 for deterministic serial execution."
        ),
    )
    p.add_argument(
        "--health-check",
        action="store_true",
        help="Probe each host with `true` first; skip hosts that fail",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help=(
            "Stop submitting new hosts after the first failure. "
            "Already-running workers complete; unstarted hosts get "
            "failure_class=skipped_fail_fast."
        ),
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Per-host command timeout (default: {DEFAULT_TIMEOUT}s)",
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT,
        help=f"SSH ConnectTimeout (default: {DEFAULT_CONNECT_TIMEOUT}s)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the shared JSON output contract on stdout",
    )
    return p


# --- main --------------------------------------------------------------------


def _emit_top_fail(
    args: argparse.Namespace,
    failure_class: str,
    message: str,
) -> int:
    data = {
        "command": args.command,
        "hosts": [],
        "summary": {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "elapsed_ms": 0},
        "results": {},
        "failure_class": failure_class,
        "fail_fast": args.fail_fast,
        "health_check": args.health_check,
    }
    emit(
        json_result(False, EXIT_FAIL, stderr=message + "\n", data=data),
        args.json,
    )
    return EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = time.monotonic()

    if args.tags or args.environment:
        sys.stderr.write(
            "ssh_cluster: --tags / --environment are reserved for v1.1; "
            "use --hosts explicitly in v1.0\n"
        )
        if not args.hosts:
            return _emit_top_fail(
                args,
                "target_resolution_failed",
                "ssh_cluster: --hosts is required "
                "(--tags / --environment unsupported in v1.0)",
            )

    aliases, err = _parse_targets(args.hosts, args.tags, args.environment)
    if aliases is None:
        return _emit_top_fail(
            args, err or "target_resolution_failed",
            "ssh_cluster: --hosts cannot be empty",
        )

    ssh_exec = _ssh_execute_path()
    if not ssh_exec.exists():
        return _emit_top_fail(
            args, "ssh_execute_missing",
            f"ssh_cluster: ssh_execute.py not found at {ssh_exec}",
        )

    dead: dict[str, dict[str, Any]] = {}
    if args.health_check:
        aliases, dead = _health_check(
            ssh_exec, aliases, args.connect_timeout, args.max_workers,
        )

    if aliases:
        results = _broadcast(
            ssh_exec, aliases, args.command, args.timeout,
            args.connect_timeout, args.max_workers, args.fail_fast,
        )
    else:
        results = {}

    for alias, env in dead.items():
        results[alias] = env

    success, fc = _classify_top(results)
    summary = _summarize(results, started_at)

    data = {
        "command": args.command,
        "hosts": list(results.keys()),
        "summary": summary,
        "results": results,
        "failure_class": fc,
        "fail_fast": args.fail_fast,
        "health_check": args.health_check,
    }
    code = EXIT_OK if success else EXIT_FAIL
    stdout = "" if args.json else _format_human(data)
    emit(json_result(success, code, stdout=stdout, data=data), args.json)
    return code


if __name__ == "__main__":
    sys.exit(main())
