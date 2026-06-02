"""Unit tests for k8s-debug/scripts/helm_status.py v1.0."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
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
import helm_status  # noqa: E402, I001
from lib.kubectl import KubectlError, KubectlResult  # noqa: E402

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _helm(stdout: Any = None) -> helm_status.HelmResult:
    value = stdout if isinstance(stdout, str) else json.dumps(stdout if stdout is not None else {})
    return helm_status.HelmResult(value, "", 0, 0.01, ["helm"], "local")


def _kubectl(payload: dict[str, Any] | None = None) -> KubectlResult:
    return KubectlResult(
        stdout=json.dumps(payload or {"items": []}),
        stderr="",
        returncode=0,
        elapsed_s=0.01,
        failure_class=None,
        route="local",
    )


def _job(
    name: str,
    *,
    active: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    age_seconds: int = 0,
    failed_condition: bool = False,
) -> dict[str, Any]:
    start = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    conditions = [{"type": "Failed", "status": "True"}] if failed_condition else []
    return {
        "metadata": {"name": name, "creationTimestamp": start.isoformat().replace("+00:00", "Z")},
        "status": {
            "active": active,
            "succeeded": succeeded,
            "failed": failed,
            "startTime": start.isoformat().replace("+00:00", "Z"),
            "conditions": conditions,
        },
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"info": {"status": "deployed"}}, "deployed"),
        ({"status": {"info": {"status": "failed"}}}, "failed"),
        ({"status": "pending-upgrade"}, "pending-upgrade"),
    ],
)
def test_release_status_schema_variants(payload: dict[str, Any], expected: str) -> None:
    assert helm_status._release_status(payload) == expected


def test_history_entries_last_five_sorted() -> None:
    payload = [{"revision": i, "status": "deployed", "chart": f"c-{i}"} for i in range(1, 8)]
    entries = helm_status._history_entries(payload)
    assert [e["revision"] for e in entries] == [7, 6, 5, 4, 3]


def test_pending_rows_filters_and_sorts() -> None:
    rows = helm_status._pending_rows(
        [
            {"namespace": "b", "name": "z", "status": "deployed"},
            {"namespace": "b", "name": "a", "status": "pending-upgrade", "revision": "3"},
            {"namespace": "a", "name": "b", "status": "pending-install", "revision": 1},
        ]
    )
    assert [(r["namespace"], r["name"]) for r in rows] == [("a", "b"), ("b", "a")]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("deployed", "ok"),
        ("failed", "crit"),
        ("pending-install", "crit"),
        ("pending-upgrade", "crit"),
        ("pending-rollback", "crit"),
        ("superseded", "warn"),
        ("uninstalling", "warn"),
        ("uninstalled", "warn"),
    ],
)
def test_summary_for_all_statuses(status: str, expected: str) -> None:
    jobs = {"all": [], "stuck": [], "failed": []}
    assert helm_status._summary(status, jobs, False) == expected


def test_normalize_jobs_failed_and_stuck() -> None:
    payload = {"items": [_job("failed", failed=1), _job("stuck", active=1, age_seconds=600), _job("ok", succeeded=1)]}
    jobs = helm_status._normalize_jobs(payload)
    assert [j["name"] for j in jobs["failed"]] == ["failed"]
    assert [j["name"] for j in jobs["stuck"]] == ["stuck"]
    assert len(jobs["all"]) == 3


def test_normalize_jobs_failed_condition() -> None:
    jobs = helm_status._normalize_jobs({"items": [_job("failed-cond", failed_condition=True)]})
    assert jobs["failed"][0]["name"] == "failed-cond"


def test_recommend_pending_with_stuck_job() -> None:
    jobs = {"all": [], "stuck": [{"name": "hook"}], "failed": []}
    assert "delete stuck hook job" in helm_status._recommend("pending-upgrade", jobs)


def test_recommend_failed_release() -> None:
    jobs = {"all": [], "stuck": [], "failed": []}
    assert "rollback" in helm_status._recommend("failed", jobs)


def test_classify_helm_missing() -> None:
    assert helm_status._classify_helm_failure(127, "helm: command not found") == ("helm_missing", 110)


def test_classify_release_not_found() -> None:
    assert helm_status._classify_helm_failure(1, 'Error: release: not found') == ("release_not_found", 111)


def test_helm_major_parses_buildinfo() -> None:
    assert helm_status._helm_major('version.BuildInfo{Version:"v3.14.0"}') == 3


def test_ensure_helm2_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helm_status, "run_helm", lambda *_a, **_kw: _helm("v2.17.0\n"))
    with pytest.raises(helm_status.HelmError) as exc:
        helm_status._ensure_helm3("local")
    assert exc.value.failure_class == "helm_version_unsupported"


def test_top_findings_stable_sort() -> None:
    findings = [
        {"severity": "warn", "kind": "Job", "namespace": "b", "name": "z", "reason": "R"},
        {"severity": "crit", "kind": "HelmRelease", "namespace": "b", "name": "a", "reason": "R"},
        {"severity": "crit", "kind": "Job", "namespace": "a", "name": "a", "reason": "R"},
    ]
    top = helm_status._top_findings(findings)
    assert [(f["severity"], f["kind"], f["namespace"]) for f in top] == [
        ("crit", "HelmRelease", "b"),
        ("crit", "Job", "a"),
        ("warn", "Job", "b"),
    ]


def test_per_release_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_helm(_host: str, args: list[str], **_kwargs: Any) -> helm_status.HelmResult:
        if args == ["version", "--short"]:
            return _helm("v3.14.0\n")
        if "status" in args:
            return _helm({"info": {"status": "deployed"}})
        if "history" in args:
            return _helm([{"revision": 1, "status": "deployed"}])
        raise AssertionError(args)

    monkeypatch.setattr(helm_status, "run_helm", fake_helm)
    monkeypatch.setattr(helm_status, "run_kubectl", lambda *_a, **_kw: _kubectl())
    rc = helm_status.main(["local", "default", "api", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["status"] == "deployed"
    assert env["data"]["summary"] == "ok"


def test_per_release_history_failure_soft(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_helm(_host: str, args: list[str], **_kwargs: Any) -> helm_status.HelmResult:
        if args == ["version", "--short"]:
            return _helm("v3.14.0\n")
        if "status" in args:
            return _helm({"info": {"status": "deployed"}})
        if "history" in args:
            raise helm_status.HelmError("history_truncated", 113, "empty")
        raise AssertionError(args)

    monkeypatch.setattr(helm_status, "run_helm", fake_helm)
    monkeypatch.setattr(helm_status, "run_kubectl", lambda *_a, **_kw: _kubectl())
    rc = helm_status.main(["local", "default", "api", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"] == "warn"
    assert env["data"]["partial_failures"][0]["failure_class"] == "history_truncated"


def test_per_release_job_failure_affects_summary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        helm_status,
        "run_helm",
        lambda _host, args, **_kw: _helm("v3.14.0\n")
        if args == ["version", "--short"]
        else _helm({"info": {"status": "deployed"}})
        if "status" in args
        else _helm([]),
    )
    monkeypatch.setattr(helm_status, "run_kubectl", lambda *_a, **_kw: _kubectl({"items": [_job("bad", failed=1)]}))
    rc = helm_status.main(["local", "default", "api", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"] == "crit"
    assert env["data"]["associated_jobs"]["failed"][0]["name"] == "bad"


def test_list_pending_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_helm(_host: str, args: list[str], **_kwargs: Any) -> helm_status.HelmResult:
        if args == ["version", "--short"]:
            return _helm("v3.14.0\n")
        return _helm([{"namespace": "default", "name": "api", "status": "pending-upgrade", "revision": 2}])

    monkeypatch.setattr(helm_status, "run_helm", fake_helm)
    rc = helm_status.main(["local", "--list-pending", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"] == "crit"
    assert env["data"]["pending_releases"][0]["name"] == "api"


def test_release_not_found_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_helm(_host: str, args: list[str], **_kwargs: Any) -> helm_status.HelmResult:
        if args == ["version", "--short"]:
            return _helm("v3.14.0\n")
        raise helm_status.HelmError("release_not_found", 111, "not found")

    monkeypatch.setattr(helm_status, "run_helm", fake_helm)
    rc = helm_status.main(["local", "default", "missing", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 111
    assert env["data"]["failure_class"] == "release_not_found"


def test_kubectl_job_failure_soft(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        helm_status,
        "run_helm",
        lambda _host, args, **_kw: _helm("v3.14.0\n")
        if args == ["version", "--short"]
        else _helm({"info": {"status": "deployed"}})
        if "status" in args
        else _helm([]),
    )

    def fake_kubectl(*_args: Any, **_kwargs: Any) -> KubectlResult:
        raise KubectlError("forbidden", 85, "denied")

    monkeypatch.setattr(helm_status, "run_kubectl", fake_kubectl)
    rc = helm_status.main(["local", "default", "api", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["partial_failures"][0]["failure_class"] == "forbidden"


def test_bad_args_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = helm_status.main(["local", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert env["data"]["failure_class"] == "bad_args"


def test_human_pending_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        helm_status,
        "run_helm",
        lambda _host, args, **_kw: _helm("v3.14.0\n")
        if args == ["version", "--short"]
        else _helm([{"namespace": "default", "name": "api", "status": "pending-upgrade"}]),
    )
    rc = helm_status.main(["local", "--list-pending"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Pending Helm releases on local" in captured.out
    assert "default/api" in captured.out


def test_parse_k8s_time() -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    assert helm_status._parse_k8s_time(ts) is not None


def test_job_stuck_uses_current_time(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    monkeypatch.setattr(helm_status.time, "time", lambda: now)
    start = datetime.fromtimestamp(now - 600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {"items": [{"metadata": {"name": "hook"}, "status": {"active": 1, "succeeded": 0, "startTime": start}}]}
    assert helm_status._normalize_jobs(payload)["stuck"][0]["name"] == "hook"
