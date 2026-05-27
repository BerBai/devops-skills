#!/usr/bin/env python3
"""diagnose_pod.py — drill into a single Pod (v1.0).

v1.0 surface:
    diagnose_pod.py <host> <namespace> <pod> [--container NAME]
                    [--json] [--logs-tail 200] [--previous]

Runs in parallel (ThreadPoolExecutor):
    kubectl describe pod
    kubectl logs (current + --previous, the latter auto on CrashLoop)
    kubectl get events --field-selector involvedObject.name=<pod>
    kubectl top pod (if metrics-server available; soft failure → warn)

Outputs the classifications used by references/pod_lifecycle.md:
    Pending  | ContainerCreating | Running-NotReady | Running-Restarting
    | Running-Healthy | Terminating | CrashLoopBackOff | ImagePullBackOff
    | OOMKilled

Each finding includes a `hint` field pointing to the relevant section in
references/ for the LLM to follow.

See:
    .trellis/tasks/05-26-k8s-debug-diagnose-pod/{prd,design,implement}.md
    .trellis/spec/backend/error-handling.md  (exit codes 80-87)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import emit, json_result  # noqa: E402, I001
from lib.kubectl import KubectlError, KubectlResult, run_kubectl  # noqa: E402

PhaseLiteral = Literal[
    "Pending",
    "ContainerCreating",
    "Running-NotReady",
    "Running-Restarting",
    "Running-Healthy",
    "Terminating",
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "OOMKilled",
]

# 10 MB cap per raw stream — matches ssh-core Safety baseline.
_TEN_MB = 10 * 1024 * 1024
_TRUNCATE_MARKER = "\n... <truncated 10MB cap>\n"

# Phase → hint anchor table (design.md D5). The anchors below are
# verified against references/{common_issues,pod_lifecycle}.md at the
# tail of this module via `_HINTS_TABLE`.
_PHASE_HINTS: dict[str, str] = {
    "Pending": "references/pod_lifecycle.md#阶段-1-pending",
    "ContainerCreating": "references/pod_lifecycle.md#阶段-2-containercreating",
    "Running-NotReady": "references/pod_lifecycle.md#3a-running-但-readyfalse",
    "Running-Restarting": "references/pod_lifecycle.md#3b-running-但有-restart",
    "Running-Healthy": "",
    "Terminating": "references/pod_lifecycle.md#阶段-4-terminating",
    "CrashLoopBackOff": "references/common_issues.md#crashloopbackoff",
    "ImagePullBackOff": "references/common_issues.md#imagepullbackoff--errimagepull",
    "OOMKilled": "references/common_issues.md#oomkilled",
}

_PHASE_SEVERITY: dict[str, str] = {
    "Pending": "warn",
    "ContainerCreating": "warn",
    "Running-NotReady": "warn",
    "Running-Restarting": "warn",
    "Running-Healthy": "ok",
    "Terminating": "warn",
    "CrashLoopBackOff": "crit",
    "ImagePullBackOff": "crit",
    "OOMKilled": "crit",
}


def safe_dig(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested dict keys, returning ``default`` instead of KeyError.

    Tolerates ``None`` mid-path so we don't have to ``or {}`` everywhere.
    """
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _container_states(pod_status: dict[str, Any]) -> list[dict[str, Any]]:
    cs = safe_dig(pod_status, "status", "containerStatuses", default=None)
    if not isinstance(cs, list):
        return []
    return cs


