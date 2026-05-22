#!/usr/bin/env python3
"""compose_status.py - Docker Compose stack snapshot (v1.0).

Public surface::

    compose_status.py <host> <project-dir> [--service NAME]
                      [--connect-timeout N] [--json]

Behaviour
---------
Local host (``host == "local"``) shells out directly to ``docker compose``
via ``subprocess.run`` with an argv list and ``cwd=<project-dir>``. Remote
hosts (any other alias) go through ``ssh-core``'s ``ssh_execute.py`` as
a CLI subprocess, using ``cd <quoted-dir> && docker compose ...`` since
ssh_execute takes a single command string. Per CONTRIBUTING.md L29 cross-
plugin invocation is **always** shell-out, never a Python import.

Per call the script issues at most ``1 + N`` docker invocations where
``N`` is the count of services that are not ``running`` and not healthy
(those get a follow-up ``docker inspect`` to pick up RestartCount and the
Health.Log tail). Healthy/running services rely on the ``ps`` row only.

The aggregated envelope follows ``docker-quick/SKILL.md`` lines 82-101::

    data.summary  = {state, services_total, services_running}
    data.services = [ps + optional inspect overlay, ...]
    data.findings = [{severity, kind, value, hint}, ...]
    data.raw      = {ps_stdout, ps_parser}

Severity rules (see design.md S 6):

    crit    any service Health.Status == "unhealthy"
    warn    any service State == "restarting" / RestartCount > 5 /
            State == "exited" with ExitCode != 0 / services_total == 0
    ok      otherwise

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

LIST_TIMEOUT_S = 30
INSPECT_TIMEOUT_S = 30
DEFAULT_CONNECT_TIMEOUT_S = 8

RESTART_WARN_THRESHOLD = 5
HEALTH_LOG_VALUE_MAX_CHARS = 200
PS_RAW_KEEP_BYTES = 2048

HEALTH_UNHEALTHY = "unhealthy"

SEVERITY_RANKS = {"crit": 0, "warn": 1, "info": 2}

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


# --- ssh_execute.py discovery (only used for remote routes) -----------------


def _ssh_execute_path() -> Path:
    """Locate ssh-core's ssh_execute.py relative to this file.

    Layout::

        plugins/ssh-core/skills/ssh-core/scripts/ssh_execute.py
        plugins/docker-quick/skills/docker-quick/scripts/compose_status.py
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


def _run_local(
    argv: list[str],
    timeout: int,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run a docker/compose command directly on this host (no ssh)."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _envelope(
            False,
            EXIT_TIMEOUT,
            stderr=(
                "compose_status: local command timed out after "
                f"{timeout}s"
            ),
            data={"failure_class": "timeout"},
        )
    except FileNotFoundError as e:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"compose_status: command not found: {e}\n",
            data={"failure_class": "remote_error"},
        )
    except NotADirectoryError as e:
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"compose_status: cwd not a directory: {e}\n",
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
                "compose_status: ssh_execute subprocess timed out after "
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
                "compose_status: ssh_execute produced non-JSON output. "
                f"stderr={proc.stderr!r}"
            ),
            data={"failure_class": "ssh_execute_broken"},
        )


# --- Per-command workers ----------------------------------------------------


def _list_services(
    host: str,
    project_dir: str,
    connect_timeout: int,
    *,
    ssh_exec_override: Path | None = None,
) -> dict[str, Any]:
    """Run `docker compose ps --format json --all` in `project_dir` on `host`."""
    if host == "local":
        return _run_local(
            ["docker", "compose", "ps", "--format", "json", "--all"],
            LIST_TIMEOUT_S,
            cwd=project_dir,
        )
    ssh_exec = ssh_exec_override or _ssh_execute_path()
    if not ssh_exec.exists():
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"compose_status: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
    cmd = (
        f"cd {shlex.quote(project_dir)} && "
        "docker compose ps --format json --all"
    )
    return _run_via_ssh_execute(
        ssh_exec, host, cmd, LIST_TIMEOUT_S, connect_timeout
    )


