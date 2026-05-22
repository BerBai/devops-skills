#!/usr/bin/env python3
"""inspect_container.py - single-container snapshot (v1.0).

Public surface::

    inspect_container.py <host> <name-or-id> [--tail 100]
                         [--runtime docker|podman] [--connect-timeout N]
                         [--json]

Behaviour
---------
Local host (``host == "local"``) shells out directly to ``docker``/
``podman`` via ``subprocess.run`` with an argv list (no shell, no
ssh_execute round-trip). Remote hosts (any other alias) go through
``ssh-core``'s ``ssh_execute.py`` as a CLI subprocess - per
CONTRIBUTING.md line 29 cross-plugin invocation is **always** shell-out,
never a Python import.

Per container the script makes at most two remote calls:

    1. ``<runtime> inspect --format "{{json .}}" <name>``
       Single-line JSON containing State / Config / Health / Mounts.
       Health.Log is already embedded, so we never need a second probe.

    2. ``<runtime> logs --tail N <name>``
       Plain text; we keep it as ``data.raw.logs`` and scan for warning
       keywords (panic / fatal / OOM / Killed / "stack trace").

The two are aggregated into three severity domains and a flat finding
list following ``docker-quick/SKILL.md`` lines 82-101::

    data.summary  = {state, config, logs}    # ok | warn | crit
    data.findings = [{severity, kind, value, hint}, ...]
    data.raw      = {inspect, logs, logs_tail}

Severity rules (see design.md S 4):

    state  crit    OOMKilled / ExitCode in {137,139} / Health=unhealthy
    state  warn    Status=restarting / RestartCount>5 / exited!=0
    state  ok      otherwise
    config ok      (v1.0 always; root user only emits an info finding)
    logs   warn    any LOG_KEYWORDS match (case-insensitive substring)
    logs   ok      otherwise

Top-level ``success`` is ``True`` iff the script ran to completion AND
``summary.state != "crit"`` AND no ``failure_class`` was set.

Exit codes follow ``error-handling.md``::

    0    script ran AND summary.state != crit
    1    summary.state == crit, or any failure_class set
    2    argv error
    124  ssh_execute subprocess timed out (remote only)

See:
    plugins/docker-quick/skills/docker-quick/SKILL.md           output contract
    .trellis/spec/backend/adr-001-ssh-execute.md                ssh_execute contract
    .trellis/spec/backend/error-handling.md                     JSON envelope rules
    CONTRIBUTING.md line 29                                      no cross-plugin imports
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

# --- Constants ---------------------------------------------------------------

DEFAULT_RUNTIME = "docker"
ALLOWED_RUNTIMES = ("docker", "podman")
LOG_TAIL_DEFAULT = 100

# Per-remote-call subprocess budget. Docker inspect/logs finish fast but we
# absorb the SSH handshake + connect-timeout window.
INSPECT_TIMEOUT_S = 30
LOGS_TIMEOUT_S = 30
DEFAULT_CONNECT_TIMEOUT_S = 8

# Severity rule constants.
RESTART_WARN_THRESHOLD = 5
HEALTH_LOG_VALUE_MAX_CHARS = 200
INSPECT_RAW_KEEP_BYTES = 1024  # cap on parse-error raw stdout we surface

# Critical state signals.
CRIT_EXIT_CODES = (137, 139)  # SIGKILL/OOM and SIGSEGV
HEALTH_UNHEALTHY = "unhealthy"

# Log keyword scan registry. Matched case-insensitively as substring. Order
# is only for stable test assertions.
LOG_KEYWORDS = ("panic", "fatal", "OOM", "OutOfMemory", "Killed", "stack trace")

# Severity ranks (lower = more severe; used for finding ordering).
SEVERITY_RANKS = {"crit": 0, "warn": 1, "info": 2}

# Exit codes (subset of error-handling.md).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124

# User strings that all mean "root".
ROOT_USERS = ("", "root", "0", "0:0")


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


# --- ssh_execute.py discovery (only used for remote routes) -----------------


def _ssh_execute_path() -> Path:
    """Locate ssh-core's ssh_execute.py relative to this file.

    Layout::

        plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
        plugins/docker-quick/skills/docker-quick/scripts/inspect_container.py
                                                                            ^ here

    parents[0]=scripts, [1]=docker-quick (skill), [2]=skills,
    [3]=docker-quick (plugin), [4]=plugins.
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


# --- Local execution --------------------------------------------------------