def classify_phase(pod_status_data: dict[str, Any]) -> PhaseLiteral:  # noqa: PLR0911, PLR0912
    """Map a ``kubectl get pod -o json`` payload to one of 9 phase strings.

    Order is most-specific → most-general; first match wins. See
    design.md D3 for the full state machine.
    """
    phase = safe_dig(pod_status_data, "status", "phase")
    deletion_ts = safe_dig(pod_status_data, "metadata", "deletionTimestamp")
    containers = _container_states(pod_status_data)

    # Terminating dominates anything Running/Pending — pod is on its way out.
    if deletion_ts:
        return "Terminating"

    if phase == "Pending":
        for c in containers:
            reason = safe_dig(c, "state", "waiting", "reason") or ""
            if reason in ("ImagePullBackOff", "ErrImagePull"):
                return "ImagePullBackOff"
        for c in containers:
            reason = safe_dig(c, "state", "waiting", "reason") or ""
            if reason == "ContainerCreating":
                return "ContainerCreating"
        return "Pending"

    if phase == "Running":
        # CrashLoopBackOff: explicit waiting-reason marker, even on Running.
        for c in containers:
            reason = safe_dig(c, "state", "waiting", "reason") or ""
            if reason == "CrashLoopBackOff":
                return "CrashLoopBackOff"
        # OOMKilled: lastState.terminated.reason == "OOMKilled".
        for c in containers:
            reason = safe_dig(c, "lastState", "terminated", "reason") or ""
            if reason == "OOMKilled":
                return "OOMKilled"
        # restartCount > 0 + currently ready → recovered but has history.
        for c in containers:
            if (c.get("restartCount") or 0) > 0 and c.get("ready") is True:
                return "Running-Restarting"
        # Any container not ready → readiness probe failing or similar.
        for c in containers:
            if c.get("ready") is False:
                return "Running-NotReady"
        return "Running-Healthy"

    if phase == "Succeeded":
        return "Running-Healthy"

    if phase == "Failed":
        return "CrashLoopBackOff"

    # Unknown phase fallthrough.
    return "Pending"


def _phase_severity(phase: PhaseLiteral) -> str:
    return _PHASE_SEVERITY.get(phase, "warn")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="diagnose_pod.py")
    p.add_argument("host")
    p.add_argument("namespace")
    p.add_argument("pod")
    p.add_argument("--container", default=None, help="Specific container in the pod")
    p.add_argument("--logs-tail", type=int, default=200)
    p.add_argument(
        "--previous",
        action="store_true",
        help="Include --previous container logs (crucial for CrashLoop)",
    )
    p.add_argument("--json", action="store_true")
    return p


def _truncate_10mb(s: str) -> str:
    if len(s) > _TEN_MB:
        return s[:_TEN_MB] + _TRUNCATE_MARKER
    return s


def _utc_now() -> float:
    return time.time()


def _event_severity(event: dict[str, Any]) -> str:
    return "warn" if (event.get("type") == "Warning") else "info"


