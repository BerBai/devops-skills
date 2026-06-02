"""Unit tests for k8s-debug/scripts/check_namespace.py v1.0."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "k8s-debug" / "skills" / "k8s-debug" / "scripts"

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import check_namespace  # noqa: E402, I001
from lib.kubectl import KubectlError, KubectlResult  # noqa: E402

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _result(payload: dict[str, Any] | None = None) -> KubectlResult:
    return KubectlResult(
        stdout=json.dumps(payload or {"items": []}),
        stderr="",
        returncode=0,
        elapsed_s=0.01,
        failure_class=None,
        route="local",
    )


def _list(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items}


def _pod(
    name: str,
    *,
    phase: str = "Running",
    ready: bool = True,
    restart_count: int = 0,
    waiting_reason: str | None = None,
    last_reason: str | None = None,
) -> dict[str, Any]:
    state = {"running": {}}
    if waiting_reason:
        state = {"waiting": {"reason": waiting_reason}}
    last_state = {"terminated": {"reason": last_reason}} if last_reason else {}
    return {
        "metadata": {"name": name},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": ready,
                    "restartCount": restart_count,
                    "state": state,
                    "lastState": last_state,
                }
            ],
        },
    }


def test_score_pods_ok() -> None:
    score, findings = check_namespace._score_pods(_list([_pod("ok")]))
    assert score == "ok"
    assert findings == []


def test_score_pods_warn_pending() -> None:
    score, findings = check_namespace._score_pods(_list([_pod("p", phase="Pending")]))
    assert score == "warn"
    assert findings[0]["reason"] == "Pending"


def test_score_pods_crit_crashloop() -> None:
    score, findings = check_namespace._score_pods(_list([_pod("p", ready=False, waiting_reason="CrashLoopBackOff")]))
    assert score == "crit"
    assert any(f["reason"] == "CrashLoopBackOff" for f in findings)


def test_score_pods_crit_many_pending() -> None:
    score, findings = check_namespace._score_pods(
        _list([_pod("a", phase="Pending"), _pod("b", phase="Pending"), _pod("c", phase="Failed")])
    )
    assert score == "crit"
    assert any(f["reason"] == "ManyPendingOrFailed" for f in findings)


def test_score_events_ok() -> None:
    score, findings = check_namespace._score_events(_list([]))
    assert score == "ok"
    assert findings == []


def test_score_events_warn_many_warnings() -> None:
    events = [
        {"metadata": {"name": f"e{i}"}, "type": "Warning", "reason": "Other", "message": "m"}
        for i in range(3)
    ]
    score, findings = check_namespace._score_events(_list(events))
    assert score == "warn"
    assert findings[0]["reason"] == "ManyWarnings"


def test_score_events_crit_reason() -> None:
    score, findings = check_namespace._score_events(
        _list([{"metadata": {"name": "e"}, "type": "Warning", "reason": "FailedMount", "message": "bad"}])
    )
    assert score == "crit"
    assert findings[0]["reason"] == "FailedMount"


def test_score_deployments_ok() -> None:
    dep = {"metadata": {"name": "d", "generation": 1}, "spec": {"replicas": 2}, "status": {"readyReplicas": 2, "availableReplicas": 2, "observedGeneration": 1}}
    score, findings = check_namespace._score_deployments(_list([dep]))
    assert score == "ok"
    assert findings == []


def test_score_deployments_crit_not_ready() -> None:
    dep = {"metadata": {"name": "d", "generation": 2}, "spec": {"replicas": 3}, "status": {"readyReplicas": 1, "availableReplicas": 1, "observedGeneration": 2}}
    score, findings = check_namespace._score_deployments(_list([dep]))
    assert score == "crit"
    assert findings[0]["reason"] == "ReplicasNotReady"


def test_score_services_empty_endpoints_crit() -> None:
    services = _list([{"metadata": {"name": "api"}, "spec": {"type": "ClusterIP"}}])
    endpoints = _list([{"metadata": {"name": "api"}, "subsets": []}])
    score, findings = check_namespace._score_services(services, endpoints)
    assert score == "crit"
    assert findings[0]["reason"] == "NoEndpoints"


def test_score_services_with_endpoint_ok() -> None:
    services = _list([{"metadata": {"name": "api"}, "spec": {"type": "ClusterIP"}}])
    endpoints = _list([{"metadata": {"name": "api"}, "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}])
    score, findings = check_namespace._score_services(services, endpoints)
    assert score == "ok"
    assert findings == []


def test_score_pvcs_ok_and_crit() -> None:
    ok, ok_findings = check_namespace._score_pvcs(_list([{"metadata": {"name": "data"}, "status": {"phase": "Bound"}}]))
    crit, crit_findings = check_namespace._score_pvcs(_list([{"metadata": {"name": "data"}, "status": {"phase": "Pending"}}]))
    assert ok == "ok"
    assert ok_findings == []
    assert crit == "crit"
    assert crit_findings[0]["reason"] == "Pending"


def test_top_findings_stable_sort() -> None:
    findings = [
        {"severity": "warn", "domain": "pvcs", "kind": "PVC", "name": "b", "reason": "R"},
        {"severity": "crit", "domain": "services", "kind": "Service", "name": "s", "reason": "R"},
        {"severity": "crit", "domain": "pods", "kind": "Pod", "name": "p", "reason": "R"},
        {"severity": "warn", "domain": "events", "kind": "Event", "name": "e", "reason": "R"},
    ]
    top = check_namespace._top_findings(findings)
    assert [f["domain"] for f in top] == ["pods", "services", "events"]


def test_namespace_not_found_pass_through(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> KubectlResult:
        raise KubectlError("namespace_not_found", 84, "missing")

    monkeypatch.setattr(check_namespace, "run_kubectl", fake_run)
    rc = check_namespace.main(["local", "missing", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 84
    assert env["data"]["failure_class"] == "namespace_not_found"


def test_check_success_envelope(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(_host: str, _namespace: str | None, args: list[str], **_kwargs: Any) -> KubectlResult:
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] == ["get", "pods"]:
            return _result(_list([_pod("ok")]))
        if args[:2] == ["get", "services"]:
            return _result(_list([{"metadata": {"name": "api"}, "spec": {"type": "ClusterIP"}}]))
        if args[:2] == ["get", "endpoints"]:
            return _result(_list([{"metadata": {"name": "api"}, "subsets": [{"addresses": [{"ip": "1.1.1.1"}]}]}]))
        return _result(_list([]))

    monkeypatch.setattr(check_namespace, "run_kubectl", fake_run)
    rc = check_namespace.main(["local", "default", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["summary"] == {
        "pods": "ok",
        "events": "ok",
        "deployments": "ok",
        "services": "ok",
        "pvcs": "ok",
    }
    assert env["data"]["partial_failures"] == []


def test_partial_collection_soft(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(_host: str, _namespace: str | None, args: list[str], **_kwargs: Any) -> KubectlResult:
        if args[:2] == ["get", "ns"]:
            return _result()
        if args[:2] in (["get", "events"], ["get", "pvc"]):
            raise KubectlError("api_timeout", 86, "slow")
        return _result(_list([]))

    monkeypatch.setattr(check_namespace, "run_kubectl", fake_run)
    rc = check_namespace.main(["local", "default", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert len(env["data"]["partial_failures"]) == 2
    assert env["data"]["summary"]["events"] == "warn"


def test_partial_collection_hard_88(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(_host: str, _namespace: str | None, args: list[str], **_kwargs: Any) -> KubectlResult:
        if args[:2] == ["get", "ns"]:
            return _result()
        raise KubectlError("api_timeout", 86, "slow")

    monkeypatch.setattr(check_namespace, "run_kubectl", fake_run)
    rc = check_namespace.main(["local", "default", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 88
    assert env["data"]["failure_class"] == "partial_collection"


def test_human_success_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(check_namespace, "run_kubectl", lambda *_a, **_kw: _result(_list([])))
    rc = check_namespace.main(["local", "default"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Namespace default on local" in captured.out
    assert "pods" in captured.out
