"""Unit tests for iac-state/scripts/drift_check.py v1.0."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "iac-state" / "skills" / "iac-state" / "scripts"

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import drift_check  # noqa: E402, I001
from lib.terraform import TfResult  # noqa: E402

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _tf(stdout: str = "", stderr: str = "", returncode: int = 0, args: list[str] | None = None) -> TfResult:
    return TfResult(stdout, stderr, returncode, 0.01, args or ["terraform"], "local")


def _project(tmp_path: Path) -> Path:
    (tmp_path / "main.tf").write_text("terraform {}\n")
    return tmp_path


def _plan(changes: list[dict[str, Any]]) -> str:
    return json.dumps({"format_version": "1.2", "resource_changes": changes})


def _change(address: str, resource_type: str, actions: list[str]) -> dict[str, Any]:
    return {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": address.rsplit(".", 1)[-1],
        "change": {"actions": actions},
    }


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        ([], "no-op"),
        (["no-op"], "no-op"),
        (["create"], "create"),
        (["update"], "update-in-place"),
        (["delete"], "destroy"),
        (["delete", "create"], "replace"),
        (["create", "delete"], "replace"),
    ],
)
def test_classify_actions(actions: list[str], expected: str) -> None:
    assert drift_check._classify_action(actions) == expected


@pytest.mark.parametrize(
    ("action", "resource_type", "address", "expected"),
    [
        ("destroy", "aws_s3_bucket", "aws_s3_bucket.data", "crit"),
        ("replace", "aws_instance", "aws_instance.web", "crit"),
        ("create", "aws_instance", "aws_instance.web", "warn"),
        ("update-in-place", "aws_security_group_rule", "aws_security_group_rule.web", "warn"),
        ("update-in-place", "aws_instance", "aws_instance.web_tags", "info"),
        ("update-in-place", "aws_instance", "aws_instance.web", "warn"),
    ],
)
def test_severity_table(action: str, resource_type: str, address: str, expected: str) -> None:
    assert drift_check._severity(action, resource_type, address) == expected


def test_parse_show_json_requires_resource_changes() -> None:
    with pytest.raises(Exception) as exc:
        drift_check._parse_show_json("{}")
    assert exc.value.failure_class == "show_parse_failed"


@pytest.mark.parametrize(
    ("action", "resource_type", "address", "fragment"),
    [
        ("destroy", "aws_lambda_function", "aws_lambda_function.fn", "resource-deleted-in-cloud"),
        ("replace", "aws_instance", "aws_instance.web", "interpreting-plan-diffs"),
        ("create", "aws_s3_bucket", "aws_s3_bucket.data", "name-namespace-conflicts"),
        ("update-in-place", "aws_iam_role", "aws_iam_role.app", "manual-console-changes"),
        ("update-in-place", "aws_instance", "aws_instance.web_tags", "provider-managed-fields"),
        ("update-in-place", "aws_instance", "aws_instance.web", "common-drift-patterns"),
    ],
)
def test_hint_fragments(action: str, resource_type: str, address: str, fragment: str) -> None:
    assert drift_check._hint(action, resource_type, address).endswith(fragment)


def test_sort_findings_by_severity_action_resource() -> None:
    findings = [
        {"severity": "warn", "action": "update-in-place", "resource": "z"},
        {"severity": "crit", "action": "destroy", "resource": "b"},
        {"severity": "crit", "action": "replace", "resource": "a"},
        {"severity": "warn", "action": "create", "resource": "a"},
    ]
    sorted_findings = drift_check._sort_findings(findings)
    assert [(f["severity"], f["action"], f["resource"]) for f in sorted_findings] == [
        ("crit", "destroy", "b"),
        ("crit", "replace", "a"),
        ("warn", "create", "a"),
        ("warn", "update-in-place", "z"),
    ]


def test_truncate_10mb_reports_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drift_check, "_TEN_MB", 3)
    truncated, did_truncate = drift_check._truncate_10mb("abcdef")
    assert did_truncate is True
    assert truncated.startswith("abc")
    assert "truncated 10MB cap" in truncated


def test_new_plan_path_is_unique() -> None:
    assert drift_check._new_plan_path("remote-a") != drift_check._new_plan_path("remote-a")


def test_validate_local_project_requires_tf_file(tmp_path: Path) -> None:
    with pytest.raises(Exception) as exc:
        drift_check._validate_local_project(str(tmp_path))
    assert exc.value.failure_class == "project_dir_invalid"


def test_no_drift_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path)
    removed: list[str] = []
    monkeypatch.setattr(drift_check, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(drift_check, "_new_plan_path", lambda _host: "/tmp/plan.tfplan")
    monkeypatch.setattr(drift_check, "remove_path", lambda _host, path: removed.append(path))

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "init":
            return _tf(args=args)
        if args[0] == "plan":
            assert "-detailed-exitcode" in args
            assert "-lock-timeout=1s" in args
            assert "-out=/tmp/plan.tfplan" in args
            return _tf(returncode=0, args=args)
        raise AssertionError(args)

    monkeypatch.setattr(drift_check, "run_tf", fake_run_tf)
    rc = drift_check.main(["local", str(project), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["summary"]["drift"] == "ok"
    assert removed == ["/tmp/plan.tfplan"]


def test_warn_drift_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(drift_check, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(drift_check, "_new_plan_path", lambda _host: "/tmp/plan.tfplan")
    monkeypatch.setattr(drift_check, "remove_path", lambda *_a: None)
    show_json = _plan(
        [
            _change("aws_instance.web", "aws_instance", ["create"]),
            _change("aws_instance.web_tags", "aws_instance", ["update"]),
        ]
    )

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "init":
            return _tf(args=args)
        if args[0] == "plan":
            return _tf(returncode=2, args=args)
        if args[0] == "show":
            return _tf(show_json, args=args)
        raise AssertionError(args)

    monkeypatch.setattr(drift_check, "run_tf", fake_run_tf)
    rc = drift_check.main(["local", str(project), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["summary"]["drift"] == "warn"
    assert env["data"]["summary"]["severity_counts"] == {"crit": 0, "warn": 1, "info": 1}
    assert [f["action"] for f in env["data"]["findings"]] == ["create", "update-in-place"]


def test_crit_drift_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(drift_check, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(drift_check, "_new_plan_path", lambda _host: "/tmp/plan.tfplan")
    monkeypatch.setattr(drift_check, "remove_path", lambda *_a: None)
    show_json = _plan([_change("aws_db_instance.main", "aws_db_instance", ["delete"])])

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "init":
            return _tf(args=args)
        if args[0] == "plan":
            return _tf(returncode=2, args=args)
        if args[0] == "show":
            return _tf(show_json, args=args)
        raise AssertionError(args)

    monkeypatch.setattr(drift_check, "run_tf", fake_run_tf)
    rc = drift_check.main(["local", str(project), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert env["success"] is False
    assert env["data"]["failure_class"] is None
    assert env["data"]["summary"]["drift"] == "crit"


def test_target_passed_to_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(drift_check, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(drift_check, "_new_plan_path", lambda _host: "/tmp/plan.tfplan")
    monkeypatch.setattr(drift_check, "remove_path", lambda *_a: None)

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "init":
            return _tf(args=args)
        if args[0] == "plan":
            assert "-target=aws_instance.web" in args
            return _tf(returncode=0, args=args)
        raise AssertionError(args)

    monkeypatch.setattr(drift_check, "run_tf", fake_run_tf)
    rc = drift_check.main(["local", str(project), "--target", "aws_instance.web", "--json"])
    assert rc == 0
    json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("stage", "stderr", "target", "exit_code", "failure_class"),
    [
        ("init", "backend failed", None, 130, "init_failed"),
        ("plan", "plan failed", None, 131, "plan_failed"),
        ("plan", "Error acquiring the state lock", None, 134, "lock_held"),
        ("plan", "No matching resource address target exists", "aws_instance.web", 133, "target_not_found"),
        ("plan", "No valid credential sources found", None, 135, "provider_auth_failed"),
    ],
)
def test_stage_error_mapping(
    stage: str,
    stderr: str,
    target: str | None,
    exit_code: int,
    failure_class: str,
) -> None:
    err = drift_check._stage_error(stage, stderr, target=target)
    assert err.exit_code == exit_code
    assert err.failure_class == failure_class


def test_show_parse_failure_from_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(drift_check, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(drift_check, "_new_plan_path", lambda _host: "/tmp/plan.tfplan")
    monkeypatch.setattr(drift_check, "remove_path", lambda *_a: None)

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args[0] == "init":
            return _tf(args=args)
        if args[0] == "plan":
            return _tf(returncode=2, args=args)
        if args[0] == "show":
            return _tf("{not-json", args=args)
        raise AssertionError(args)

    monkeypatch.setattr(drift_check, "run_tf", fake_run_tf)
    rc = drift_check.main(["local", str(project), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 132
    assert env["data"]["failure_class"] == "show_parse_failed"


def test_human_failure_surfaces_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(drift_check, "resolve_tool", lambda *_a: "terraform")
    monkeypatch.setattr(drift_check, "_new_plan_path", lambda _host: "/tmp/plan.tfplan")
    monkeypatch.setattr(drift_check, "remove_path", lambda *_a: None)
    monkeypatch.setattr(drift_check, "run_tf", lambda *_a, **_kw: _tf(stderr="bad", returncode=1))
    rc = drift_check.main(["local", str(project)])
    captured = capsys.readouterr()
    assert rc == 130
    assert "bad" in captured.err


def test_shared_tool_error_passes_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(
        drift_check,
        "resolve_tool",
        lambda *_a: (_ for _ in ()).throw(drift_check.TfError("tool_missing", 121, "missing")),
    )
    rc = drift_check.main(["local", str(project), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 121
    assert env["data"]["failure_class"] == "tool_missing"
