#!/usr/bin/env python3
"""check_namespace.py - namespace health snapshot (v1.0)."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result  # type: ignore[import-not-found]  # noqa: E402, I001
from lib.kubectl import KubectlError, KubectlResult, run_kubectl  # type: ignore[import-not-found]  # noqa: E402
from _version import add_version_flag, handle_version

_TEN_MB = 10 * 1024 * 1024
_TRUNCATE_MARKER = "\n... <truncated 10MB cap>\n"
_PARTIAL_COLLECTION_EXIT = 88
_MAX_SOFT_DOMAIN_FAILURES = 3
_TOP_FINDINGS = 3
_MAX_SINCE_MIN = 1440
_MANY_PENDING_FAILED_PODS = 3
_MANY_WARNING_EVENTS = 3

_DOMAIN_ORDER = {
    "pods": 0,
    "events": 1,
    "deployments": 2,
    "services": 3,
    "pvcs": 4,
}
_SEVERITY_ORDER = {"crit": 0, "warn": 1, "ok": 2}
_CRIT_EVENT_REASONS = {
    "Failed",
    "FailedMount",
    "FailedScheduling",
    "BackOff",
    "Unhealthy",
    "Killing",
    "OOMKilling",
}


@dataclass(frozen=True)
class DomainResult:
    raw_key: str
    result: KubectlResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_namespace.py")
    parser.add_argument("host", help="ssh-core alias, or 'local'")
    parser.add_argument("namespace")
    parser.add_argument("--since", type=int, default=60, help="Look this many minutes back in Events")
    parser.add_argument("--json", action="store_true")
    add_version_flag(parser)
    return parser


def _truncate_10mb(value: str) -> str:
    if len(value) > _TEN_MB:
        return value[:_TEN_MB] + _TRUNCATE_MARKER
    return value


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _loads_json(stdout: str) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _metadata_name(item: dict[str, Any]) -> str:
    metadata = _dict_field(item, "metadata")
    return str(metadata.get("name") or "")


def _dict_field(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _finding(
    severity: str,
    domain: str,
    kind: str,
    name: str,
    reason: str,
    message: str,
    *,
    hint: str = "",
) -> dict[str, Any]:
    out = {
        "severity": severity,
        "domain": domain,
        "kind": kind,
        "name": name,
        "reason": reason,
        "message": message,
    }
    if hint:
        out["hint"] = hint
    return out


def _score_from_findings(findings: list[dict[str, Any]]) -> str:
    if any(f["severity"] == "crit" for f in findings):
        return "crit"
    if any(f["severity"] == "warn" for f in findings):
        return "warn"
    return "ok"


def _score_pods(pods_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    pending_or_failed = 0
    for pod in _items(pods_payload):
        name = _metadata_name(pod)
        status = _dict_field(pod, "status")
        phase = str(status.get("phase") or "")
        if phase in {"Pending", "Failed"}:
            pending_or_failed += 1
        if phase == "Failed":
            findings.append(_finding("crit", "pods", "Pod", name, "Failed", "pod phase is Failed"))
        elif phase == "Pending":
            findings.append(_finding("warn", "pods", "Pod", name, "Pending", "pod phase is Pending"))

        containers = status.get("containerStatuses")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            cname = str(container.get("name") or "")
            waiting = _nested(container, "state", "waiting")
            terminated = _nested(container, "lastState", "terminated")
            reason = str((waiting or {}).get("reason") or (terminated or {}).get("reason") or "")
            if reason in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"}:
                findings.append(
                    _finding(
                        "crit",
                        "pods",
                        "Pod",
                        name,
                        reason,
                        f"container {cname} is {reason}",
                        hint="references/common_issues.md",
                    )
                )
            if container.get("ready") is False:
                findings.append(
                    _finding("warn", "pods", "Pod", name, "NotReady", f"container {cname} is not ready")
                )
            if (container.get("restartCount") or 0) > 0:
                findings.append(
                    _finding(
                        "warn",
                        "pods",
                        "Pod",
                        name,
                        "Restarting",
                        f"container {cname} restartCount={container.get('restartCount')}",
                    )
                )
    if pending_or_failed >= _MANY_PENDING_FAILED_PODS:
        findings.append(
            _finding("crit", "pods", "Pod", "*", "ManyPendingOrFailed", f"{pending_or_failed} pods pending/failed")
        )
    return _score_from_findings(findings), findings


def _score_events(events_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for event in _items(events_payload):
        if event.get("type") != "Warning":
            continue
        warnings.append(event)
        reason = str(event.get("reason") or "")
        severity = "crit" if reason in _CRIT_EVENT_REASONS else "warn"
        if severity == "crit":
            findings.append(
                _finding(
                    "crit",
                    "events",
                    "Event",
                    _metadata_name(event),
                    reason,
                    str(event.get("message") or ""),
                )
            )
    if len(warnings) >= _MANY_WARNING_EVENTS and not any(f["severity"] == "crit" for f in findings):
        findings.append(
            _finding("warn", "events", "Event", "*", "ManyWarnings", f"{len(warnings)} Warning events")
        )
    return _score_from_findings(findings), findings


def _score_deployments(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    for dep in _items(payload):
        name = _metadata_name(dep)
        spec = _dict_field(dep, "spec")
        status = _dict_field(dep, "status")
        desired = int(spec.get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        available = int(status.get("availableReplicas") or 0)
        observed = int(status.get("observedGeneration") or 0)
        generation = int(_dict_field(dep, "metadata").get("generation") or 0)
        if desired > ready and observed >= generation:
            findings.append(
                _finding("crit", "deployments", "Deployment", name, "ReplicasNotReady", f"ready {ready}/{desired}")
            )
        elif desired > available:
            findings.append(
                _finding("warn", "deployments", "Deployment", name, "ReplicasUnavailable", f"available {available}/{desired}")
            )
    return _score_from_findings(findings), findings


def _score_services(
    services_payload: dict[str, Any],
    endpoints_payload: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    endpoint_names: set[str] = set()
    for ep in _items(endpoints_payload):
        subsets = ep.get("subsets")
        if not isinstance(subsets, list):
            continue
        has_address = any(isinstance(s, dict) and s.get("addresses") for s in subsets)
        if has_address:
            endpoint_names.add(_metadata_name(ep))

    findings: list[dict[str, Any]] = []
    for svc in _items(services_payload):
        name = _metadata_name(svc)
        spec = _dict_field(svc, "spec")
        if spec.get("type") == "ExternalName":
            continue
        if name not in endpoint_names:
            findings.append(
                _finding("crit", "services", "Service", name, "NoEndpoints", "service has no ready endpoints")
            )
    return _score_from_findings(findings), findings


def _score_pvcs(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    for pvc in _items(payload):
        status = _dict_field(pvc, "status")
        phase = str(status.get("phase") or "")
        if phase in {"Pending", "Lost"}:
            findings.append(
                _finding("crit", "pvcs", "PersistentVolumeClaim", _metadata_name(pvc), phase, f"PVC phase is {phase}")
            )
    return _score_from_findings(findings), findings


def _nested(obj: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else None


def _top_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(str(f.get("severity")), 9),
            _DOMAIN_ORDER.get(str(f.get("domain")), 9),
            str(f.get("kind") or ""),
            str(f.get("name") or ""),
            str(f.get("reason") or ""),
        ),
    )[:_TOP_FINDINGS]


def _collect_domains(args: argparse.Namespace) -> tuple[dict[str, DomainResult], dict[str, KubectlError]]:
    calls: dict[str, tuple[str, list[str], bool, str]] = {
        "pods": ("pods", ["get", "pods"], True, "pods"),
        "events": ("events", ["get", "events"], True, "events"),
        "deployments": ("deployments", ["get", "deployments"], True, "deployments"),
        "services": ("services", ["get", "services"], True, "services"),
        "endpoints": ("endpoints", ["get", "endpoints"], True, "endpoints"),
        "pvcs": ("pvcs", ["get", "pvc"], True, "pvcs"),
    }
    results: dict[str, DomainResult] = {}
    errors: dict[str, KubectlError] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = {
            pool.submit(run_kubectl, args.host, args.namespace, k_args, use_json=use_json, timeout=30): domain
            for domain, (_raw_key, k_args, use_json, _score_key) in calls.items()
        }
        for future in concurrent.futures.as_completed(futures):
            domain = futures[future]
            raw_key = calls[domain][0]
            try:
                results[domain] = DomainResult(raw_key=raw_key, result=future.result())
            except KubectlError as exc:
                errors[domain] = exc
    return results, errors


def _check(args: argparse.Namespace) -> dict[str, Any]:
    since_min = max(1, int(args.since))
    warnings: list[str] = []
    if since_min > _MAX_SINCE_MIN:
        warnings.append("--since over 1440 minutes; event volume may be large")

    run_kubectl(args.host, None, ["get", "ns", args.namespace], timeout=30)
    results, errors = _collect_domains(args)
    if len(errors) > _MAX_SOFT_DOMAIN_FAILURES:
        return json_result(
            success=False,
            exit_code=_PARTIAL_COLLECTION_EXIT,
            stderr=f"partial_collection: {len(errors)} domains failed",
            data={
                "host": args.host,
                "namespace": args.namespace,
                "since_min": since_min,
                "failure_class": "partial_collection",
                "partial_failures": _partial_failures(errors),
            },
        )

    payloads = {name: _loads_json(domain.result.stdout) for name, domain in results.items()}
    summary: dict[str, str] = {}
    all_findings: list[dict[str, Any]] = []

    summary["pods"], pod_findings = _score_pods(payloads.get("pods", {}))
    summary["events"], event_findings = _score_events(payloads.get("events", {}))
    summary["deployments"], deployment_findings = _score_deployments(payloads.get("deployments", {}))
    summary["services"], service_findings = _score_services(
        payloads.get("services", {}),
        payloads.get("endpoints", {}),
    )
    summary["pvcs"], pvc_findings = _score_pvcs(payloads.get("pvcs", {}))
    all_findings.extend(pod_findings + event_findings + deployment_findings + service_findings + pvc_findings)

    for domain in ("pods", "events", "deployments", "services", "pvcs"):
        if domain in errors:
            summary[domain] = "warn"
            all_findings.append(
                _finding("warn", domain, "Collection", domain, "PartialCollection", errors[domain].stderr)
            )

    raw = {domain.raw_key: _truncate_10mb(domain.result.stdout or "") for domain in results.values()}
    return json_result(
        success=True,
        exit_code=0,
        data={
            "host": args.host,
            "namespace": args.namespace,
            "since_min": since_min,
            "summary": summary,
            "findings": _top_findings(all_findings),
            "all_findings_count": len(all_findings),
            "partial_failures": _partial_failures(errors),
            "raw": raw,
            "warnings": warnings,
            "failure_class": None,
        },
    )


def _partial_failures(errors: dict[str, KubectlError]) -> list[dict[str, Any]]:
    return [
        {
            "domain": domain,
            "failure_class": error.failure_class,
            "exit_code": error.exit_code,
            "stderr": error.stderr,
        }
        for domain, error in sorted(errors.items())
    ]


def _format_human(result: dict[str, Any]) -> str:
    data = result.get("data", {})
    summary = data.get("summary", {})
    lines = [f"Namespace {data.get('namespace', '')} on {data.get('host', '')}"]
    for domain in ("pods", "events", "deployments", "services", "pvcs"):
        lines.append(f"{domain:<12} {summary.get(domain, 'unknown')}")
    findings = data.get("findings") or []
    if findings:
        lines.append("Findings:")
        for finding in findings:
            lines.append(
                f"- {finding.get('severity')} {finding.get('domain')}/{finding.get('kind')} "
                f"{finding.get('name')}: {finding.get('reason')}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    early = handle_version("check_namespace.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    try:
        result = _check(args)
    except KubectlError as exc:
        result = json_result(
            success=False,
            exit_code=exc.exit_code,
            stderr=exc.stderr,
            data={
                "host": args.host,
                "namespace": args.namespace,
                "since_min": max(1, int(args.since)),
                "failure_class": exc.failure_class,
            },
        )
    except Exception as exc:  # pragma: no cover - last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"check_namespace unexpected error: {type(exc).__name__}: {exc}",
            data={"host": args.host, "namespace": args.namespace, "failure_class": None},
        )

    if not args.json and result.get("success"):
        result = dict(result)
        result["stdout"] = _format_human(result)
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
