"""Unit tests for ssh-guarded ``run_request.py`` executor (≥18 per D12).

Covers (design.md §3 D12 row "run_request.py"):
- help exits 0 + all v1.0 surface flags
- schema bad → 53
- expired → 53 (failure_class=artifact_expired distinct from invalid)
- server mismatch → 53 + data.reason=server_mismatch
- sandbox refusal → 50
- denylist refusal → 51
- sha256 mismatch → 56 + envelope expected/actual fields
- lock held → 54 + envelope holder_pid
- delete needs --confirm-delete → 59
- happy dispatch command (mocked subprocess.run)
- happy dispatch upload (mocked subprocess.run)
- happy dispatch mkdir (one-liner shape check)
- happy dispatch delete (one-liner shape check)
- transport failed → 57 (non-JSON child stdout)
- transport timeout → 124
- result.json schema check
- redaction applied to stdout (default catalog, AKIA prefix)
- --no-redact bypass populates transport_argv in result.json
- --show-sensitive does NOT change result.json content
- validate-only happy path returns 0 with data.validate_only=True
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)

# Evict-restore the ``lib`` package (ssh-core's lib has the same name).
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import run_request as _rr_mod  # noqa: E402
from lib import (  # noqa: E402, I001
    ArtifactError,
    LockHeld,
    RequestArtifact,
    RiskSummary,
)

run_request = _rr_mod
# Pin the original function reference BEFORE the autouse stub installs a
# lambda; tests that exercise the real resolver restore this object.
_ORIGINAL_RESOLVE_REMOTE_HOME = run_request._resolve_remote_home

# Restore other modules' ``lib`` namespace.
for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


# --- Helpers ---------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_artifact(
    *,
    operation: str = "command",
    server: str = "pai",
    payload: dict[str, Any] | None = None,
    risk: RiskSummary | None = None,
    request_id: str | None = None,
    expires_in_seconds: int = 3600,
    created_offset_seconds: int = 0,
) -> RequestArtifact:
    """Build a stand-alone RequestArtifact instance (no disk write here)."""
    now = datetime.now(timezone.utc) + timedelta(seconds=created_offset_seconds)
    expires = now + timedelta(seconds=expires_in_seconds)
    if payload is None:
        payload = {"command": "echo hi", "timeout": 60}
    if risk is None:
        risk = RiskSummary(
            irreversible=False, scope="single-file", blast_radius="test",
        )
    return RequestArtifact(
        version=1,
        request_id=request_id or f"{operation}-deadbeef",
        operation=operation,
        server=server,
        reason="unit test",
        created_at=_iso(now),
        expires_at=_iso(expires),
        payload=payload,
        risk_summary=risk,
    )


def _save_artifact(tmp_path: Path, art: RequestArtifact) -> Path:
    """Save artifact to ``tmp_path / <request_id>.json`` and return path."""
    art_path = tmp_path / f"{art.request_id}.json"
    art.save(art_path)
    return art_path


def _capture(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any] | None, str]:
    rc = run_request.main(argv)
    cap = capsys.readouterr()
    env: dict[str, Any] | None
    try:
        env = json.loads(cap.out) if cap.out.strip() else None
    except json.JSONDecodeError:
        env = None
    return rc, env, cap.err


def _fake_child_envelope(
    *, success: bool = True, exit_code: int = 0,
    stdout: str = "", stderr: str = "",
) -> dict[str, Any]:
    """Build an ssh-core-shaped envelope as a Python dict."""
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": {"route": "remote", "failure_class": None},
    }


class _FakeCompletedProcess:
    """Stand-in for ``subprocess.CompletedProcess`` returned by ``subprocess.run``."""

    def __init__(
        self,
        *,
        stdout: str,
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# --- Fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stable_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a known project_id so sandbox checks are deterministic."""
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "test-proj")


@pytest.fixture
def _safe_payload_command() -> dict[str, Any]:
    """Command payload that lives inside the sandbox."""
    return {
        "command": "echo hi",
        "timeout": 30,
        "workdir": "~/workspace/test-proj/sub",
    }


