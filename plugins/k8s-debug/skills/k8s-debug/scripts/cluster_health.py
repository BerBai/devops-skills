#!/usr/bin/env python3
"""cluster_health.py - cluster-wide health snapshot (v1.0)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result  # type: ignore[import-not-found]  # noqa: E402, I001
from lib.kubectl import KubectlError, KubectlResult, run_kubectl  # type: ignore[import-not-found]  # noqa: E402
from _version import add_version_flag, handle_version

_TEN_MB = 10 * 1024 * 1024
_TRUNCATE_MARKER = "\n... <truncated 10MB cap>\n"
_CLUSTER_UNREACHABLE_EXIT = 100
_TOP_FINDINGS = 5
_MAX_SINCE_MIN = 1440
_MANY_WARNING_EVENTS = 3

_DOMAIN_ORDER = {
    "nodes": 0,
    "control_plane": 1,
    "kube_system": 2,
    "events": 3,
    "versions": 4,
}
_SEVERITY_ORDER = {"crit": 0, "warn": 1, "ok": 2}
_PRESSURE_CONDITIONS = {
    "DiskPressure",
    "MemoryPressure",
    "PIDPressure",
    "NetworkUnavailable",
}
_CONTROL_PLANE_COMPONENTS = {
    "kube-apiserver",
    "kube-controller-manager",
    "kube-scheduler",
    "etcd",
}
_CRIT_CONTAINER_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "OOMKilled",
}
_CRIT_EVENT_REASONS = {
    "NodeNotReady",
    "Failed",
    "FailedMount",
    "FailedScheduling",
    "BackOff",
    "Unhealthy",
    "Killing",
    "OOMKilling",
}
_API_UNREACHABLE_MARKERS = (
    "Unable to connect to the server",
    "The connection to the server",
    "dial tcp",
    "no such host",
)
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cluster_health.py")
    parser.add_argument("host", help="ssh-core alias, or 'local'")
    parser.add_argument("--since", type=int, default=30, help="kube-system Events lookback minutes")
    parser.add_argument("--json", action="store_true")
    add_version_flag(parser)
    return parser


def _truncate_10mb(value: str) -> str:
    if len(value) > _TEN_MB:
        return value[:_TEN_MB] + _TRUNCATE_MARKER
    return value


def _loads_json(stdout: str) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _dict_field(item: dict[str, Any], key: str) -> dict[str, Any]:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _metadata_name(item: dict[str, Any]) -> str:
    return str(_dict_field(item, "metadata").get("name") or "")


def _nested(obj: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else None


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


def _condition(nodes_status: dict[str, Any], condition_type: str) -> dict[str, Any] | None:
    conditions = nodes_status.get("conditions")
    if not isinstance(conditions, list):
        return None
    for condition in conditions:
        if isinstance(condition, dict) and condition.get("type") == condition_type:
            return condition
    return None


def _score_nodes(nodes_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    for node in _items(nodes_payload):
        name = _metadata_name(node)
        spec = _dict_field(node, "spec")
        status = _dict_field(node, "status")
        ready = _condition(status, "Ready")
        if not ready or ready.get("status") != "True":
            message = str((ready or {}).get("message") or "node Ready condition is not True")
            findings.append(_finding("crit", "nodes", "Node", name, "NotReady", message))
        for condition_type in sorted(_PRESSURE_CONDITIONS):
            condition = _condition(status, condition_type)
            if condition and condition.get("status") == "True":
                message = str(condition.get("message") or f"{condition_type}=True")
                findings.append(_finding("warn", "nodes", "Node", name, condition_type, message))
        if spec.get("unschedulable") is True:
            findings.append(_finding("warn", "nodes", "Node", name, "SchedulingDisabled", "node is cordoned"))
    return _score_from_findings(findings), findings


def _container_status_findings(
    pod: dict[str, Any],
    *,
    domain: str,
    kind: str = "Pod",
) -> list[dict[str, Any]]:
    name = _metadata_name(pod)
    status = _dict_field(pod, "status")
    phase = str(status.get("phase") or "")
    findings: list[dict[str, Any]] = []
    if phase == "Failed":
        findings.append(_finding("crit", domain, kind, name, "Failed", "pod phase is Failed"))
    elif phase == "Pending":
        findings.append(_finding("warn", domain, kind, name, "Pending", "pod phase is Pending"))

    containers = status.get("containerStatuses")
    if not isinstance(containers, list):
        return findings
    for container in containers:
        if not isinstance(container, dict):
            continue
        cname = str(container.get("name") or "")
        waiting = _nested(container, "state", "waiting")
        terminated = _nested(container, "lastState", "terminated")
        reason = str((waiting or {}).get("reason") or (terminated or {}).get("reason") or "")
        if reason in _CRIT_CONTAINER_REASONS:
            findings.append(
                _finding("crit", domain, kind, name, reason, f"container {cname} is {reason}")
            )
        if container.get("ready") is False:
            findings.append(_finding("warn", domain, kind, name, "NotReady", f"container {cname} is not ready"))
        if (container.get("restartCount") or 0) > 0:
            findings.append(
                _finding(
                    "warn",
                    domain,
                    kind,
                    name,
                    "Restarting",
                    f"container {cname} restartCount={container.get('restartCount')}",
                )
            )
    return findings


def _control_plane_pods(kube_system_payload: dict[str, Any]) -> list[dict[str, Any]]:
    pods: list[dict[str, Any]] = []
    for pod in _items(kube_system_payload):
        name = _metadata_name(pod)
        metadata = _dict_field(pod, "metadata")
        labels = metadata.get("labels")
        label_values = set(labels.values()) if isinstance(labels, dict) else set()
        if label_values & _CONTROL_PLANE_COMPONENTS or any(name.startswith(c) for c in _CONTROL_PLANE_COMPONENTS):
            pods.append(pod)
    return pods


def _raw_ok(value: str | None) -> bool:
    return value is not None and "ok" in value.strip().lower()


def _score_control_plane(
    kube_system_payload: dict[str, Any],
    raw: dict[str, str],
    errors: dict[str, KubectlError],
) -> tuple[str, list[dict[str, Any]]]:
    pods = _control_plane_pods(kube_system_payload)
    if pods:
        findings: list[dict[str, Any]] = []
        for pod in pods:
            findings.extend(_container_status_findings(pod, domain="control_plane"))
        return _score_from_findings(findings), findings

    healthz = raw.get("healthz")
    readyz = raw.get("readyz")
    findings = []
    collected = [body for body in (healthz, readyz) if body is not None]
    if not collected:
        detail = "; ".join(f"{k}: {v.stderr}" for k, v in sorted(errors.items()) if k in {"healthz", "readyz"})
        findings.append(
            _finding(
                "warn",
                "control_plane",
                "ControlPlane",
                "managed",
                "healthz_unavailable",
                detail or "control-plane pods and raw health endpoints are unavailable",
                hint="references/common_issues.md#managed-control-plane",
            )
        )
        return "warn", findings

    for endpoint, body in (("healthz", healthz), ("readyz", readyz)):
        if body is not None and not _raw_ok(body):
            findings.append(
                _finding("crit", "control_plane", "ControlPlane", endpoint, "NotReady", body.strip()[:500])
            )
    if errors and any(k in errors for k in ("healthz", "readyz")) and not findings:
        findings.append(
            _finding("warn", "control_plane", "ControlPlane", "managed", "healthz_unavailable", "one raw health endpoint is unavailable")
        )
    return _score_from_findings(findings), findings


def _score_kube_system(kube_system_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    for pod in _items(kube_system_payload):
        findings.extend(_container_status_findings(pod, domain="kube_system"))
    return _score_from_findings(findings), findings


def _score_events(events_payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for event in _items(events_payload):
        if event.get("type") != "Warning":
            continue
        warnings.append(event)
        reason = str(event.get("reason") or "")
        if reason in _CRIT_EVENT_REASONS:
            findings.append(
                _finding("crit", "events", "Event", _metadata_name(event), reason, str(event.get("message") or ""))
            )
    if len(warnings) >= _MANY_WARNING_EVENTS and not any(f["severity"] == "crit" for f in findings):
        findings.append(_finding("warn", "events", "Event", "*", "ManyWarnings", f"{len(warnings)} Warning events"))
    return _score_from_findings(findings), findings


def _minor(version: str) -> int | None:
    match = _VERSION_RE.search(version)
    if not match:
        return None
    return int(match.group(2))


def _score_versions(
    nodes_payload: dict[str, Any],
    version_stdout: str | None,
    version_error: KubectlError | None,
) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    if version_error is not None:
        findings.append(
            _finding("warn", "versions", "Version", "kubectl", "versions_partial", version_error.stderr)
        )
    elif version_stdout is None:
        findings.append(_finding("warn", "versions", "Version", "kubectl", "versions_partial", "version output missing"))

    node_versions = [
        str(_dict_field(_dict_field(node, "status"), "nodeInfo").get("kubeletVersion") or "")
        for node in _items(nodes_payload)
    ]
    minors = [minor for minor in (_minor(v) for v in node_versions if v) if minor is not None]
    if minors and max(minors) - min(minors) > 1:
        findings.append(
            _finding(
                "warn",
                "versions",
                "Version",
                "kubelet",
                "MinorSkew",
                f"kubelet minor versions differ by {max(minors) - min(minors)}",
            )
        )
    return _score_from_findings(findings), findings


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


def _looks_like_api_unreachable(stderr: str) -> bool:
    return any(marker in stderr for marker in _API_UNREACHABLE_MARKERS)


def _run_optional(
    host: str,
    namespace: str | None,
    args: list[str],
    *,
    raw_key: str,
    use_json: bool = False,
    timeout: int = 30,
) -> tuple[str, KubectlResult | None, KubectlError | None]:
    try:
        return raw_key, run_kubectl(host, namespace, args, use_json=use_json, timeout=timeout), None
    except KubectlError as exc:
        return raw_key, None, exc


def _soft_failure_findings(errors: dict[str, KubectlError]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    domain_map = {
        "kube_system_pods": "kube_system",
        "kube_system_events": "events",
    }
    for raw_key, domain in domain_map.items():
        if raw_key in errors:
            findings.append(_finding("warn", domain, "Collection", raw_key, "PartialCollection", errors[raw_key].stderr))
    return findings


def _check(args: argparse.Namespace) -> dict[str, Any]:  # noqa: PLR0915
    since_min = max(1, int(args.since))
    warnings: list[str] = []
    if since_min > _MAX_SINCE_MIN:
        warnings.append("--since over 1440 minutes; event volume may be large")

    try:
        nodes_result = run_kubectl(args.host, None, ["get", "nodes"], use_json=True, timeout=30)
    except KubectlError as exc:
        if exc.failure_class == "host_unreachable" and _looks_like_api_unreachable(exc.stderr):
            raise KubectlError("cluster_unreachable", _CLUSTER_UNREACHABLE_EXIT, exc.stderr) from None
        raise

    raw: dict[str, str] = {"nodes": _truncate_10mb(nodes_result.stdout or "")}
    errors: dict[str, KubectlError] = {}

    for raw_key, result, error in (
        _run_optional(args.host, "kube-system", ["get", "pods"], raw_key="kube_system_pods", use_json=True),
        _run_optional(args.host, "kube-system", ["get", "events"], raw_key="kube_system_events", use_json=True),
        _run_optional(args.host, None, ["get", "--raw=/healthz"], raw_key="healthz", timeout=10),
        _run_optional(args.host, None, ["get", "--raw=/readyz"], raw_key="readyz", timeout=10),
        _run_optional(args.host, None, ["version", "--short"], raw_key="version", timeout=10),
    ):
        if result is not None:
            raw[raw_key] = _truncate_10mb(result.stdout or "")
        elif error is not None:
            errors[raw_key] = error

    nodes_payload = _loads_json(raw.get("nodes", ""))
    kube_system_payload = _loads_json(raw.get("kube_system_pods", ""))
    events_payload = _loads_json(raw.get("kube_system_events", ""))

    summary: dict[str, str] = {}
    all_findings: list[dict[str, Any]] = []
    summary["nodes"], node_findings = _score_nodes(nodes_payload)
    summary["control_plane"], control_findings = _score_control_plane(kube_system_payload, raw, errors)
    summary["kube_system"], kube_system_findings = _score_kube_system(kube_system_payload)
    summary["events"], event_findings = _score_events(events_payload)
    summary["versions"], version_findings = _score_versions(
        nodes_payload,
        raw.get("version"),
        errors.get("version"),
    )
    all_findings.extend(
        node_findings
        + control_findings
        + kube_system_findings
        + event_findings
        + version_findings
        + _soft_failure_findings(errors)
    )

    if "kube_system_pods" in errors:
        summary["kube_system"] = "warn"
        if not control_findings:
            summary["control_plane"] = "warn"
    if "kube_system_events" in errors:
        summary["events"] = "warn"
    if "version" in errors:
        summary["versions"] = "warn"

    return json_result(
        success=True,
        exit_code=0,
        data={
            "host": args.host,
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
    lines = [f"Cluster health on {data.get('host', '')}"]
    for domain in ("nodes", "control_plane", "kube_system", "events", "versions"):
        lines.append(f"{domain:<15} {summary.get(domain, 'unknown')}")
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
    early = handle_version("cluster_health.py", argv)
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
                "since_min": max(1, int(args.since)),
                "failure_class": exc.failure_class,
            },
        )
    except Exception as exc:  # pragma: no cover - last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"cluster_health unexpected error: {type(exc).__name__}: {exc}",
            data={"host": args.host, "failure_class": None},
        )

    if not args.json and result.get("success"):
        result = dict(result)
        result["stdout"] = _format_human(result)
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
