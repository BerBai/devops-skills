"""Unit tests for k8s-debug/scripts/cluster_health.py v1.0."""

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
import cluster_health  # noqa: E402, I001
from lib.kubectl import KubectlError, KubectlResult  # noqa: E402

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _result(payload: dict[str, Any] | None = None, *, stdout: str | None = None) -> KubectlResult:
    return KubectlResult(
        stdout=stdout if stdout is not None else json.dumps(payload or {"items": []}),
        stderr="",
        returncode=0,
        elapsed_s=0.01,
        failure_class=None,
        route="local",
    )


def _list(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items}


def _node(
    name: str = "node-a",
    *,
    ready: str = "True",
    pressure: str | None = None,
    unschedulable: bool = False,
    kubelet: str = "v1.31.1",
) -> dict[str, Any]:
    conditions = [
        {"type": "Ready", "status": ready, "message": f"ready={ready}"},
        {"type": "DiskPressure", "status": "False"},
        {"type": "MemoryPressure", "status": "False"},
        {"type": "PIDPressure", "status": "False"},
        {"type": "NetworkUnavailable", "status": "False"},
    ]
    if pressure:
        for condition in conditions:
            if condition["type"] == pressure:
                condition["status"] = "True"
                condition["message"] = f"{pressure}=True"
    return {
        "metadata": {"name": name},
        "spec": {"unschedulable": unschedulable},
        "status": {"conditions": conditions, "nodeInfo": {"kubeletVersion": kubelet}},
    }


