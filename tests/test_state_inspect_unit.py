"""Unit tests for iac-state/scripts/state_inspect.py v1.0."""

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
import state_inspect  # noqa: E402, I001
from lib.terraform import TfError, TfResult  # noqa: E402

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _tf(stdout: str = "", stderr: str = "", returncode: int = 0) -> TfResult:
    return TfResult(stdout, stderr, returncode, 0.01, ["terraform"], "local")


def _backend(backend_type: str = "local", config: dict[str, Any] | None = None) -> str:
    return json.dumps({"backend": {"type": backend_type, "config": config or {"path": "terraform.tfstate"}}})


def test_parse_backend_redacts_to_safe_keys() -> None:
    backend, finding = state_inspect._parse_backend(
        _backend("s3", {"bucket": "b", "key": "k", "access_key": "secret"})
    )
    assert finding is None
    assert backend["config"] == {"bucket": "b", "key": "k"}


@pytest.mark.parametrize("backend_type", ["local", "s3", "remote", "gcs", "azurerm"])
def test_parse_backend_known_types(backend_type: str) -> None:
    backend, finding = state_inspect._parse_backend(_backend(backend_type))
    assert backend["type"] == backend_type
    assert finding is None


@pytest.mark.parametrize("payload", ["not-json", "{}", json.dumps({"backend": {"config": {}}})])
def test_parse_backend_unknown_warns(payload: str) -> None:
    backend, finding = state_inspect._parse_backend(payload)
    assert backend["type"] == "unknown"
    assert finding is not None
    assert finding["reason"] == "backend_unknown"


def test_state_size_local_uses_backend_path(tmp_path: Path) -> None:
    state = tmp_path / "custom.tfstate"
    state.write_text("{}")
    size, finding = state_inspect._state_size(str(tmp_path), {"type": "local", "config": {"path": "custom.tfstate"}})
    assert size == 2
    assert finding is None


@pytest.mark.parametrize("backend_type", ["s3", "gcs", "azurerm", "remote"])
def test_state_size_remote_warns(tmp_path: Path, backend_type: str) -> None:
    size, finding = state_inspect._state_size(str(tmp_path), {"type": backend_type, "config": {}})
    assert size is None
    assert finding is not None
    assert finding["reason"] == "remote_size_unavailable"


def test_resource_count() -> None:
    assert state_inspect._resource_count("aws_instance.a\n\naws_s3_bucket.b\n") == 2


def test_parse_workspaces() -> None:
    assert state_inspect._parse_workspaces("* default\n  prod\n") == [
        {"name": "default", "current": True},
        {"name": "prod", "current": False},
    ]


def test_parse_lock_fields() -> None:
    stderr = """
Error acquiring the state lock
ID: abc
Operation: OperationTypePlan
Who: alice@runner
Created: 2026-06-02T00:00:00Z
"""
    locked, info, finding = state_inspect._parse_lock(stderr)
    assert locked is True
    assert info and info["who"] == "alice@runner"
    assert finding is None


def test_parse_lock_failed() -> None:
    locked, info, finding = state_inspect._parse_lock("Error acquiring the state lock")
    assert locked is True
    assert info is None
    assert finding and finding["reason"] == "lock_parse_failed"


def test_parse_lock_no_lock() -> None:
    locked, info, finding = state_inspect._parse_lock("No changes. Infrastructure is up-to-date.")
    assert locked is False
    assert info is None
    assert finding is None


@pytest.mark.parametrize(
    ("findings", "locked", "expected"),
    [
        ([], False, "ok"),
        ([{"severity": "warn"}], False, "warn"),
        ([], True, "crit"),
    ],
)
def test_score(findings: list[dict[str, Any]], locked: bool, expected: str) -> None:
    assert state_inspect._score(findings, locked) == expected


def test_top_findings_stable_sort() -> None:
    findings = [
        {"severity": "warn", "kind": "workspace", "name": "b", "reason": "R"},
        {"severity": "crit", "kind": "lock", "name": "state-lock", "reason": "locked"},
        {"severity": "warn", "kind": "backend", "name": "backend", "reason": "backend_unknown"},
    ]
    top = state_inspect._top_findings(findings)
    assert [(f["severity"], f["kind"]) for f in top] == [
        ("crit", "lock"),
        ("warn", "backend"),
        ("warn", "workspace"),
    ]


def test_validate_local_project_errors(tmp_path: Path) -> None:
    with pytest.raises(TfError) as exc:
        state_inspect._validate_local_project(str(tmp_path))
    assert exc.value.failure_class == "project_dir_invalid"


def test_success_envelope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "main.tf").write_text("terraform {}\n")
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(_backend("local"))
    (tmp_path / "terraform.tfstate").write_text("{}")
    monkeypatch.setattr(state_inspect, "resolve_tool", lambda *_a: "terraform")

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args == ["state", "list"]:
            return _tf("aws_instance.a\n")
        if args[0] == "plan":
            assert "-lock-timeout=1s" in args
            assert "-refresh=false" in args
            assert "-input=false" in args
            assert "-lock=false" not in args
            return _tf("", "")
        if args == ["workspace", "list"]:
            return _tf("* default\n")
        raise AssertionError(args)

    monkeypatch.setattr(state_inspect, "run_tf", fake_run_tf)
    rc = state_inspect.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"]["state"] == "ok"
    assert env["data"]["resource_count"] == 1


def test_locked_state_summary_crit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "main.tf").write_text("terraform {}\n")
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(_backend("local"))
    monkeypatch.setattr(state_inspect, "resolve_tool", lambda *_a: "terraform")

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args == ["state", "list"]:
            return _tf("")
        if args[0] == "plan":
            return _tf("", "Error acquiring the state lock\nID: abc\nWho: alice\n")
        if args == ["workspace", "list"]:
            return _tf("* default\n")
        raise AssertionError(args)

    monkeypatch.setattr(state_inspect, "run_tf", fake_run_tf)
    rc = state_inspect.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"]["state"] == "crit"
    assert env["data"]["is_locked"] is True


def test_workspace_failure_soft(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "main.tf").write_text("terraform {}\n")
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(_backend("local"))
    monkeypatch.setattr(state_inspect, "resolve_tool", lambda *_a: "terraform")

    def fake_run_tf(_host: str, _project_dir: str, args: list[str], **_kwargs: Any) -> TfResult:
        if args == ["state", "list"]:
            return _tf("")
        if args[0] == "plan":
            return _tf("")
        if args == ["workspace", "list"]:
            raise TfError("workspace_list_failed", 127, "bad")
        raise AssertionError(args)

    monkeypatch.setattr(state_inspect, "run_tf", fake_run_tf)
    rc = state_inspect.main(["local", str(tmp_path), "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert env["data"]["summary"]["state"] == "warn"
    assert env["data"]["findings"][0]["reason"] == "workspace_list_failed"


@pytest.mark.parametrize(
    ("failure_class", "exit_code"),
    [
        ("host_unreachable", 120),
        ("tool_missing", 121),
        ("project_dir_invalid", 122),
        ("not_initialized", 123),
    ],
)
def test_hard_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure_class: str, exit_code: int) -> None:
    monkeypatch.setattr(state_inspect, "_validate_local_project", lambda *_a: None)
    monkeypatch.setattr(state_inspect, "resolve_tool", lambda *_a: (_ for _ in ()).throw(TfError(failure_class, exit_code, "bad")))
    rc = state_inspect.main(["local", "/tmp/x", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert rc == exit_code
    assert env["data"]["failure_class"] == failure_class
