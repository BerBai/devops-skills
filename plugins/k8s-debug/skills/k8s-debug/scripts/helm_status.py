#!/usr/bin/env python3
"""helm_status.py - Helm release health (v1.0)."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result, msys_safe_env  # type: ignore[import-not-found]  # noqa: E402, I001
from lib.kubectl import KubectlError, run_kubectl  # type: ignore[import-not-found]  # noqa: E402

_TEN_MB = 10 * 1024 * 1024
_TRUNCATE_MARKER = "\n... <truncated 10MB cap>\n"
_DEFAULT_TIMEOUT = 60
_HOOK_STUCK_SECONDS = 5 * 60
_TOP_FINDINGS = 5

_HELM_MISSING = 110
_RELEASE_NOT_FOUND = 111
_HELM_VERSION_UNSUPPORTED = 112
_HISTORY_TRUNCATED = 113
_PENDING_SCAN_PARTIAL = 114
_TIMEOUT = 124
_RC_COMMAND_NOT_FOUND = 127
_MIN_HELM_MAJOR = 3

_PENDING_STATUSES = {"pending-install", "pending-upgrade", "pending-rollback"}
_CRIT_STATUSES = {"failed", "pending-install", "pending-upgrade", "pending-rollback"}
_WARN_STATUSES = {"superseded", "uninstalling", "uninstalled"}
_KNOWN_STATUSES = _PENDING_STATUSES | _CRIT_STATUSES | _WARN_STATUSES | {"deployed"}
_SEVERITY_ORDER = {"crit": 0, "warn": 1, "ok": 2}

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[4]
_SSH_EXECUTE_PATH = _REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts" / "ssh_execute.py"


@dataclass(frozen=True)
class HelmResult:
    stdout: str
    stderr: str
    returncode: int
    elapsed_s: float
    argv: list[str]
    route: str


class HelmError(Exception):
    def __init__(self, failure_class: str, exit_code: int, stderr: str) -> None:
        self.failure_class = failure_class
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(f"helm failed [{failure_class}]: {stderr}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="helm_status.py")
    parser.add_argument("host")
    parser.add_argument("namespace", nargs="?", default=None)
    parser.add_argument("release", nargs="?", default=None)
    parser.add_argument("--list-pending", action="store_true", help="List all pending-* releases cluster-wide")
    parser.add_argument("--json", action="store_true")
    return parser


def _truncate_10mb(value: str) -> str:
    if len(value) > _TEN_MB:
        return value[:_TEN_MB] + _TRUNCATE_MARKER
    return value


def _json_loads(stdout: str) -> Any:
    try:
        return json.loads(stdout or "null")
    except json.JSONDecodeError:
        return None


def _dict_field(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _list_from_json(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("releases", "history", "items"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _finding(
    severity: str,
    kind: str,
    name: str,
    reason: str,
    message: str,
    *,
    namespace: str = "",
    hint: str = "",
) -> dict[str, Any]:
    out = {
        "severity": severity,
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "reason": reason,
        "message": message,
    }
    if hint:
        out["hint"] = hint
    return out


def _top_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(str(f.get("severity")), 9),
            str(f.get("kind") or ""),
            str(f.get("namespace") or ""),
            str(f.get("name") or ""),
            str(f.get("reason") or ""),
        ),
    )[:_TOP_FINDINGS]


def _classify_helm_failure(returncode: int, stderr: str) -> tuple[str, int] | None:
    lower = stderr.lower()
    if returncode == 0:
        return None
    if returncode == _RC_COMMAND_NOT_FOUND or "command not found" in lower or "executable file not found" in lower:
        return "helm_missing", _HELM_MISSING
    if "not found" in lower and "release" in lower:
        return "release_not_found", _RELEASE_NOT_FOUND
    if "no deployed releases" in lower:
        return "release_not_found", _RELEASE_NOT_FOUND
    return None


def _run_helm_local(args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> HelmResult:
    argv = ["helm", *args]
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=msys_safe_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HelmError("timeout", _TIMEOUT, f"helm timeout after {timeout}s: {exc}") from None
    except FileNotFoundError:
        raise HelmError("helm_missing", _HELM_MISSING, "helm binary not found in PATH") from None
    elapsed = round(time.monotonic() - start, 3)
    classified = _classify_helm_failure(proc.returncode, proc.stderr)
    if classified:
        failure_class, exit_code = classified
        raise HelmError(failure_class, exit_code, proc.stderr)
    if proc.returncode != 0:
        raise HelmError("helm_failed", 1, proc.stderr)
    return HelmResult(proc.stdout, proc.stderr, proc.returncode, elapsed, argv, "local")


def _run_helm_remote(host: str, args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> HelmResult:
    if not _SSH_EXECUTE_PATH.exists():
        raise HelmError("host_unreachable", 80, f"ssh-core ssh_execute.py not found at {_SSH_EXECUTE_PATH}")
    helm_argv = ["helm", *args]
    command_str = " ".join(shlex.quote(part) for part in helm_argv)
    ssh_argv = [sys.executable, str(_SSH_EXECUTE_PATH), host, command_str, "--timeout", str(timeout), "--json"]
    start = time.monotonic()
    try:
        proc = subprocess.run(ssh_argv, capture_output=True, text=True, timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired as exc:
        raise HelmError("host_unreachable", 80, f"ssh_execute timeout after {timeout + 15}s: {exc}") from None
    elapsed = round(time.monotonic() - start, 3)
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HelmError("host_unreachable", 80, f"ssh_execute returned non-JSON output: {exc}") from None
    ssh_fc = envelope.get("data", {}).get("failure_class")
    if ssh_fc in {"network", "timeout"}:
        raise HelmError("host_unreachable", 80, envelope.get("stderr", "ssh transport failed"))
    if ssh_fc == "auth":
        raise HelmError("forbidden", 85, envelope.get("stderr", "ssh authentication failed"))
    remote_rc = int(envelope.get("exit_code", 0))
    remote_stdout = str(envelope.get("stdout") or "")
    remote_stderr = str(envelope.get("stderr") or "")
    classified = _classify_helm_failure(remote_rc, remote_stderr)
    if classified:
        failure_class, exit_code = classified
        raise HelmError(failure_class, exit_code, remote_stderr)
    if remote_rc != 0:
        raise HelmError("helm_failed", 1, remote_stderr)
    return HelmResult(remote_stdout, remote_stderr, remote_rc, elapsed, helm_argv, "remote")


def run_helm(host: str, args: list[str], timeout: int = _DEFAULT_TIMEOUT) -> HelmResult:
    if host == "local":
        return _run_helm_local(args, timeout)
    return _run_helm_remote(host, args, timeout)


def _helm_major(version_stdout: str) -> int | None:
    # Handles 'v3.14.0+g...' and 'version.BuildInfo{Version:"v3.14.0"...}'.
    marker = "Version:\""
    if marker in version_stdout:
        version_stdout = version_stdout.split(marker, 1)[1]
    for token in version_stdout.replace(",", " ").replace('"', " ").split():
        if token.startswith("v") and len(token) > 1 and token[1].isdigit():
            return int(token[1:].split(".", 1)[0])
    return None


def _ensure_helm3(host: str) -> HelmResult:
    result = run_helm(host, ["version", "--short"], timeout=15)
    major = _helm_major(result.stdout)
    if major is not None and major < _MIN_HELM_MAJOR:
        raise HelmError("helm_version_unsupported", _HELM_VERSION_UNSUPPORTED, result.stdout.strip())
    return result


def _release_status(payload: Any) -> str:
    if isinstance(payload, dict):
        for path in (("status", "info", "status"), ("info", "status"), ("status",)):
            cur: Any = payload
            for key in path:
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(key)
            if isinstance(cur, str) and cur:
                return cur.lower()
    return "unknown"


def _history_entries(payload: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in _list_from_json(payload):
        revision_int = _to_int(item.get("revision"))
        entries.append(
            {
                "revision": revision_int,
                "status": str(item.get("status") or "").lower(),
                "chart": str(item.get("chart") or ""),
                "app_version": str(item.get("app_version") or item.get("appVersion") or ""),
                "updated": str(item.get("updated") or ""),
                "description": str(item.get("description") or ""),
            }
        )
    return sorted(entries, key=lambda item: _to_int(item.get("revision")), reverse=True)[:5]


def _pending_rows(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _list_from_json(payload):
        status = str(item.get("status") or "").lower()
        if status not in _PENDING_STATUSES:
            continue
        revision_int = _to_int(item.get("revision"))
        rows.append(
            {
                "namespace": str(item.get("namespace") or ""),
                "name": str(item.get("name") or ""),
                "status": status,
                "revision": revision_int,
                "updated": str(item.get("updated") or ""),
                "chart": str(item.get("chart") or ""),
            }
        )
    return sorted(rows, key=lambda item: (item["namespace"], item["name"]))


def _parse_k8s_time(value: str) -> float | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _job_condition(job: dict[str, Any], condition_type: str) -> bool:
    conditions = _dict_field(job, "status").get("conditions")
    if not isinstance(conditions, list):
        return False
    return any(isinstance(c, dict) and c.get("type") == condition_type and c.get("status") == "True" for c in conditions)


def _normalize_jobs(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    now = time.time()
    all_jobs: list[dict[str, Any]] = []
    stuck: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for job in _list_from_json(payload):
        metadata = _dict_field(job, "metadata")
        status = _dict_field(job, "status")
        name = str(metadata.get("name") or "")
        started = str(status.get("startTime") or metadata.get("creationTimestamp") or "")
        started_ts = _parse_k8s_time(started)
        active = int(status.get("active") or 0)
        succeeded = int(status.get("succeeded") or 0)
        failed_count = int(status.get("failed") or 0)
        row = {
            "name": name,
            "active": active,
            "succeeded": succeeded,
            "failed": failed_count,
            "start_time": started,
        }
        all_jobs.append(row)
        if failed_count > 0 or _job_condition(job, "Failed"):
            failed.append(row)
        elif active > 0 and succeeded == 0 and started_ts is not None and now - started_ts > _HOOK_STUCK_SECONDS:
            stuck.append(row)
    return {"all": all_jobs, "stuck": stuck, "failed": failed}


def _release_findings(
    namespace: str,
    release: str,
    status: str,
    associated_jobs: dict[str, list[dict[str, Any]]],
    history_warning: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if status in _CRIT_STATUSES:
        findings.append(
            _finding(
                "crit",
                "HelmRelease",
                release,
                status,
                f"release status is {status}",
                namespace=namespace,
                hint="references/common_issues.md#helm-release-stuck-pending-upgrade--pending-rollback",
            )
        )
    elif status in _WARN_STATUSES or status not in _KNOWN_STATUSES:
        findings.append(_finding("warn", "HelmRelease", release, status or "unknown", f"release status is {status}", namespace=namespace))
    for job in associated_jobs["failed"]:
        findings.append(_finding("crit", "Job", job["name"], "Failed", "associated hook job failed", namespace=namespace))
    for job in associated_jobs["stuck"]:
        findings.append(_finding("crit", "Job", job["name"], "Stuck", "associated hook job is still active after 5 minutes", namespace=namespace))
    if history_warning:
        findings.append(_finding("warn", "HelmHistory", release, "history_truncated", "release history unavailable", namespace=namespace))
    return findings


def _summary(status: str, associated_jobs: dict[str, list[dict[str, Any]]], history_warning: bool) -> str:
    if status in _CRIT_STATUSES or associated_jobs["failed"] or associated_jobs["stuck"]:
        return "crit"
    if status in _WARN_STATUSES or status not in _KNOWN_STATUSES or history_warning:
        return "warn"
    return "ok"


def _recommend(status: str, associated_jobs: dict[str, list[dict[str, Any]]]) -> str:
    if status in _PENDING_STATUSES and (associated_jobs["failed"] or associated_jobs["stuck"]):
        return "inspect/delete stuck hook job, then rollback"
    if status in _PENDING_STATUSES:
        return "inspect Helm history and rollback to last deployed revision"
    if status == "failed":
        return "inspect failed revision resources and rollback"
    if associated_jobs["failed"] or associated_jobs["stuck"]:
        return "inspect/delete associated hook job"
    return "no action"


def _per_release(args: argparse.Namespace, helm_version: HelmResult) -> dict[str, Any]:
    namespace = str(args.namespace)
    release = str(args.release)
    status_result = run_helm(args.host, ["-n", namespace, "status", release, "-o", "json"])
    status_payload = _json_loads(status_result.stdout)
    status = _release_status(status_payload)

    raw = {"helm_version": _truncate_10mb(helm_version.stdout), "status": _truncate_10mb(status_result.stdout)}
    partial_failures: list[dict[str, Any]] = []
    history_warning = False
    history: list[dict[str, Any]] = []
    try:
        history_result = run_helm(args.host, ["-n", namespace, "history", release, "-o", "json"])
        raw["history"] = _truncate_10mb(history_result.stdout)
        history = _history_entries(_json_loads(history_result.stdout))
    except HelmError as exc:
        history_warning = True
        partial_failures.append(_partial_failure("history", exc.failure_class, exc.exit_code, exc.stderr))

    try:
        jobs_result = run_kubectl(
            args.host,
            namespace,
            ["get", "jobs", "-l", f"app.kubernetes.io/instance={release}"],
            use_json=True,
            timeout=30,
        )
        raw["jobs"] = _truncate_10mb(jobs_result.stdout)
        associated_jobs = _normalize_jobs(_json_loads(jobs_result.stdout) if isinstance(_json_loads(jobs_result.stdout), dict) else {})
    except KubectlError as exc:
        associated_jobs = {"all": [], "stuck": [], "failed": []}
        partial_failures.append(_partial_failure("jobs", exc.failure_class, exc.exit_code, exc.stderr))

    findings = _release_findings(namespace, release, status, associated_jobs, history_warning)
    return json_result(
        success=True,
        exit_code=0,
        data={
            "host": args.host,
            "namespace": namespace,
            "release": release,
            "list_pending": False,
            "status": status,
            "history": history,
            "associated_jobs": associated_jobs,
            "stuck_resources": [],
            "stuck_resources_deferred": True,
            "summary": _summary(status, associated_jobs, history_warning),
            "recommended_next_step": _recommend(status, associated_jobs),
            "findings": _top_findings(findings),
            "all_findings_count": len(findings),
            "partial_failures": partial_failures,
            "raw": raw,
            "failure_class": None,
        },
    )


def _list_pending(args: argparse.Namespace, helm_version: HelmResult) -> dict[str, Any]:
    result = run_helm(args.host, ["list", "--all-namespaces", "--pending", "-o", "json"])
    pending = _pending_rows(_json_loads(result.stdout))
    findings = [
        _finding(
            "crit",
            "HelmRelease",
            row["name"],
            row["status"],
            f"release status is {row['status']}",
            namespace=row["namespace"],
            hint="references/common_issues.md#helm-release-stuck-pending-upgrade--pending-rollback",
        )
        for row in pending
    ]
    return json_result(
        success=True,
        exit_code=0,
        data={
            "host": args.host,
            "list_pending": True,
            "pending_releases": pending,
            "summary": "crit" if pending else "ok",
            "findings": _top_findings(findings),
            "all_findings_count": len(findings),
            "raw": {"helm_version": _truncate_10mb(helm_version.stdout), "pending": _truncate_10mb(result.stdout)},
            "failure_class": None,
        },
    )


def _partial_failure(domain: str, failure_class: str, exit_code: int, stderr: str) -> dict[str, Any]:
    return {"domain": domain, "failure_class": failure_class, "exit_code": exit_code, "stderr": stderr}


def _validate_args(args: argparse.Namespace) -> HelmError | None:
    if args.list_pending and (args.namespace or args.release):
        return HelmError("bad_args", 2, "--list-pending cannot be combined with <namespace> <release>")
    if not args.list_pending and (not args.namespace or not args.release):
        return HelmError("bad_args", 2, "Need <namespace> <release>, or --list-pending.")
    return None


def _format_human(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    if data.get("list_pending"):
        lines = [f"Pending Helm releases on {data.get('host', '')}"]
        pending = data.get("pending_releases") or []
        if not pending:
            lines.append("none")
        for row in pending:
            lines.append(f"- {row.get('namespace')}/{row.get('name')}: {row.get('status')}")
        return "\n".join(lines) + "\n"

    lines = [
        f"Helm release {data.get('namespace')}/{data.get('release')} on {data.get('host', '')}",
        f"status      {data.get('status')}",
        f"summary     {data.get('summary')}",
        f"next_step   {data.get('recommended_next_step')}",
    ]
    findings = data.get("findings") or []
    if findings:
        lines.append("Findings:")
        for finding in findings:
            lines.append(f"- {finding.get('severity')} {finding.get('kind')} {finding.get('name')}: {finding.get('reason')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    arg_error = _validate_args(args)
    if arg_error is not None:
        result = json_result(False, arg_error.exit_code, stderr=arg_error.stderr, data={"failure_class": arg_error.failure_class})
        emit(result, as_json=args.json)
        return arg_error.exit_code
    try:
        helm_version = _ensure_helm3(args.host)
        result = _list_pending(args, helm_version) if args.list_pending else _per_release(args, helm_version)
    except HelmError as exc:
        result = json_result(
            success=False,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
            data={
                "host": args.host,
                "namespace": args.namespace,
                "release": args.release,
                "list_pending": args.list_pending,
                "failure_class": exc.failure_class,
            },
        )
    except KubectlError as exc:
        result = json_result(
            success=False,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
            data={
                "host": args.host,
                "namespace": args.namespace,
                "release": args.release,
                "list_pending": args.list_pending,
                "failure_class": exc.failure_class,
            },
        )
    except Exception as exc:  # pragma: no cover - last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"helm_status unexpected error: {type(exc).__name__}: {exc}",
            data={"host": args.host, "failure_class": None},
        )

    if not args.json and result.get("success"):
        result = dict(result)
        result["stdout"] = _format_human(result)
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