def _build_findings(
    phase: PhaseLiteral,
    pod_name: str,
    pod_status_data: dict[str, Any],
    events_data: dict[str, Any] | None,
    top_failed: bool,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    # Pod-level finding.
    restart_count = 0
    for c in _container_states(pod_status_data):
        rc = c.get("restartCount") or 0
        restart_count = max(restart_count, rc)

    pod_finding: dict[str, Any] = {
        "severity": _phase_severity(phase),
        "kind": "Pod",
        "name": pod_name,
        "phase": phase,
        "restart_count": restart_count,
    }
    hint = _PHASE_HINTS.get(phase, "")
    if hint:
        pod_finding["hint"] = hint
    findings.append(pod_finding)

    # Event-level Warning findings (recent only).
    if events_data and isinstance(events_data.get("items"), list):
        for ev in events_data["items"]:
            if ev.get("type") != "Warning":
                continue
            reason = ev.get("reason") or ""
            message = ev.get("message") or ""
            findings.append(
                {
                    "severity": "warn",
                    "kind": "Event",
                    "name": (ev.get("metadata") or {}).get("name") or "",
                    "reason": reason,
                    "message": message,
                }
            )

    if top_failed:
        findings.append(
            {
                "severity": "warn",
                "kind": "Pod",
                "name": pod_name,
                "reason": "metrics_unavailable",
                "message": "kubectl top pod failed (metrics-server may not be installed)",
                "hint": _PHASE_HINTS["OOMKilled"],
            }
        )

    return findings


def _diagnose(args: argparse.Namespace) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    """Returns the JSON envelope dict.

    Raises ``KubectlError`` only for hard failures; soft 87 is folded
    into a warn finding.
    """
    host = args.host
    namespace = args.namespace
    pod = args.pod

    # Step 1: probe namespace exists. We probe on the same host the Pod
    # lookup will use, so a remote-only `KUBECONFIG` works correctly.
    run_kubectl(host, None, ["get", "ns", namespace])

    # Step 2: probe pod exists + grab JSON status.
    pod_lookup = run_kubectl(
        host, namespace, ["get", "pod", pod], use_json=True, timeout=30
    )
    try:
        pod_status_data = json.loads(pod_lookup.stdout) if pod_lookup.stdout else {}
    except json.JSONDecodeError as e:
        return json_result(
            success=False,
            exit_code=1,
            stderr=f"failed to parse pod status JSON: {e}",
            data={
                "host": host,
                "namespace": namespace,
                "pod": pod,
                "failure_class": None,
            },
        )

    # Step 3: classify.
    phase = classify_phase(pod_status_data)

    # Step 4: plan kubectl calls.
    log_args: list[str] = ["logs", pod, f"--tail={args.logs_tail}"]
    if args.container:
        log_args += ["-c", args.container]
    prev_args: list[str] = list(log_args) + ["--previous"]
    want_previous = args.previous or phase == "CrashLoopBackOff"

    calls: dict[str, tuple[str | None, list[str], bool]] = {
        "describe": (namespace, ["describe", "pod", pod], False),
        "events": (
            namespace,
            [
                "get",
                "events",
                "--field-selector",
                f"involvedObject.name={pod}",
            ],
            True,
        ),
        "current_logs": (namespace, log_args, False),
    }
    if want_previous:
        calls["previous_logs"] = (namespace, prev_args, False)
    calls["top"] = (namespace, ["top", "pod", pod], False)

    # Step 5: run concurrently.
    raw: dict[str, str] = {}
    top_failed = False
    results: dict[str, KubectlResult] = {}
    errors: dict[str, KubectlError] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                run_kubectl, host, ns, k_args, use_json=use_json, timeout=30
            ): name
            for name, (ns, k_args, use_json) in calls.items()
        }
        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except KubectlError as e:
                if name == "top" and e.failure_class == "metrics_unavailable":
                    # Should never trigger (soft 87 returns a result, not
                    # raises) but keep belt-and-braces.
                    top_failed = True
                elif name == "top":
                    # Any other top failure → demote to soft warning.
                    top_failed = True
                else:
                    errors[name] = e

    # If a non-top call hard-failed, re-raise the first error.
    if errors:
        # describe / events / logs failure: surface as hard error.
        first = next(iter(errors.values()))
        raise first

    # Step 6: build findings.
    events_data: dict[str, Any] | None = None
    if "events" in results:
        try:
            events_data = json.loads(results["events"].stdout or "{}")
        except json.JSONDecodeError:
            events_data = None

    # Mark top as failed when soft 87 was returned.
    if "top" in results and results["top"].failure_class == "metrics_unavailable":
        top_failed = True

    findings = _build_findings(phase, pod, pod_status_data, events_data, top_failed)

    # Step 7: build raw payload (with 10MB truncation).
    raw = {
        "describe": _truncate_10mb(results.get("describe", _empty()).stdout or ""),
        "current_logs": _truncate_10mb(results.get("current_logs", _empty()).stdout or ""),
        "events": _truncate_10mb(results.get("events", _empty()).stdout or ""),
        "top": _truncate_10mb(results.get("top", _empty()).stdout or ""),
    }
    if "previous_logs" in results:
        raw["previous_logs"] = _truncate_10mb(results["previous_logs"].stdout or "")

    # Step 8: assemble envelope.
    summary = {"pods": _phase_severity(phase)}
    return json_result(
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        data={
            "host": host,
            "namespace": namespace,
            "pod": pod,
            "phase": phase,
            "summary": summary,
            "findings": findings,
            "raw": raw,
            "failure_class": None,
        },
    )


def _empty() -> KubectlResult:
    return KubectlResult(
        stdout="", stderr="", returncode=0, elapsed_s=0.0, failure_class=None, route="local"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _diagnose(args)
    except KubectlError as e:
        result = json_result(
            success=False,
            exit_code=e.exit_code,
            stderr=e.stderr,
            data={
                "host": args.host,
                "namespace": args.namespace,
                "pod": args.pod,
                "failure_class": e.failure_class,
            },
        )
    except Exception as e:  # pragma: no cover — last-ditch safety
        result = json_result(
            success=False,
            exit_code=1,
            stderr=f"diagnose_pod unexpected error: {type(e).__name__}: {e}",
            data={
                "host": args.host,
                "namespace": args.namespace,
                "pod": args.pod,
                "failure_class": None,
            },
        )
    emit(result, as_json=args.json)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
