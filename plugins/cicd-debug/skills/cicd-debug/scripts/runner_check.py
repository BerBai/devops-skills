#!/usr/bin/env python3
# ruff: noqa: E402
"""runner_check.py — CI runner pool health (v1.0).

v1.0 surface:
    runner_check.py <provider>
        [--org <name> | --enterprise <slug> | --repo <owner/name>]
        [--group <path> | --project <g/p> | --instance]
        [--pool-cap N]
        [--queue-cap N]
        [--gh-bin <path>] [--glab-bin <path>]
        [--timeout N]
        [--json]

For each runner:
    id, name, os, status, busy, ephemeral, labels,
    last_heartbeat, running_job, machine_info

Aggregate:
    online_count / offline_count / busy_count / total_count
    label_coverage
    queue_depth / queue_p95_age_sec / queue_max_age_sec
    summary ok | warn | crit

See: .trellis/tasks/05-26-cicd-debug-runner-check/prd.md
See: lib_cicd.py (provider abstraction)
See: references/runner_debug.md (runner health patterns)
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

_LIB_SPEC = importlib.util.spec_from_file_location(
    "cicd_debug_lib", SCRIPTS_DIR / "lib" / "__init__.py"
)
if _LIB_SPEC is None or _LIB_SPEC.loader is None:
    raise RuntimeError("could not load cicd-debug local lib")
_LIB = importlib.util.module_from_spec(_LIB_SPEC)
_LIB_SPEC.loader.exec_module(_LIB)
emit = _LIB.emit
json_result = _LIB.json_result

from lib_cicd import (  # type: ignore[import-not-found]
    ApiUnexpectedError,
    AuthError,
    BadArgsError,
    CliTimeoutError,
    NetworkError,
    NotFoundError,
    PermissionError,
    ProviderUnavailable,
    QueuedRun,
    RunnerData,
    resolve_provider,
)

DEFAULT_POOL_CAP = 5000
DEFAULT_QUEUE_CAP = 200
DEFAULT_TIMEOUT_SEC = 60
QUEUE_DEPTH_WARN_THRESHOLD = 10
QUEUE_P95_WARN_SEC = 600
OFFLINE_WARN_RATIO = 0.30

FAILURE_CLASS_TO_EXIT = {
    None: 0,
    "tool_missing": 160,
    "provider_unavailable": 161,
    "auth": 162,
    "not_found": 163,
    "permission": 164,
    "network": 165,
    "bad_args": 166,
    "api_unexpected": 167,
    "pagination_overflow": 168,
    "timeout": 124,
    "internal": 1,
}

HINTS = {
    "all_offline": "see references/runner_debug.md#all-offline",
    "queue_starving": "see references/runner_debug.md#queue-starving",
    "runner_offline_majority": "see references/runner_debug.md#runner-offline-majority",
    "queue_pressure": "see references/runner_debug.md#queue-pressure",
    "last_heartbeat_unavailable": "see references/runner_debug.md#last-heartbeat-unavailable",
    "running_job_unavailable_at_scope": "see references/runner_debug.md#running-job-unavailable-at-scope",
    "pool_truncated": "see references/runner_debug.md#pool-truncated",
    "ephemeral_runner_offline": "see references/runner_debug.md#ephemeral-runner-offline",
}

GH_HEARTBEAT_URL = (
    "https://docs.github.com/en/rest/actions/self-hosted-runners"
    "?apiVersion=2022-11-28"
)


@dataclass(frozen=True)
class Scope:
    kind: str
    value: str


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="runner_check.py")
    p.add_argument("provider", choices=["gh", "glab"], help="CI provider: gh or glab")
    p.add_argument("--org", default=None, help="GitHub org runner scope")
    p.add_argument("--enterprise", default=None, help="GitHub enterprise runner scope")
    p.add_argument("--repo", default=None, help="GitHub repo runner scope, owner/name")
    p.add_argument("--group", default=None, help="GitLab group runner scope")
    p.add_argument("--project", default=None, help="GitLab project runner scope, group/name")
    p.add_argument("--instance", action="store_true", help="GitLab instance runner scope")
    p.add_argument("--pool-cap", type=int, default=DEFAULT_POOL_CAP)
    p.add_argument("--queue-cap", type=int, default=DEFAULT_QUEUE_CAP)
    p.add_argument("--gh-bin", default=None, help="Path to gh binary")
    p.add_argument("--glab-bin", default=None, help="Path to glab binary")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    p.add_argument("--json", action="store_true")
    return p


def validate_scope(args: argparse.Namespace) -> Scope:
    chosen = [
        ("org", args.org),
        ("enterprise", args.enterprise),
        ("repo", args.repo),
        ("group", args.group),
        ("project", args.project),
        ("instance", "*" if args.instance else None),
    ]
    selected = [(kind, value) for kind, value in chosen if value]
    if len(selected) != 1:
        raise BadArgsError("exactly one scope flag is required")
    if args.pool_cap < 1:
        raise BadArgsError("--pool-cap must be >= 1")
    if args.queue_cap < 0:
        raise BadArgsError("--queue-cap must be >= 0")
    if args.timeout < 1:
        raise BadArgsError("--timeout must be >= 1")
    kind, value = selected[0]
    if args.provider == "gh" and kind not in {"org", "enterprise", "repo"}:
        raise BadArgsError("gh supports --org, --enterprise, or --repo")
    if args.provider == "glab" and kind not in {"group", "project", "instance"}:
        raise BadArgsError("glab supports --group, --project, or --instance")
    return Scope(kind, str(value))


def _parse_timestamp(value: str, now: datetime) -> float:
    del now
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiUnexpectedError(f"invalid queued timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _percentile95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(0.95 * (len(ordered) - 1))
    return ordered[index]


def compute_aggregate(
    runners: list[RunnerData],
    queued: list[QueuedRun],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    clock = now or datetime.now(timezone.utc)
    total = len(runners)
    online = sum(1 for runner in runners if runner.status == "online")
    offline = sum(1 for runner in runners if runner.status != "online")
    busy = sum(1 for runner in runners if runner.busy)
    label_coverage: dict[str, int] = {}
    for runner in runners:
        for label in runner.labels:
            name = label.get("name")
            if name:
                label_coverage[name] = label_coverage.get(name, 0) + 1

    now_ts = clock.timestamp()
    ages = [max(0.0, now_ts - _parse_timestamp(run.created_at, clock)) for run in queued]
    return {
        "online_count": online,
        "offline_count": offline,
        "busy_count": busy,
        "total_count": total,
        "label_coverage": dict(sorted(label_coverage.items())),
        "queue_depth": len(queued),
        "queue_p95_age_sec": _percentile95(ages),
        "queue_max_age_sec": max(ages) if ages else None,
    }


def compute_summary(aggregate: dict[str, Any]) -> str:
    total = int(aggregate["total_count"])
    online = int(aggregate["online_count"])
    offline = int(aggregate["offline_count"])
    queue_depth = int(aggregate["queue_depth"])
    p95 = aggregate["queue_p95_age_sec"]
    if total > 0 and online == 0:
        return "crit"
    if queue_depth >= 1 and online == 0:
        return "crit"
    if queue_depth >= QUEUE_DEPTH_WARN_THRESHOLD:
        return "warn"
    if isinstance(p95, (int, float)) and p95 >= QUEUE_P95_WARN_SEC:
        return "warn"
    if total > 0 and offline / total >= OFFLINE_WARN_RATIO:
        return "warn"
    return "ok"


def build_findings(
    provider: str,
    scope: Scope,
    runners: list[RunnerData],
    aggregate: dict[str, Any],
    *,
    pool_truncated: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    total = int(aggregate["total_count"])
    online = int(aggregate["online_count"])
    offline = int(aggregate["offline_count"])
    queue_depth = int(aggregate["queue_depth"])
    p95 = aggregate["queue_p95_age_sec"]

    if total > 0 and online == 0:
        findings.append(
            {
                "severity": "crit",
                "kind": "all_offline",
                "total_count": total,
                "hint": HINTS["all_offline"],
            }
        )
    if queue_depth >= 1 and online == 0:
        findings.append(
            {
                "severity": "crit",
                "kind": "queue_starving",
                "queue_depth": queue_depth,
                "missing_labels": [],
                "hint": HINTS["queue_starving"],
            }
        )
    if total > 0 and offline / total >= OFFLINE_WARN_RATIO:
        findings.append(
            {
                "severity": "warn",
                "kind": "runner_offline_majority",
                "offline_pct": round(offline / total, 3),
                "hint": HINTS["runner_offline_majority"],
            }
        )
    if queue_depth >= QUEUE_DEPTH_WARN_THRESHOLD or (
        isinstance(p95, (int, float)) and p95 >= QUEUE_P95_WARN_SEC
    ):
        findings.append(
            {
                "severity": "warn",
                "kind": "queue_pressure",
                "queue_depth": queue_depth,
                "p95_sec": p95,
                "hint": HINTS["queue_pressure"],
            }
        )
    if provider == "gh":
        findings.append(
            {
                "severity": "info",
                "kind": "last_heartbeat_unavailable",
                "api_limitation_url": GH_HEARTBEAT_URL,
                "hint": HINTS["last_heartbeat_unavailable"],
            }
        )
    if scope.kind not in {"repo", "project"}:
        findings.append(
            {
                "severity": "info",
                "kind": "running_job_unavailable_at_scope",
                "scope_kind": scope.kind,
                "hint": HINTS["running_job_unavailable_at_scope"],
            }
        )
    if pool_truncated:
        findings.append(
            {
                "severity": "warn",
                "kind": "pool_truncated",
                "returned": total,
                "hint": HINTS["pool_truncated"],
            }
        )
    for runner in runners:
        if runner.ephemeral and runner.status != "online":
            findings.append(
                {
                    "severity": "info",
                    "kind": "ephemeral_runner_offline",
                    "runner_name": runner.name,
                    "hint": HINTS["ephemeral_runner_offline"],
                }
            )
    return findings


def _runner_payload(runner: RunnerData) -> dict[str, Any]:
    payload = asdict(runner)
    if payload["machine_info"] is None:
        payload["machine_info"] = {"os": runner.os} if runner.os else {}
    return payload


def _format_human(data: dict[str, Any]) -> str:
    aggregate = data["aggregate"]
    return (
        f"runner_check {data['provider']} {data['scope']['kind']}:{data['scope']['value']} "
        f"summary={data['summary']} runners={aggregate['total_count']} "
        f"online={aggregate['online_count']} queue={aggregate['queue_depth']} "
        f"findings={len(data['findings'])}\n"
    )


def _emit_data(
    data: dict[str, Any],
    exit_code: int,
    stderr: str,
    json_mode: bool,
    stdout: str = "",
) -> int:
    return emit(
        json_result(
            success=exit_code == 0,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            data=data,
        ),
        json_mode,
    )


def build_success_data(
    args: argparse.Namespace,
    scope: Scope,
    runners: list[RunnerData],
    queued: list[QueuedRun],
    failure_class: str | None,
    started_at: float,
) -> dict[str, Any]:
    aggregate = compute_aggregate(runners, queued)
    findings = build_findings(
        args.provider,
        scope,
        runners,
        aggregate,
        pool_truncated=failure_class == "pagination_overflow",
    )
    return {
        "provider": args.provider,
        "scope": asdict(scope),
        "runners": [_runner_payload(runner) for runner in runners],
        "aggregate": aggregate,
        "summary": compute_summary(aggregate),
        "findings": findings,
        "failure_class": failure_class,
        "elapsed_s": time.time() - started_at,
    }


def _failure_data(args: argparse.Namespace, started_at: float, failure_class: str) -> dict[str, Any]:
    return {
        "provider": getattr(args, "provider", None),
        "scope": None,
        "runners": [],
        "aggregate": {
            "online_count": 0,
            "offline_count": 0,
            "busy_count": 0,
            "total_count": 0,
            "label_coverage": {},
            "queue_depth": 0,
            "queue_p95_age_sec": None,
            "queue_max_age_sec": None,
        },
        "summary": "crit",
        "findings": [],
        "failure_class": failure_class,
        "elapsed_s": time.time() - started_at,
    }


def _map_exception(exc: Exception, provider: str | None) -> tuple[str, str]:
    if isinstance(exc, ProviderUnavailable):
        if provider == "glab" or "glab" in str(exc).lower():
            return "provider_unavailable", str(exc)
        return "tool_missing", str(exc)
    mapped: tuple[tuple[type[Exception] | tuple[type[Exception], ...], str], ...] = (
        (AuthError, "auth"),
        (NotFoundError, "not_found"),
        (PermissionError, "permission"),
        (NetworkError, "network"),
        (BadArgsError, "bad_args"),
        (ApiUnexpectedError, "api_unexpected"),
        ((CliTimeoutError, TimeoutError), "timeout"),
    )
    for exc_type, failure_class in mapped:
        if isinstance(exc, exc_type):
            return failure_class, str(exc)
    return "internal", f"Internal error: {exc}"


def main(argv: list[str] | None = None) -> int:
    started_at = time.time()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        scope = validate_scope(args)
        provider = resolve_provider(args.provider, args.gh_bin, args.glab_bin)
        runners = provider.list_runners(scope, limit=args.pool_cap)
        queued = provider.list_queued_runs(scope, limit=args.queue_cap)
        failure_class = "pagination_overflow" if len(runners) >= args.pool_cap else None
        data = build_success_data(args, scope, runners, queued, failure_class, started_at)
        exit_code = FAILURE_CLASS_TO_EXIT[failure_class]
        stdout = _format_human(data)
        if exit_code == 0:
            return _emit_data(data, 0, "", args.json, stdout)
        return _emit_data(data, exit_code, f"{failure_class}\n", args.json, stdout)
    except Exception as exc:
        failure_class, stderr = _map_exception(exc, getattr(args, "provider", None))
        data = _failure_data(args, started_at, failure_class)
        return _emit_data(data, FAILURE_CLASS_TO_EXIT[failure_class], stderr, args.json)


if __name__ == "__main__":
    sys.exit(main())
