"""Unit tests for ssh-guarded ``exec_detached.py`` v1.0."""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import exec_detached as _exec_mod  # noqa: E402
from lib import LockHeld, RequestArtifact, RiskSummary  # noqa: E402, I001

exec_detached = _exec_mod

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


PROJECT_ID = "test-proj"
REMOTE_PID = 4321


class _FakeCompletedProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeUuid:
    hex = "abcd1234ffff"


def _iso(seconds: int = 3600) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact(
    *,
    operation: str = "command",
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> RequestArtifact:
    if payload is None:
        payload = {
            "command": "echo hi",
            "timeout": 30,
            "workdir": f"~/workspace/{PROJECT_ID}/sub",
        }
    return RequestArtifact(
        version=1,
        request_id=request_id or f"{operation}-deadbeef",
        operation=operation,
        server="pai",
        reason="unit test",
        created_at=_iso(0),
        expires_at=_iso(3600),
        payload=payload,
        risk_summary=RiskSummary(
            irreversible=False,
            scope="single-file",
            blast_radius="unit test",
        ),
    )


def _save(tmp_path: Path, art: RequestArtifact) -> Path:
    path = tmp_path / f"{art.request_id}.json"
    art.save(path)
    return path


def _job_dict(**overrides: Any) -> dict[str, Any]:
    data = {
        "version": 1,
        "job_id": "abcd1234",
        "request_id": "command-deadbeef",
        "server": "pai",
        "remote_pid": REMOTE_PID,
        "remote_log_path": f"~/workspace/{PROJECT_ID}/sub/.exec-detached/abcd1234.log",
        "started_at": "2026-05-31T00:00:00Z",
        "command_excerpt": "echo hi",
        "cwd_on_remote": f"~/workspace/{PROJECT_ID}/sub",
    }
    data.update(overrides)
    return data


def _write_job(jobs_dir: Path, **overrides: Any) -> Path:
    data = _job_dict(**overrides)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / f"{data['job_id']}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _capture(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any] | None, str]:
    rc = exec_detached.main(argv)
    cap = capsys.readouterr()
    env = json.loads(cap.out) if cap.out.strip() else None
    return rc, env, cap.err


@pytest.fixture(autouse=True)
def _isolate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(exec_detached, "JOBS_DIR", tmp_path / "jobs")
    yield


@contextmanager
def _ok_lock(_path: Path) -> Iterator[None]:
    yield


def _ssh_env(stdout: str = "", stderr: str = "", exit_code: int = 0) -> dict[str, Any]:
    return {
        "success": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": {"failure_class": None if exit_code == 0 else "remote_error"},
    }


def test_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        exec_detached.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for sub in ("run", "status", "tail-log", "wait", "list"):
        assert sub in out
    for sub in ("run", "status", "tail-log", "wait", "list"):
        with pytest.raises(SystemExit) as sub_exc:
            exec_detached.main([sub, "--help"])
        assert sub_exc.value.code == 0


def test_run_rejects_non_command_operation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    art = _artifact(
        operation="mkdir",
        payload={"remote_path": f"~/workspace/{PROJECT_ID}/new", "mode": "0755", "parents": False},
    )
    path = _save(tmp_path, art)
    rc, env, _ = _capture(["run", "--request", str(path), "--json"], capsys)
    assert rc == 60
    assert env is not None
    assert env["data"]["failure_class"] == "request_not_executable"


def test_run_rejects_locked_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _save(tmp_path, _artifact())

    @contextmanager
    def held(_path: Path) -> Iterator[None]:
        raise LockHeld(999)
        yield

    monkeypatch.setattr(exec_detached, "exclusive_lock", held)
    rc, env, _ = _capture(["run", "--request", str(path), "--json"], capsys)
    assert rc == 60
    assert env is not None
    assert env["data"]["subclass"] == "lock_held"
    assert env["data"]["holding_pid"] == 999


def test_run_rejects_duplicate_executed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _save(tmp_path, _artifact())
    _write_job(exec_detached.JOBS_DIR, request_id="command-deadbeef", job_id="abcddddd")
    monkeypatch.setattr(exec_detached, "exclusive_lock", _ok_lock)
    rc, env, _ = _capture(["run", "--request", str(path), "--json"], capsys)
    assert rc == 60
    assert env is not None
    assert env["data"]["subclass"] == "request_already_executed"
    assert env["data"]["existing_job_id"] == "abcddddd"


def test_nohup_wrapper_template_escapes_single_quotes() -> None:
    cmd = exec_detached._build_nohup_command(
        "printf 'hello'",
        f"~/workspace/{PROJECT_ID}/sub",
        f"~/workspace/{PROJECT_ID}/sub/.exec-detached/abcd1234.log",
    )
    assert "nohup bash -c" in cmd
    assert "'\\''hello'\\''" in cmd
    assert "echo $!" in cmd
    assert exec_detached.EXIT_SENTINEL in cmd


def test_pid_parser_handles_trailing_blank_line() -> None:
    assert exec_detached._parse_remote_pid("noise\n4321\n\n") == REMOTE_PID


def test_pid_parser_rejects_non_integer() -> None:
    with pytest.raises(exec_detached.DetachedError) as exc_info:
        exec_detached._parse_remote_pid("not-pid\n")
    assert exc_info.value.exit_code == 62


def test_run_writes_job_state_atomically(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    art = _artifact(payload={"command": "echo hi", "timeout": 30, "workdir": f"~/workspace/{PROJECT_ID}/sub"})
    path = _save(tmp_path, art)
    monkeypatch.setattr(exec_detached, "exclusive_lock", _ok_lock)
    monkeypatch.setattr(exec_detached.uuid, "uuid4", _FakeUuid)
    monkeypatch.setattr(
        exec_detached,
        "_ssh_execute",
        lambda *_a, **_k: _ssh_env(stdout=f"{REMOTE_PID}\n"),
    )
    rc, env, _ = _capture(["run", "--request", str(path), "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert env["data"]["job_id"] == "abcd1234"
    job_path = exec_detached.JOBS_DIR / "abcd1234.json"
    assert job_path.exists()
    job = json.loads(job_path.read_text())
    assert job["remote_pid"] == REMOTE_PID
    assert job["request_id"] == art.request_id
    assert not list(exec_detached.JOBS_DIR.glob("*.tmp"))


def test_run_command_excerpt_truncates_at_80() -> None:
    out = exec_detached._command_excerpt("x" * 90)
    assert len(out) == 83
    assert out.endswith("...")


def test_status_alive_returns_running(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)
    monkeypatch.setattr(exec_detached, "_ssh_execute", lambda *_a, **_k: _ssh_env(stdout="alive\n"))
    rc, env, _ = _capture(["status", "abcd1234", "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert env["data"]["state"] == "running"


def test_status_dead_with_sentinel_returns_exited(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)

    def fake(_server: str, command: str, _timeout: int) -> dict[str, Any]:
        if "kill -0" in command:
            return _ssh_env(stdout="dead\n")
        return _ssh_env(stdout="done\n__EXEC_DETACHED_EXIT__=7\n")

    monkeypatch.setattr(exec_detached, "_ssh_execute", fake)
    rc, env, _ = _capture(["status", "abcd1234", "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert env["data"]["state"] == "exited"
    assert env["data"]["exit_code"] == 7


def test_status_dead_no_sentinel_returns_vanished(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)
    monkeypatch.setattr(exec_detached, "_ssh_execute", lambda *_a, **_k: _ssh_env(stdout="dead\n"))
    rc, env, _ = _capture(["status", "abcd1234", "--json"], capsys)
    assert rc == 64
    assert env is not None
    assert env["data"]["failure_class"] == "job_vanished"


def test_status_corrupt_job_file_returns_65(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exec_detached.JOBS_DIR.mkdir(parents=True)
    (exec_detached.JOBS_DIR / "abcd1234.json").write_text("{not json")
    rc, env, _ = _capture(["status", "abcd1234", "--json"], capsys)
    assert rc == 65
    assert env is not None
    assert env["data"]["failure_class"] == "job_state_corrupt"


def test_status_log_unreachable_returns_63(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)

    def fake(_server: str, command: str, _timeout: int) -> dict[str, Any]:
        if "kill -0" in command:
            return _ssh_env(stdout="dead\n")
        return _ssh_env(stderr="missing", exit_code=1)

    monkeypatch.setattr(exec_detached, "_ssh_execute", fake)
    rc, env, _ = _capture(["status", "abcd1234", "--json"], capsys)
    assert rc == 63
    assert env is not None
    assert env["data"]["failure_class"] == "remote_log_unreachable"


def test_tail_log_redacts_by_default(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)
    token = "AKIA" + "A" * 16
    monkeypatch.setattr(exec_detached, "_ssh_execute", lambda *_a, **_k: _ssh_env(stdout=f"{token}\nplain\n"))
    rc, env, _ = _capture(["tail-log", "abcd1234", "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert env["data"]["lines"][0] == "[REDACTED-AWS-ACCESS-KEY]"


def test_tail_log_no_redact_passes_through(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)
    token = "AKIA" + "A" * 16
    monkeypatch.setattr(exec_detached, "_ssh_execute", lambda *_a, **_k: _ssh_env(stdout=f"{token}\n"))
    rc, env, _ = _capture(["tail-log", "abcd1234", "--no-redact", "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert env["data"]["lines"] == [token]


def test_wait_returns_124_on_timeout(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR)
    monkeypatch.setattr(
        exec_detached,
        "_status_payload",
        lambda _job: {"state": "running", "remote_pid": REMOTE_PID, "failure_class": None},
    )
    rc, env, _ = _capture(["wait", "abcd1234", "--timeout", "0", "--json"], capsys)
    assert rc == 124
    assert env is not None
    assert env["data"]["state"] == "running"
    assert env["data"]["failure_class"] is None


def test_wait_intervals_schedule_matches_design() -> None:
    intervals = exec_detached._wait_intervals()
    assert intervals[:30] == [1] * 30
    assert intervals[30:36] == [5] * 6
    assert 15 in intervals[90:]
    assert max(intervals) <= 15


def test_list_offline_scan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_job(exec_detached.JOBS_DIR, job_id="bbbb2222")
    (exec_detached.JOBS_DIR / "bad.json").write_text("{not json")
    rc, env, _ = _capture(["list", "--json"], capsys)
    assert rc == 0
    assert env is not None
    states = {row["job_id"]: row["state"] for row in env["data"]["jobs"]}
    assert states["bbbb2222"] == "unknown"
    assert states["bad"] == "corrupt"


def test_list_probe_calls_status_per_job(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_job(exec_detached.JOBS_DIR, job_id="bbbb2222")
    called: list[str] = []

    def fake(job: exec_detached.JobState) -> dict[str, Any]:
        called.append(job.job_id)
        return {"state": "exited", "exit_code": 0, "failure_class": None}

    monkeypatch.setattr(exec_detached, "_status_payload", fake)
    rc, env, _ = _capture(["list", "--probe", "--json"], capsys)
    assert rc == 0
    assert env is not None
    assert called == ["bbbb2222"]
    assert env["data"]["jobs"][0]["state"] == "exited"


def test_ssh_execute_non_json_maps_to_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exec_detached.subprocess,
        "run",
        lambda *_a, **_k: _FakeCompletedProcess(stdout="not json", stderr="boom", returncode=2),
    )
    env = exec_detached._ssh_execute("pai", "true", 1)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "remote_error"