@pytest.fixture
def _safe_payload_mkdir() -> dict[str, Any]:
    return {
        "remote_path": "~/workspace/test-proj/staging",
        "mode": "0755",
        "parents": False,
    }


@pytest.fixture
def _safe_payload_delete() -> dict[str, Any]:
    return {
        "remote_path": "~/workspace/test-proj/stale.log",
        "recursive": False,
        "is_directory_pre_check": False,
    }


@pytest.fixture
def _delete_risk() -> RiskSummary:
    return RiskSummary(
        irreversible=True, scope="single-file",
        blast_radius="deletes ~/workspace/test-proj/stale.log; recursive=False",
    )


# --- Tests -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_remote_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-mock ``_resolve_remote_home`` so unit tests never ssh.

    Individual tests that exercise the resolver itself can override via
    ``monkeypatch.setattr(run_request, "_resolve_remote_home", ...)`` —
    the later patch wins.
    """
    monkeypatch.setattr(
        run_request, "_resolve_remote_home", lambda *_a, **_k: "/root",
    )


def test_help_exits_zero_and_lists_v10_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        run_request.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in (
        "--request", "--execute", "--confirm-delete", "--server",
        "--show-sensitive", "--json", "--project-id", "--no-redact",
    ):
        assert flag in out, f"missing v1.0 flag {flag}"
    # --timeout-execute is deferred to v1.1; must NOT appear.
    assert "--timeout-execute" not in out


def test_schema_invalid_exits_53(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rc, env, _ = _capture(["--request", str(bad), "--json"], capsys)
    assert rc == 53
    assert env is not None
    assert env["error"]["failure_class"] == "artifact_invalid"


def test_expired_artifact_exits_53(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    _safe_payload_command: dict[str, Any],
) -> None:
    # created_offset -7200s so expires_at lands 3600s in the past.
    art = _build_artifact(
        operation="command",
        payload=_safe_payload_command,
        created_offset_seconds=-7200,
        expires_in_seconds=3600,
    )
    art_path = _save_artifact(tmp_path, art)
    rc, env, _ = _capture(["--request", str(art_path), "--json"], capsys)
    assert rc == 53
    assert env is not None
    assert env["error"]["failure_class"] == "artifact_expired"


def test_server_mismatch_exits_53_with_data_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command,
                          server="pai")
    art_path = _save_artifact(tmp_path, art)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--server", "other-alias", "--json"],
        capsys,
    )
    assert rc == 53
    assert env is not None
    assert env["error"]["failure_class"] == "artifact_invalid"
    assert env["data"]["reason"] == "server_mismatch"
    assert env["data"]["expected_server"] == "other-alias"
    assert env["data"]["artifact_server"] == "pai"


def test_sandbox_refusal_exits_50(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "command": "echo x", "timeout": 30, "workdir": "/etc",
    }
    art = _build_artifact(operation="command", payload=payload)
    art_path = _save_artifact(tmp_path, art)
    rc, env, _ = _capture(["--request", str(art_path), "--json"], capsys)
    assert rc == 50
    assert env is not None
    assert env["error"]["failure_class"] == "workdir_outside_sandbox"


def test_denylist_refusal_exits_51(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "remote_path": "~/workspace/test-proj/.ssh/authorized_keys",
        "recursive": False,
        "is_directory_pre_check": False,
    }
    risk = RiskSummary(
        irreversible=True, scope="single-file", blast_radius="x",
    )
    art = _build_artifact(operation="delete", payload=payload, risk=risk)
    art_path = _save_artifact(tmp_path, art)
    # --confirm-delete present so we hit the denylist check, not the
    # delete-confirm gate first.
    rc, env, _ = _capture(
        ["--request", str(art_path), "--confirm-delete", "--json"], capsys,
    )
    assert rc == 51
    assert env is not None
    assert env["error"]["failure_class"] == "path_in_denylist"


def test_sha256_mismatch_exits_56_with_envelope_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Write a real local file, build an artifact with a WRONG sha256, and
    # verify the executor flags drift.
    local = tmp_path / "release.tar.gz"
    local.write_bytes(b"hello world")
    wrong_sha = "0" * 64
    actual_sha = hashlib.sha256(b"hello world").hexdigest()
    payload = {
        "local_path": str(local),
        "local_sha256": wrong_sha,
        "local_size": 11,
        "remote_path": "~/workspace/test-proj/release.tar.gz",
        "overwrite": False,
        "sensitive_local": False,
    }
    art = _build_artifact(operation="upload", payload=payload)
    art_path = _save_artifact(tmp_path, art)
    rc, env, _ = _capture(["--request", str(art_path), "--json"], capsys)
    assert rc == 56
    assert env is not None
    assert env["error"]["failure_class"] == "sha256_mismatch"
    assert env["data"]["expected_sha256"] == wrong_sha
    assert env["data"]["actual_sha256"] == actual_sha


def test_lock_held_exits_54_with_holder_pid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)

    @contextlib.contextmanager
    def fake_lock(_p: Path) -> Any:
        raise LockHeld(holder_pid=12345)
        yield  # pragma: no cover

    monkeypatch.setattr(run_request, "exclusive_lock", fake_lock)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 54
    assert env is not None
    assert env["error"]["failure_class"] == "lock_held"
    assert env["data"]["holder_pid"] == 12345


def test_delete_irreversible_without_confirm_exits_59(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    _safe_payload_delete: dict[str, Any], _delete_risk: RiskSummary,
) -> None:
    art = _build_artifact(
        operation="delete", payload=_safe_payload_delete, risk=_delete_risk,
    )
    art_path = _save_artifact(tmp_path, art)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 59
    assert env is not None
    assert env["error"]["failure_class"] == "delete_confirm_required"


def test_happy_dispatch_command_mocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)

    fake_env = _fake_child_envelope(stdout="hello\n", stderr="")
    captured_argv: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured_argv.append(argv)
        return _FakeCompletedProcess(stdout=json.dumps(fake_env))

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 0
    assert env is not None
    assert env["success"] is True
    assert env["data"]["transport"] == "ssh_execute"
    assert env["data"]["exit_code"] == 0
    assert env["data"]["result_path"].endswith(".result.json")
    # argv shape: python <ssh_execute.py> server "echo hi" --timeout 30 --json
    args = captured_argv[0]
    assert args[2] == "pai"
    assert args[3] == "echo hi"
    assert "--timeout" in args
    assert "--json" in args


def test_happy_dispatch_upload_mocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "release.tar.gz"
    local.write_bytes(b"hello world")
    sha = hashlib.sha256(b"hello world").hexdigest()
    payload = {
        "local_path": str(local),
        "local_sha256": sha,
        "local_size": 11,
        "remote_path": "~/workspace/test-proj/release.tar.gz",
        "overwrite": True,
        "sensitive_local": False,
    }
    risk = RiskSummary(
        irreversible=True, scope="single-file", blast_radius="upload",
    )
    art = _build_artifact(operation="upload", payload=payload, risk=risk)
    art_path = _save_artifact(tmp_path, art)

    fake_env = _fake_child_envelope(stdout="ok\n")
    captured_argv: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured_argv.append(argv)
        return _FakeCompletedProcess(stdout=json.dumps(fake_env))

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 0
    assert env is not None
    assert env["data"]["transport"] == "ssh_upload"
    args = captured_argv[0]
    # python ssh_upload.py pai <local> <remote> --overwrite --json
    assert "ssh_upload.py" in args[1]
    assert "--overwrite" in args
    assert "--json" in args


def test_happy_dispatch_mkdir_oneliner_quotes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_mkdir: dict[str, Any],
) -> None:
    payload = dict(_safe_payload_mkdir)
    payload["parents"] = True
    art = _build_artifact(operation="mkdir", payload=payload)
    art_path = _save_artifact(tmp_path, art)

    fake_env = _fake_child_envelope()
    captured_argv: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured_argv.append(argv)
        return _FakeCompletedProcess(stdout=json.dumps(fake_env))

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    rc, _env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 0
    # one-liner: argv[3] = "mkdir -p -m 0755 -- '/root/workspace/test-proj/staging'"
    # _resolve_remote_home is stubbed to '/root' by the autouse fixture, so
    # the payload's literal ~/ gets materialised before quoting.
    cmd = captured_argv[0][3]
    assert cmd.startswith("mkdir -p -m 0755 -- ")
    assert "/root/workspace/test-proj/staging" in cmd
    assert "~/" not in cmd, "transport string must not contain literal ~"


def test_happy_dispatch_delete_oneliner_recursive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "remote_path": "~/workspace/test-proj/staging",
        "recursive": True,
        "is_directory_pre_check": True,
    }
    risk = RiskSummary(
        irreversible=True, scope="tree",
        blast_radius="deletes ~/workspace/test-proj/staging; recursive=True",
    )
    art = _build_artifact(operation="delete", payload=payload, risk=risk)
    art_path = _save_artifact(tmp_path, art)

    fake_env = _fake_child_envelope()
    captured_argv: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured_argv.append(argv)
        return _FakeCompletedProcess(stdout=json.dumps(fake_env))

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    rc, _env, _ = _capture(
        ["--request", str(art_path), "--execute", "--confirm-delete", "--json"],
        capsys,
    )
    assert rc == 0
    cmd = captured_argv[0][3]
    assert cmd.startswith("rm -rf -- ")
    assert "/root/workspace/test-proj/staging" in cmd
    assert "~/" not in cmd


def test_transport_failed_non_json_exits_57(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(
            stdout="not-json", stderr="ssh: connect failure", returncode=1,
        )

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 57
    assert env is not None
    assert env["error"]["failure_class"] == "transport_failed"
    assert env["data"]["transport_returncode"] == 1
    assert "ssh: connect failure" in env["data"]["transport_stderr"]


def test_transport_timeout_exits_124(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 124
    assert env is not None
    assert env["error"]["failure_class"] == "timeout"


def test_result_json_schema_after_happy_dispatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)
    fake_env = _fake_child_envelope(stdout="ok\n", stderr="some warn\n")
    monkeypatch.setattr(
        run_request.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(stdout=json.dumps(fake_env)),
    )
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 0
    assert env is not None
    result_path = Path(env["data"]["result_path"])
    assert result_path.exists()
    body = json.loads(result_path.read_text())
    assert body["version"] == 1
    assert body["request_id"] == art.request_id
    assert body["exit_code"] == 0
    assert body["transport"] == "ssh_execute"
    assert body["transport_exit_code"] == 0
    assert body["stdout_redacted"] == "ok\n"
    assert body["stderr_redacted"] == "some warn\n"
    assert "duration_ms" in body
    assert "executed_at" in body
    # Default mode (redacted): transport_argv is None.
    assert body["transport_argv"] is None


def test_redaction_applied_to_result_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    # Default catalog includes AKIA → [REDACTED-AWS-ACCESS-KEY].
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)
    secret = "AKIA" + "Z" * 16
    fake_env = _fake_child_envelope(stdout=f"key={secret}\n")
    monkeypatch.setattr(
        run_request.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(stdout=json.dumps(fake_env)),
    )
    # Force the redactor catalog away from the real ~/.devops path so the
    # default _DEFAULT_PATTERNS catalog is used (Phase A Redactor handles
    # missing catalog → defaults).
    monkeypatch.setattr(
        run_request, "DEFAULT_REDACT_CATALOG", tmp_path / "no-such.json",
    )
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 0
    body = json.loads(Path(env["data"]["result_path"]).read_text())  # type: ignore[index]
    assert secret not in body["stdout_redacted"]
    assert "[REDACTED-AWS-ACCESS-KEY]" in body["stdout_redacted"]


def test_no_redact_bypass_includes_transport_argv(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)
    secret = "AKIA" + "Z" * 16
    fake_env = _fake_child_envelope(stdout=f"key={secret}\n")
    monkeypatch.setattr(
        run_request.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(stdout=json.dumps(fake_env)),
    )
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--no-redact", "--json"],
        capsys,
    )
    assert rc == 0
    body = json.loads(Path(env["data"]["result_path"]).read_text())  # type: ignore[index]
    # No-redact: stdout unchanged, transport_argv populated.
    assert secret in body["stdout_redacted"]
    assert isinstance(body["transport_argv"], list)
    assert "ssh_execute.py" in body["transport_argv"][1]


def test_show_sensitive_does_not_change_result_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)
    secret = "AKIA" + "Z" * 16
    fake_env = _fake_child_envelope(stdout=f"key={secret}\n")
    monkeypatch.setattr(
        run_request.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(stdout=json.dumps(fake_env)),
    )
    monkeypatch.setattr(
        run_request, "DEFAULT_REDACT_CATALOG", tmp_path / "no-such.json",
    )
    rc, env, _ = _capture(
        ["--request", str(art_path), "--execute", "--show-sensitive", "--json"],
        capsys,
    )
    assert rc == 0
    body = json.loads(Path(env["data"]["result_path"]).read_text())  # type: ignore[index]
    # --show-sensitive only affects printing — result.json is still redacted.
    assert secret not in body["stdout_redacted"]
    assert "[REDACTED-AWS-ACCESS-KEY]" in body["stdout_redacted"]
    # And transport_argv stays None (controlled by --no-redact, not this flag).
    assert body["transport_argv"] is None


def test_validate_only_happy_path_no_lock_no_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_command: dict[str, Any],
) -> None:
    art = _build_artifact(operation="command", payload=_safe_payload_command)
    art_path = _save_artifact(tmp_path, art)

    def _refuse_lock(*a: Any, **kw: Any) -> Any:
        raise AssertionError("validate-only must not acquire the lock")

    def _refuse_run(*a: Any, **kw: Any) -> Any:
        raise AssertionError("validate-only must not call subprocess.run")

    monkeypatch.setattr(run_request, "exclusive_lock", _refuse_lock)
    monkeypatch.setattr(run_request.subprocess, "run", _refuse_run)

    rc, env, _ = _capture(["--request", str(art_path), "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert env["success"] is True
    assert env["data"]["validate_only"] is True
    assert env["data"]["request_id"] == art.request_id
    assert env["data"]["operation"] == "command"
    # No result file written.
    assert not (tmp_path / f"{art.request_id}.result.json").exists()


def test_human_mode_failure_writes_stderr(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-JSON mode failure path writes failure_class line to stderr."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rc = run_request.main(["--request", str(bad)])
    cap = capsys.readouterr()
    assert rc == 53
    assert "artifact_invalid" in cap.err


# --- Remote $HOME resolution (B1+B2 fix) ----------------------------------


class _TildeCase:
    """Container for ``_expand_remote_tilde`` parameterized inputs."""


@pytest.mark.parametrize(
    ("path_in", "remote_home", "expected"),
    [
        ("~/foo", "/root", "/root/foo"),
        ("~/workspace/p/x", "/home/jason", "/home/jason/workspace/p/x"),
        ("~", "/root", "/root"),
        ("~/", "/root", "/root"),
        ("/abs/path", "/root", "/abs/path"),       # absolute, untouched
        ("relative/x", "/root", "relative/x"),     # relative, untouched
        ("~user/foo", "/root", "~user/foo"),       # other-user form, untouched
    ],
)
def test_expand_remote_tilde_table(
    path_in: str, remote_home: str, expected: str,
) -> None:
    assert run_request._expand_remote_tilde(path_in, remote_home) == expected


def test_build_argv_upload_expands_tilde_in_remote_path() -> None:
    art = _build_artifact(
        operation="upload",
        payload={
            "local_path": "/tmp/x.bin",
            "remote_path": "~/workspace/test-proj/x.bin",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "overwrite": False,
        },
    )
    argv = run_request._build_argv(art, remote_home="/root")
    # argv shape: [py, ssh_upload_path, server, local, remote, "--json"]
    assert argv[3] == "/tmp/x.bin"
    assert argv[4] == "/root/workspace/test-proj/x.bin"


def test_build_argv_mkdir_expands_tilde_in_shell_string() -> None:
    art = _build_artifact(
        operation="mkdir",
        payload={
            "remote_path": "~/workspace/test-proj/staging",
            "mode": "0755",
            "parents": True,
        },
    )
    argv = run_request._build_argv(art, remote_home="/root")
    # argv[3] = "mkdir -p -m 0755 -- '/root/workspace/test-proj/staging'"
    assert "/root/workspace/test-proj/staging" in argv[3]
    assert "~/" not in argv[3]


def test_build_argv_without_remote_home_leaves_tilde_literal() -> None:
    """Backwards-safe fallback path: no remote_home → no expansion."""
    art = _build_artifact(
        operation="delete",
        payload={"remote_path": "~/workspace/test-proj/x", "recursive": False},
        risk=RiskSummary(
            irreversible=True, scope="single-file",
            blast_radius="deletes ~/workspace/test-proj/x; recursive=False",
        ),
    )
    argv = run_request._build_argv(art)  # no remote_home
    assert "~/workspace/test-proj/x" in argv[3]


def test_resolve_remote_home_returns_stripped_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        # Verify we call ssh with the expected argv shape.
        assert argv[0] == "ssh"
        assert "BatchMode=yes" in argv
        assert argv[-2] == "pai"
        assert "$HOME" in argv[-1]
        return _FakeCompletedProcess(stdout="/home/jason\n", returncode=0)

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)
    # Bypass the autouse stub for this case to exercise the real function.
    monkeypatch.setattr(
        run_request, "_resolve_remote_home",
        _ORIGINAL_RESOLVE_REMOTE_HOME,
    )
    assert run_request._resolve_remote_home("pai") == "/home/jason"


def test_resolve_remote_home_ssh_failure_raises_57(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_request.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(
            stdout="", stderr="Permission denied", returncode=255,
        ),
    )
    monkeypatch.setattr(
        run_request, "_resolve_remote_home",
        _ORIGINAL_RESOLVE_REMOTE_HOME,
    )
    with pytest.raises(ArtifactError) as ei:
        run_request._resolve_remote_home("pai")
    assert ei.value.exit_code == 57
    assert ei.value.failure_class == "transport_failed"


def test_resolve_remote_home_non_absolute_raises_57(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_request.subprocess, "run",
        lambda *a, **k: _FakeCompletedProcess(stdout="not-a-path", returncode=0),
    )
    monkeypatch.setattr(
        run_request, "_resolve_remote_home",
        _ORIGINAL_RESOLVE_REMOTE_HOME,
    )
    with pytest.raises(ArtifactError) as ei:
        run_request._resolve_remote_home("pai")
    assert ei.value.exit_code == 57
    assert "non-absolute" in ei.value.message


def test_resolve_remote_home_timeout_raises_57(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_timeout(*a: Any, **k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=10)

    monkeypatch.setattr(run_request.subprocess, "run", _raise_timeout)
    monkeypatch.setattr(
        run_request, "_resolve_remote_home",
        _ORIGINAL_RESOLVE_REMOTE_HOME,
    )
    with pytest.raises(ArtifactError) as ei:
        run_request._resolve_remote_home("pai", timeout_seconds=10)
    assert ei.value.exit_code == 57
    assert "timed out" in ei.value.message


def test_execute_resolves_remote_home_and_expands_payload_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    _safe_payload_mkdir: dict[str, Any],
) -> None:
    """End-to-end: --execute must materialise ~/ via _resolve_remote_home."""
    art = _build_artifact(operation="mkdir", payload=_safe_payload_mkdir)
    art_path = _save_artifact(tmp_path, art)

    # Override autouse stub with a marker value to confirm the wiring.
    monkeypatch.setattr(
        run_request, "_resolve_remote_home",
        lambda *_a, **_k: "/home/jason",
    )

    fake_env = _fake_child_envelope()
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured.append(argv)
        return _FakeCompletedProcess(stdout=json.dumps(fake_env))

    monkeypatch.setattr(run_request.subprocess, "run", fake_run)

    rc, _env, _ = _capture(
        ["--request", str(art_path), "--execute", "--json"], capsys,
    )
    assert rc == 0
    cmd = captured[0][3]
    assert "/home/jason/workspace/test-proj/staging" in cmd
    assert "~/" not in cmd