def _pod(
    name: str,
    *,
    phase: str = "Running",
    ready: bool = True,
    restart_count: int = 0,
    waiting_reason: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    state = {"running": {}}
    if waiting_reason:
        state = {"waiting": {"reason": waiting_reason}}
    return {
        "metadata": {"name": name, "labels": labels or {}},
        "status": {
            "phase": phase,
            "containerStatuses": [
                {
                    "name": "app",
                    "ready": ready,
                    "restartCount": restart_count,
                    "state": state,
                    "lastState": {},
                }
            ],
        },
    }


def test_score_nodes_ok() -> None:
    score, findings = cluster_health._score_nodes(_list([_node()]))
    assert score == "ok"
    assert findings == []


def test_score_nodes_not_ready_crit() -> None:
    score, findings = cluster_health._score_nodes(_list([_node(ready="False")]))
    assert score == "crit"
    assert findings[0]["reason"] == "NotReady"


@pytest.mark.parametrize("pressure", ["DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"])
def test_score_nodes_pressure_warn(pressure: str) -> None:
    score, findings = cluster_health._score_nodes(_list([_node(pressure=pressure)]))
    assert score == "warn"
    assert findings[0]["reason"] == pressure


def test_score_nodes_unschedulable_warn() -> None:
    score, findings = cluster_health._score_nodes(_list([_node(unschedulable=True)]))
    assert score == "warn"
    assert findings[0]["reason"] == "SchedulingDisabled"


def test_score_control_plane_visible_pods_ok() -> None:
    payload = _list([_pod("kube-apiserver-kind", labels={"component": "kube-apiserver"})])
    score, findings = cluster_health._score_control_plane(payload, {}, {})
    assert score == "ok"
    assert findings == []


@pytest.mark.parametrize("reason", ["CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"])
def test_score_control_plane_visible_pods_crit(reason: str) -> None:
    payload = _list([
        _pod("kube-apiserver-kind", ready=False, waiting_reason=reason, labels={"component": "kube-apiserver"})
    ])
    score, findings = cluster_health._score_control_plane(payload, {}, {})
    assert score == "crit"
    assert findings[0]["reason"] == reason


def test_score_control_plane_managed_raw_ok() -> None:
    score, findings = cluster_health._score_control_plane(_list([]), {"healthz": "ok\n", "readyz": "ok\n"}, {})
    assert score == "ok"
    assert findings == []


def test_score_control_plane_managed_raw_not_ok_crit() -> None:
    score, findings = cluster_health._score_control_plane(_list([]), {"healthz": "ok", "readyz": "failed"}, {})
    assert score == "crit"
    assert findings[0]["name"] == "readyz"


def test_score_control_plane_managed_unavailable_warn() -> None:
    errors = {
        "healthz": KubectlError("forbidden", 85, "denied"),
        "readyz": KubectlError("forbidden", 85, "denied"),
    }
    score, findings = cluster_health._score_control_plane(_list([]), {}, errors)
    assert score == "warn"
    assert findings[0]["reason"] == "healthz_unavailable"


def test_score_kube_system_ok() -> None:
    score, findings = cluster_health._score_kube_system(_list([_pod("coredns")]))
    assert score == "ok"
    assert findings == []


def test_score_kube_system_failed_crit() -> None:
    score, findings = cluster_health._score_kube_system(_list([_pod("coredns", phase="Failed")]))
    assert score == "crit"
    assert findings[0]["reason"] == "Failed"


def test_score_kube_system_pending_warn() -> None:
    score, findings = cluster_health._score_kube_system(_list([_pod("coredns", phase="Pending")]))
    assert score == "warn"
    assert findings[0]["reason"] == "Pending"


def test_score_events_ok() -> None:
    score, findings = cluster_health._score_events(_list([]))
    assert score == "ok"
    assert findings == []


@pytest.mark.parametrize(
    "reason",
    ["NodeNotReady", "Failed", "FailedMount", "FailedScheduling", "BackOff", "Unhealthy", "Killing", "OOMKilling"],
)
def test_score_events_crit_reasons(reason: str) -> None:
    score, findings = cluster_health._score_events(
        _list([{"metadata": {"name": "e"}, "type": "Warning", "reason": reason, "message": "bad"}])
    )
    assert score == "crit"
    assert findings[0]["reason"] == reason


def test_score_events_many_warnings_warn() -> None:
    events = [
        {"metadata": {"name": f"e{i}"}, "type": "Warning", "reason": "Other", "message": "m"}
        for i in range(3)
    ]
    score, findings = cluster_health._score_events(_list(events))
    assert score == "warn"
    assert findings[0]["reason"] == "ManyWarnings"


def test_score_versions_ok() -> None:
    score, findings = cluster_health._score_versions(_list([_node(kubelet="v1.31.1"), _node("node-b", kubelet="v1.32.0")]), "Client Version: v1.32.0", None)
    assert score == "ok"
    assert findings == []


def test_score_versions_minor_skew_warn() -> None:
    score, findings = cluster_health._score_versions(_list([_node(kubelet="v1.29.1"), _node("node-b", kubelet="v1.32.0")]), "Client Version: v1.32.0", None)
    assert score == "warn"
    assert findings[0]["reason"] == "MinorSkew"


def test_score_versions_partial_warn() -> None:
    score, findings = cluster_health._score_versions(_list([_node()]), None, KubectlError("api_timeout", 86, "slow"))
    assert score == "warn"
    assert findings[0]["reason"] == "versions_partial"


def test_top_findings_stable_sort() -> None:
    findings = [
        {"severity": "warn", "domain": "versions", "kind": "Version", "name": "z", "reason": "R"},
        {"severity": "crit", "domain": "kube_system", "kind": "Pod", "name": "p", "reason": "R"},
        {"severity": "crit", "domain": "nodes", "kind": "Node", "name": "n", "reason": "R"},
        {"severity": "warn", "domain": "events", "kind": "Event", "name": "e", "reason": "R"},
        {"severity": "warn", "domain": "control_plane", "kind": "Pod", "name": "c", "reason": "R"},
        {"severity": "warn", "domain": "nodes", "kind": "Node", "name": "b", "reason": "R"},
    ]
    top = cluster_health._top_findings(findings)
    assert [f["domain"] for f in top] == ["nodes", "kube_system", "nodes", "control_plane", "events"]


def test_success_envelope(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(_host: str, namespace: str | None, args: list[str], **_kwargs: Any) -> KubectlResult:
        if args[:2] == ["get", "nodes"]:
            return _result(_list([_node()]))
        if namespace == "kube-system" and args[:2] == ["get", "pods"]:
            return _result(_list([_pod("coredns")]))
        if namespace == "kube-system" and args[:2] == ["get", "events"]:
            return _result(_list([]))
        if args in (["get", "--raw=/healthz"], ["get", "--raw=/readyz"]):
            return _result(stdout="ok\n")
        if args == ["version", "--short"]:
            return _result(stdout="Client Version: v1.31.0\nServer Version: v1.31.0\n")
        return _result()

    monkeypatch.setattr(cluster_health, "run_kubectl", fake_run)
    rc = cluster_health.main(["local", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["summary"] == {
        "nodes": "ok",
        "control_plane": "ok",
        "kube_system": "ok",
        "events": "ok",
        "versions": "ok",
    }


def test_node_probe_api_unreachable_becomes_100(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> KubectlResult:
        raise KubectlError("host_unreachable", 80, "Unable to connect to the server: dial tcp timeout")

    monkeypatch.setattr(cluster_health, "run_kubectl", fake_run)
    rc = cluster_health.main(["local", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 100
    assert env["data"]["failure_class"] == "cluster_unreachable"


def test_node_probe_ssh_unreachable_passes_through_80(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> KubectlResult:
        raise KubectlError("host_unreachable", 80, "ssh_execute timeout after 45s")

    monkeypatch.setattr(cluster_health, "run_kubectl", fake_run)
    rc = cluster_health.main(["bastion", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 80
    assert env["data"]["failure_class"] == "host_unreachable"


def test_version_collection_failure_is_soft(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_run(_host: str, namespace: str | None, args: list[str], **_kwargs: Any) -> KubectlResult:
        if args[:2] == ["get", "nodes"]:
            return _result(_list([_node()]))
        if args == ["version", "--short"]:
            raise KubectlError("api_timeout", 86, "slow")
        if namespace == "kube-system":
            return _result(_list([]))
        return _result(stdout="ok\n")

    monkeypatch.setattr(cluster_health, "run_kubectl", fake_run)
    rc = cluster_health.main(["local", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"]["versions"] == "warn"
    assert env["data"]["partial_failures"][0]["domain"] == "version"


def test_since_normalization_and_warning(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cluster_health, "run_kubectl", lambda *_a, **_kw: _result(_list([_node()])))
    rc = cluster_health.main(["local", "--since", "2000", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["since_min"] == 2000
    assert env["data"]["warnings"]


def test_human_success_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cluster_health, "run_kubectl", lambda *_a, **_kw: _result(_list([_node()])))
    rc = cluster_health.main(["local"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Cluster health on local" in captured.out
    assert "nodes" in captured.out