def _run_local(argv: list[str], timeout: int) -> dict[str, Any]:
    """Run a docker/podman command directly on this host (no ssh)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "inspect_container: local command timed out after "
                f"{timeout}s"
            ),
            data={"failure_class": "timeout"},
        )
    except FileNotFoundError as e:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"inspect_container: command not found: {e}\n",
            data={"failure_class": "remote_error"},
        )
    return _envelope(
        proc.returncode == 0,
        proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        data={"failure_class": None if proc.returncode == 0 else "remote_error"},
    )


# --- Remote execution via ssh_execute ---------------------------------------


def _run_via_ssh_execute(
    ssh_exec: Path,
    host: str,
    command: str,
    timeout: int,
    connect_timeout: int,
) -> dict[str, Any]:
    """Run a shell command on `host` through ssh_execute.py; return envelope."""
    argv = [
        sys.executable,
        str(ssh_exec),
        host,
        command,
        "--json",
        "--connect-timeout",
        str(connect_timeout),
        "--timeout",
        str(timeout),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout + connect_timeout + 5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "inspect_container: ssh_execute subprocess timed out after "
                f"{timeout + connect_timeout + 5}s"
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
                "inspect_container: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Per-command workers ----------------------------------------------------


def _inspect_one(
    host: str,
    name: str,
    runtime: str,
    connect_timeout: int,
    *,
    ssh_exec_override: Path | None = None,
) -> dict[str, Any]:
    """Run `<runtime> inspect --format "{{json .}}" <name>` on `host`."""
    if host == "local":
        return _run_local(
            [runtime, "inspect", "--format", "{{json .}}", name],
            INSPECT_TIMEOUT_S,
        )
    ssh_exec = ssh_exec_override or _ssh_execute_path()
    if not ssh_exec.exists():
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"inspect_container: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
    cmd = (
        f"{runtime} inspect --format '{{{{json .}}}}' {shlex.quote(name)}"
    )
    return _run_via_ssh_execute(
        ssh_exec, host, cmd, INSPECT_TIMEOUT_S, connect_timeout
    )


def _logs_one(
    host: str,
    name: str,
    tail: int,
    runtime: str,
    connect_timeout: int,
    *,
    ssh_exec_override: Path | None = None,
) -> dict[str, Any]:
    """Run `<runtime> logs --tail N <name>` on `host`."""
    if host == "local":
        return _run_local(
            [runtime, "logs", "--tail", str(tail), name],
            LOGS_TIMEOUT_S,
        )
    ssh_exec = ssh_exec_override or _ssh_execute_path()
    if not ssh_exec.exists():
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"inspect_container: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
    cmd = f"{runtime} logs --tail {tail} {shlex.quote(name)}"
    return _run_via_ssh_execute(
        ssh_exec, host, cmd, LOGS_TIMEOUT_S, connect_timeout
    )


# --- inspect JSON extraction (robust by construction; bad input -> {}) ------


def _extract_inspect(raw_json: dict[str, Any]) -> dict[str, Any]:
    """Pick the fields we actually care about from a docker inspect blob.

    Never raises; missing fields collapse to sensible defaults.
    """
    state = raw_json.get("State") or {}
    health = state.get("Health") or {}
    cfg = raw_json.get("Config") or {}

    return {
        "id": raw_json.get("Id") or "",
        "name": (raw_json.get("Name") or "").lstrip("/"),
        "image": cfg.get("Image") or raw_json.get("Image") or "",
        "restart_count": raw_json.get("RestartCount") or 0,
        "state": {
            "status": state.get("Status") or "",
            "running": bool(state.get("Running")),
            "restarting": bool(state.get("Restarting")),
            "oom_killed": bool(state.get("OOMKilled")),
            "exit_code": state.get("ExitCode") or 0,
            "error": state.get("Error") or "",
            "started_at": state.get("StartedAt") or "",
            "finished_at": state.get("FinishedAt") or "",
        },
        "health": {
            "status": health.get("Status") or "",
            "failing_streak": health.get("FailingStreak") or 0,
            "log": health.get("Log") or [],
        },
        "config": {
            "user": cfg.get("User") or "",
            "cmd": cfg.get("Cmd") or [],
            "entrypoint": cfg.get("Entrypoint") or [],
            "env_count": len(cfg.get("Env") or []),
        },
        "mounts": [
            {
                "source": m.get("Source") or "",
                "destination": m.get("Destination") or "",
                "mode": m.get("Mode") or "",
            }
            for m in (raw_json.get("Mounts") or [])
        ],
    }


# --- Severity scoring -------------------------------------------------------


def _score_state(inspect: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    state = inspect.get("state") or {}
    health = inspect.get("health") or {}
    findings: list[dict[str, Any]] = []
    sev = "ok"

    # crit-tier signals (any one trips).
    if state.get("oom_killed"):
        findings.append({
            "severity": "crit",
            "kind": "oomkilled",
            "value": True,
            "hint": "references/container_issues.md#oomkilled-exit-137",
        })
        sev = "crit"

    exit_code = state.get("exit_code") or 0
    if exit_code in CRIT_EXIT_CODES:
        findings.append({
            "severity": "crit",
            "kind": "exit_code",
            "value": exit_code,
            "hint": f"references/container_issues.md#exit-{exit_code}",
        })
        sev = "crit"

    if health.get("status") == HEALTH_UNHEALTHY:
        log_entries = health.get("log") or []
        last = (log_entries[-1] if log_entries else {}) or {}
        snippet = (last.get("Output") or "")[:HEALTH_LOG_VALUE_MAX_CHARS]
        findings.append({
            "severity": "crit",
            "kind": "health_unhealthy",
            "value": snippet,
            "hint": "references/container_issues.md#unhealthy",
        })
        sev = "crit"

    if sev == "crit":
        return sev, findings

    # warn-tier signals.
    if state.get("status") == "restarting":
        findings.append({
            "severity": "warn",
            "kind": "restarting",
            "value": True,
            "hint": "references/container_issues.md#restart-loop",
        })
        sev = "warn"

    rc = inspect.get("restart_count") or 0
    if rc > RESTART_WARN_THRESHOLD:
        findings.append({
            "severity": "warn",
            "kind": "restart_count",
            "value": rc,
            "hint": "references/container_issues.md#restart-loop",
        })
        sev = "warn"

    if state.get("status") == "exited" and exit_code != 0 and exit_code not in CRIT_EXIT_CODES:
        findings.append({
            "severity": "warn",
            "kind": "exit_code",
            "value": exit_code,
            "hint": f"references/container_issues.md#exit-{exit_code}",
        })
        sev = "warn"

    return sev, findings


def _score_config(inspect: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """v1.0 simplified: config never warns/crits; only emits info findings."""
    findings: list[dict[str, Any]] = []
    user = (inspect.get("config") or {}).get("user") or ""
    if user.strip() in ROOT_USERS:
        findings.append({
            "severity": "info",
            "kind": "running_as_root",
            "value": user or "(default root)",
            "hint": "references/container_issues.md#root-user",
        })
    return "ok", findings


def _score_logs(log_text: str) -> tuple[str, list[dict[str, Any]]]:
    if not log_text:
        return "ok", []
    lower = log_text.lower()
    matched = [kw for kw in LOG_KEYWORDS if kw.lower() in lower]
    if not matched:
        return "ok", []
    return "warn", [{
        "severity": "warn",
        "kind": "log_keyword",
        "value": matched,
        "hint": "references/container_issues.md#log-keywords",
    }]


def _severity_rank(sev: str) -> int:
    return SEVERITY_RANKS.get(sev, 3)


# --- Human formatter ---------------------------------------------------------


def _format_human(data: dict[str, Any]) -> str:
    """One-screen summary for the non-`--json` path."""
    summary = data.get("summary") or {}
    findings = data.get("findings") or []
    raw_inspect = (data.get("raw") or {}).get("inspect") or {}
    lines = [
        f"container: {data.get('target') or '(unknown)'}",
        f"host:      {data.get('host') or '(unknown)'}",
        f"runtime:   {data.get('runtime') or '(unknown)'}",
        f"summary:   state={summary.get('state', '?')}  "
        f"config={summary.get('config', '?')}  "
        f"logs={summary.get('logs', '?')}",
    ]
    state = raw_inspect.get("state") or {}
    if state:
        lines.append(
            f"state:     status={state.get('status', '?')}  "
            f"exit_code={state.get('exit_code', '?')}  "
            f"restart_count={raw_inspect.get('restart_count', '?')}"
        )
    if findings:
        lines.append("findings:")
        for f in findings:
            lines.append(
                f"  [{f['severity']:<4s}] {f['kind']:<18s} {f.get('value')}"
            )
    return "\n".join(lines) + "\n"


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="inspect_container.py",
        description=(
            "Single-container snapshot via docker/podman. host == 'local' "
            "runs the runtime directly; any other host goes through ssh-core. "
            "Emits the shared JSON contract with --json."
        ),
    )
    p.add_argument(
        "host",
        help="ssh-core alias, or the literal 'local' for direct execution",
    )
    p.add_argument(
        "name",
        help="Container name or id (prefix works in docker)",
    )
    p.add_argument(
        "--tail",
        type=int,
        default=LOG_TAIL_DEFAULT,
        help=f"Number of log lines to fetch (default: {LOG_TAIL_DEFAULT})",
    )
    p.add_argument(
        "--runtime",
        default=DEFAULT_RUNTIME,
        choices=list(ALLOWED_RUNTIMES),
        help=f"Container runtime to invoke (default: {DEFAULT_RUNTIME})",
    )
    p.add_argument(
        "--connect-timeout",
        type=int,
        default=DEFAULT_CONNECT_TIMEOUT_S,
        help=(
            "SSH ConnectTimeout for remote hosts (ignored for 'local') "
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


def _parse_inspect_stdout(stdout: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a docker inspect --format "{{json .}}" output.

    Returns (parsed_dict_or_none, parse_error_or_none).
    """
    text = (stdout or "").strip()
    if not text:
        return None, "empty stdout"
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as e:
        return None, str(e)
    if not isinstance(loaded, dict):
        return None, f"expected object, got {type(loaded).__name__}"
    return loaded, None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Step 1: inspect.
    inspect_env = _inspect_one(
        args.host, args.name, args.runtime, args.connect_timeout
    )

    # Bail early on precondition (ssh_execute missing) or remote auth/network
    # failure: we don't have inspect data to score, but we still emit a
    # well-formed envelope.
    if not inspect_env.get("success"):
        inspect_fc = (inspect_env.get("data") or {}).get("failure_class")
        result = _envelope(
            False,
            inspect_env.get("exit_code") or EXIT_FAIL,
            stderr=inspect_env.get("stderr") or "",
            data={
                "host": args.host,
                "target": args.name,
                "runtime": args.runtime,
                "summary": {"state": "crit", "config": "ok", "logs": "ok"},
                "findings": [],
                "failure_class": inspect_fc or "remote_error",
                "raw": {
                    "inspect_stdout": (inspect_env.get("stdout") or "")[:INSPECT_RAW_KEEP_BYTES],
                    "inspect_stderr": inspect_env.get("stderr") or "",
                },
            },
        )
        _emit(result, args.json)
        return result["exit_code"]

    # Parse the inspect JSON.
    parsed, parse_err = _parse_inspect_stdout(inspect_env.get("stdout") or "")
    if parsed is None:
        result = _envelope(
            False,
            EXIT_FAIL,
            stderr=f"inspect_container: failed to parse inspect output ({parse_err})\n",
            data={
                "host": args.host,
                "target": args.name,
                "runtime": args.runtime,
                "summary": {"state": "crit", "config": "ok", "logs": "ok"},
                "findings": [],
                "failure_class": "parse_error",
                "raw": {
                    "inspect_stdout": (inspect_env.get("stdout") or "")[:INSPECT_RAW_KEEP_BYTES],
                },
            },
        )
        _emit(result, args.json)
        return EXIT_FAIL

    inspect = _extract_inspect(parsed)

    # Step 2: logs (best-effort; failure here doesn't void inspect data).
    logs_env = _logs_one(
        args.host, args.name, args.tail, args.runtime, args.connect_timeout
    )
    logs_text = logs_env.get("stdout") or "" if logs_env.get("success") else ""
    logs_available = bool(logs_env.get("success"))

    # Step 3: score.
    state_sev, state_findings = _score_state(inspect)
    config_sev, config_findings = _score_config(inspect)
    log_sev, log_findings = _score_logs(logs_text) if logs_available else ("ok", [])

    findings = sorted(
        state_findings + config_findings + log_findings,
        key=lambda f: _severity_rank(f["severity"]),
    )

    summary = {"state": state_sev, "config": config_sev, "logs": log_sev}

    # Top-level success: script ran AND no crit AND logs reachable.
    failure_class = None
    if not logs_available:
        # Always use the more specific 'logs_unavailable'; the underlying
        # ssh_execute failure_class is preserved in data.raw.logs_stderr.
        failure_class = "logs_unavailable"
    success = state_sev != "crit" and failure_class is None
    overall_exit = EXIT_OK if success else EXIT_FAIL

    data = {
        "host": args.host,
        "target": args.name,
        "runtime": args.runtime,
        "summary": summary,
        "findings": findings,
        "failure_class": failure_class,
        "raw": {
            "inspect": inspect,
            "logs": logs_text,
            "logs_tail": args.tail,
        },
    }
    if not logs_available:
        data["raw"]["logs_stderr"] = logs_env.get("stderr") or ""

    result = _envelope(success, overall_exit, stdout=_format_human(data), data=data)
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