def _inspect_container_one(
    host: str,
    container_name: str,
    connect_timeout: int,
    *,
    ssh_exec_override: Path | None = None,
) -> dict[str, Any]:
    """Run `docker inspect --format "{{json .}}" <name>` on `host`."""
    if host == "local":
        return _run_local(
            ["docker", "inspect", "--format", "{{json .}}", container_name],
            INSPECT_TIMEOUT_S,
        )
    ssh_exec = ssh_exec_override or _ssh_execute_path()
    if not ssh_exec.exists():
        return _envelope(
            False,
            EXIT_FAIL,
            stderr=f"compose_status: ssh_execute.py not found at {ssh_exec}\n",
            data={
                "failure_class": "precondition",
                "expected_path": str(ssh_exec),
            },
        )
    cmd = (
        f"docker inspect --format '{{{{json .}}}}' {shlex.quote(container_name)}"
    )
    return _run_via_ssh_execute(
        ssh_exec, host, cmd, INSPECT_TIMEOUT_S, connect_timeout
    )


# --- compose ps output parser (handles JSON Lines or JSON Array) ------------


def _parse_ps_output(
    stdout: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Parse `docker compose ps --format json` output.

    Two valid shapes (varies by docker compose version):

    - JSON Lines: one service object per line (docker compose v2.21+).
    - JSON Array: ``[ {...}, {...} ]`` (early v2 / some plugins).

    Returns (services_list, error_message_or_none). Empty stdout maps to
    an empty list (no services running) -- not an error.
    """
    text = (stdout or "").strip()
    if not text:
        return [], None
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as e:
            return None, f"JSON array parse failed: {e}"
        if not isinstance(loaded, list):
            return None, f"expected array, got {type(loaded).__name__}"
        return [s for s in loaded if isinstance(s, dict)], None
    # JSON Lines
    services: list[dict[str, Any]] = []
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as e:
            return None, f"line {idx} JSON parse failed: {e}"
        if isinstance(obj, dict):
            services.append(obj)
    return services, None


def _parse_inspect_blob(
    stdout: str,
) -> tuple[dict[str, Any] | None, str | None]:
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


# --- Per-service inspect overlay --------------------------------------------


def _overlay_inspect(svc: dict[str, Any], inspect_blob: dict[str, Any]) -> None:
    """Mutate `svc` to add restart_count / health_log from inspect blob."""
    state = inspect_blob.get("State") or {}
    health = state.get("Health") or {}
    svc["restart_count"] = inspect_blob.get("RestartCount") or 0
    log_entries = health.get("Log") or []
    last = (log_entries[-1] if log_entries else {}) or {}
    snippet = (last.get("Output") or "")[:HEALTH_LOG_VALUE_MAX_CHARS]
    svc["health_log_tail"] = snippet
    svc["state_inspect"] = {
        "running": bool(state.get("Running")),
        "restarting": bool(state.get("Restarting")),
        "exit_code": state.get("ExitCode") or 0,
        "error": state.get("Error") or "",
        "health_status": health.get("Status") or "",
    }


def _needs_inspect(svc: dict[str, Any]) -> bool:
    state = str(svc.get("State") or "").lower()
    health = str(svc.get("Health") or "").lower()
    return state != "running" or health == HEALTH_UNHEALTHY


# --- Severity scoring -------------------------------------------------------


def _classify_service(svc: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate findings for a single service. Read both ps row and any
    inspect overlay we attached earlier."""
    findings: list[dict[str, Any]] = []
    name = svc.get("Service") or svc.get("Name") or "(unknown)"
    state = str(svc.get("State") or "").lower()
    health = str(svc.get("Health") or "").lower()
    restart_count = svc.get("restart_count") or 0

    if health == HEALTH_UNHEALTHY:
        findings.append({
            "severity": "crit",
            "kind": "unhealthy",
            "value": {
                "service": name,
                "log": svc.get("health_log_tail") or "",
            },
            "hint": "references/container_issues.md#unhealthy",
        })
        return findings

    if state == "restarting":
        findings.append({
            "severity": "warn",
            "kind": "restart_loop",
            "value": {"service": name, "restart_count": restart_count},
            "hint": "references/container_issues.md#restart-loop",
        })
        return findings

    if restart_count > RESTART_WARN_THRESHOLD:
        findings.append({
            "severity": "warn",
            "kind": "restart_loop",
            "value": {"service": name, "restart_count": restart_count},
            "hint": "references/container_issues.md#restart-loop",
        })

    if state == "exited":
        overlay = svc.get("state_inspect") or {}
        exit_code = overlay.get("exit_code") or svc.get("ExitCode") or 0
        if exit_code != 0:
            findings.append({
                "severity": "warn",
                "kind": "exited_nonzero",
                "value": {"service": name, "exit_code": exit_code},
                "hint": f"references/container_issues.md#exit-{exit_code}",
            })

    return findings


def _score_services(
    services: list[dict[str, Any]],
    service_filter: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    state = "ok"

    if not services:
        findings.append({
            "severity": "warn",
            "kind": "empty_stack",
            "value": None,
            "hint": "references/compose_debug.md#empty-stack",
        })
        return "warn", findings

    if service_filter:
        match = any(
            (svc.get("Service") == service_filter
             or svc.get("Name") == service_filter)
            for svc in services
        )
        if not match:
            findings.append({
                "severity": "warn",
                "kind": "service_not_found",
                "value": service_filter,
                "hint": "references/compose_debug.md#service-not-found",
            })
            return "warn", findings

    for svc in services:
        if service_filter and not (
            svc.get("Service") == service_filter
            or svc.get("Name") == service_filter
        ):
            continue
        findings.extend(_classify_service(svc))

    severities = {f["severity"] for f in findings}
    if "crit" in severities:
        state = "crit"
    elif "warn" in severities:
        state = "warn"
    return state, findings


def _severity_rank(sev: str) -> int:
    return SEVERITY_RANKS.get(sev, 3)


# --- Human formatter ---------------------------------------------------------


def _format_human(data: dict[str, Any]) -> str:
    summary = data.get("summary") or {}
    findings = data.get("findings") or []
    services = data.get("services") or []
    lines = [
        f"host:        {data.get('host') or '(unknown)'}",
        f"project:     {data.get('project_dir') or '(unknown)'}",
        f"summary:     state={summary.get('state', '?')}  "
        f"services={summary.get('services_running', 0)}/"
        f"{summary.get('services_total', 0)} running",
    ]
    if data.get("service"):
        lines.append(f"service:     {data['service']}")
    if services:
        lines.append("services:")
        for svc in services:
            tag = svc.get("Service") or svc.get("Name") or "(unknown)"
            state = svc.get("State") or "?"
            health = svc.get("Health") or "-"
            rc = svc.get("restart_count")
            extra = f" restart_count={rc}" if rc is not None else ""
            err = " inspect_error" if svc.get("inspect_error") else ""
            lines.append(
                f"  [{state:<10s}] {tag:<24s} health={health}{extra}{err}"
            )
    if findings:
        lines.append("findings:")
        for f in findings:
            lines.append(
                f"  [{f['severity']:<4s}] {f['kind']:<20s} {f.get('value')}"
            )
    return "\n".join(lines) + "\n"


# --- argparse ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compose_status.py",
        description=(
            "Docker Compose stack snapshot via docker compose ps. "
            "host == 'local' runs the runtime directly with cwd=<project_dir>; "
            "any other host goes through ssh-core. Emits the shared JSON "
            "contract with --json. Docker only -- podman-compose not supported."
        ),
    )
    p.add_argument(
        "host",
        help="ssh-core alias, or the literal 'local' for direct execution",
    )
    p.add_argument(
        "project_dir",
        help="Directory on <host> containing compose.yml/compose.yaml",
    )
    p.add_argument(
        "--service",
        default=None,
        help="Focus on one service (default: all)",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Step 1: list services via `docker compose ps`.
    list_env = _list_services(
        args.host, args.project_dir, args.connect_timeout
    )

    if not list_env.get("success"):
        fc = (list_env.get("data") or {}).get("failure_class") or "remote_error"
        result = _envelope(
            False,
            list_env.get("exit_code") or EXIT_FAIL,
            stderr=list_env.get("stderr") or "",
            data={
                "host": args.host,
                "project_dir": args.project_dir,
                "service": args.service,
                "summary": {
                    "state": "crit",
                    "services_total": 0,
                    "services_running": 0,
                },
                "services": [],
                "findings": [],
                "failure_class": fc,
                "raw": {
                    "ps_stdout": (list_env.get("stdout") or "")[:PS_RAW_KEEP_BYTES],
                    "ps_stderr": list_env.get("stderr") or "",
                },
            },
        )
        _emit(result, args.json)
        return result["exit_code"]

    # Step 2: parse ps output.
    services, parse_err = _parse_ps_output(list_env.get("stdout") or "")
    if services is None:
        result = _envelope(
            False,
            EXIT_FAIL,
            stderr=f"compose_status: ps output parse failed ({parse_err})\n",
            data={
                "host": args.host,
                "project_dir": args.project_dir,
                "service": args.service,
                "summary": {
                    "state": "crit",
                    "services_total": 0,
                    "services_running": 0,
                },
                "services": [],
                "findings": [],
                "failure_class": "parse_error",
                "raw": {
                    "ps_stdout": (list_env.get("stdout") or "")[:PS_RAW_KEEP_BYTES],
                },
            },
        )
        _emit(result, args.json)
        return EXIT_FAIL

    # Step 3: per-service inspect overlay for non-ok services.
    for svc in services:
        if not _needs_inspect(svc):
            continue
        container_name = svc.get("Name") or svc.get("Service")
        if not container_name:
            continue
        env = _inspect_container_one(
            args.host, container_name, args.connect_timeout
        )
        if not env.get("success"):
            svc["inspect_error"] = True
            svc["inspect_stderr"] = env.get("stderr") or ""
            continue
        blob, _err = _parse_inspect_blob(env.get("stdout") or "")
        if blob is None:
            svc["inspect_error"] = True
            continue
        _overlay_inspect(svc, blob)

    # Step 4: score + findings.
    state, findings = _score_services(services, args.service)
    findings = sorted(findings, key=lambda f: _severity_rank(f["severity"]))

    services_total = (
        len([s for s in services
             if (not args.service)
             or s.get("Service") == args.service
             or s.get("Name") == args.service])
        if args.service else len(services)
    )
    services_running = len([
        s for s in services
        if str(s.get("State") or "").lower() == "running"
        and ((not args.service)
             or s.get("Service") == args.service
             or s.get("Name") == args.service)
    ])

    summary = {
        "state": state,
        "services_total": services_total,
        "services_running": services_running,
    }

    failure_class = None
    if args.service and any(
        f["kind"] == "service_not_found" for f in findings
    ):
        failure_class = "service_not_found"

    success = state != "crit" and failure_class is None
    overall_exit = EXIT_OK if success else EXIT_FAIL

    data = {
        "host": args.host,
        "project_dir": args.project_dir,
        "service": args.service,
        "summary": summary,
        "services": services,
        "findings": findings,
        "failure_class": failure_class,
        "raw": {
            "ps_stdout": (list_env.get("stdout") or "")[:PS_RAW_KEEP_BYTES],
        },
    }

    result = _envelope(success, overall_exit, stdout=_format_human(data), data=data)
    _emit(result, args.json)
    return overall_exit


if __name__ == "__main__":
    sys.exit(main())
