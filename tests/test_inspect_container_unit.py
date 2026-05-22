"""Unit tests for docker-quick/scripts/inspect_container.py v1.0.

The script is exercised through its in-process API (``_extract_inspect``,
``_score_state``, ``_score_config``, ``_score_logs``, ``_run_local``,
``_run_via_ssh_execute``, ``_inspect_one``, ``_logs_one``, ``main``) with
``subprocess.run`` monkey-patched. The integration counterpart lives in
``tests/test_inspect_container_integration.py`` (marker: ``live_ssh``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "docker-quick" / "skills" / "docker-quick" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import inspect_container as ic  # noqa: E402, I001

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope_json(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: dict | None = None,
) -> str:
    """JSON string ssh_execute would print on --json."""
    return json.dumps({
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    })


def _inspect_blob(
    *,
    status: str = "running",
    running: bool = True,
    restarting: bool = False,
    oom_killed: bool = False,
    exit_code: int = 0,
    restart_count: int = 0,
    health_status: str = "",
    health_log: list | None = None,
    user: str = "1000:1000",
    image: str = "myorg/api:1.0.0",
    name: str = "/my-ctr",
    extra_mounts: list | None = None,
) -> dict:
    """Construct a docker inspect-shaped dict for tests."""
    blob = {
        "Id": "abc123",
        "Name": name,
        "RestartCount": restart_count,
        "Image": image,
        "State": {
            "Status": status,
            "Running": running,
            "Restarting": restarting,
            "OOMKilled": oom_killed,
            "ExitCode": exit_code,
            "Error": "",
            "StartedAt": "2026-05-22T08:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
        },
        "Config": {
            "User": user,
            "Cmd": ["python", "app.py"],
            "Entrypoint": None,
            "Env": ["LANG=C", "PATH=/usr/bin"],
            "Image": image,
        },
        "Mounts": extra_mounts or [],
    }
    if health_status:
        blob["State"]["Health"] = {
            "Status": health_status,
            "FailingStreak": 1 if health_status != "healthy" else 0,
            "Log": health_log or [],
        }
    return blob


# -----------------------------------------------------------------------------
# _extract_inspect
# -----------------------------------------------------------------------------


def test_extract_inspect_happy() -> None:
    blob = _inspect_blob()
    out = ic._extract_inspect(blob)
    assert out["id"] == "abc123"
    # Leading slash stripped from name.
    assert out["name"] == "my-ctr"
    assert out["state"]["status"] == "running"
    assert out["state"]["running"] is True
    assert out["state"]["exit_code"] == 0
    assert out["config"]["user"] == "1000:1000"
    expected_env_count = 2
    assert out["config"]["env_count"] == expected_env_count
    assert out["mounts"] == []


def test_extract_inspect_missing_state_collapses_to_defaults() -> None:
    out = ic._extract_inspect({"Id": "x", "Name": "y"})
    assert out["state"]["status"] == ""
    assert out["state"]["running"] is False
    assert out["state"]["exit_code"] == 0
    assert out["health"]["status"] == ""
    assert out["health"]["log"] == []


def test_extract_inspect_with_health() -> None:
    blob = _inspect_blob(
        health_status="unhealthy",
        health_log=[
            {"Output": "ping failed: timeout", "ExitCode": 1},
            {"Output": "ping failed: refused", "ExitCode": 1},
        ],
    )
    out = ic._extract_inspect(blob)
    assert out["health"]["status"] == "unhealthy"
    expected_log_entries = 2
    assert len(out["health"]["log"]) == expected_log_entries


def test_extract_inspect_with_mounts() -> None:
    blob = _inspect_blob(extra_mounts=[
        {"Source": "/host/data", "Destination": "/data", "Mode": "rw"},
    ])
    out = ic._extract_inspect(blob)
    assert len(out["mounts"]) == 1
    assert out["mounts"][0]["source"] == "/host/data"
    assert out["mounts"][0]["mode"] == "rw"


# -----------------------------------------------------------------------------
# _score_state
# -----------------------------------------------------------------------------


def test_score_state_running_clean_is_ok() -> None:
    inspect = ic._extract_inspect(_inspect_blob())
    sev, findings = ic._score_state(inspect)
    assert sev == "ok"
    assert findings == []


def test_score_state_oomkilled_is_crit() -> None:
    inspect = ic._extract_inspect(
        _inspect_blob(status="exited", running=False, oom_killed=True, exit_code=137)
    )
    sev, findings = ic._score_state(inspect)
    assert sev == "crit"
    kinds = {f["kind"] for f in findings}
    assert "oomkilled" in kinds
    assert "exit_code" in kinds


def test_score_state_exit137_alone_is_crit() -> None:
    inspect = ic._extract_inspect(
        _inspect_blob(status="exited", running=False, exit_code=137)
    )
    sev, findings = ic._score_state(inspect)
    assert sev == "crit"
    assert any(f["kind"] == "exit_code" and f["value"] == 137 for f in findings)  # noqa: PLR2004


def test_score_state_exit139_is_crit() -> None:
    inspect = ic._extract_inspect(
        _inspect_blob(status="exited", running=False, exit_code=139)
    )
    sev, _ = ic._score_state(inspect)
    assert sev == "crit"


def test_score_state_unhealthy_is_crit() -> None:
    inspect = ic._extract_inspect(_inspect_blob(
        health_status="unhealthy",
        health_log=[{"Output": "probe failed: connection refused"}],
    ))
    sev, findings = ic._score_state(inspect)
    assert sev == "crit"
    health_finding = next(f for f in findings if f["kind"] == "health_unhealthy")
    assert "connection refused" in health_finding["value"]


def test_score_state_restarting_is_warn() -> None:
    inspect = ic._extract_inspect(_inspect_blob(status="restarting"))
    sev, findings = ic._score_state(inspect)
    assert sev == "warn"
    assert any(f["kind"] == "restarting" for f in findings)


def test_score_state_restart_count_over_threshold_is_warn() -> None:
    inspect = ic._extract_inspect(_inspect_blob(restart_count=10))
    sev, findings = ic._score_state(inspect)
    assert sev == "warn"
    rc_finding = next(f for f in findings if f["kind"] == "restart_count")
    expected_count = 10
    assert rc_finding["value"] == expected_count


def test_score_state_restart_count_at_threshold_is_ok() -> None:
    # threshold is strict (> 5), so exactly 5 stays ok.
    inspect = ic._extract_inspect(_inspect_blob(restart_count=5))
    sev, _ = ic._score_state(inspect)
    assert sev == "ok"


def test_score_state_exited_nonzero_non_crit_is_warn() -> None:
    inspect = ic._extract_inspect(_inspect_blob(
        status="exited", running=False, exit_code=1
    ))
    sev, findings = ic._score_state(inspect)
    assert sev == "warn"
    assert any(f["kind"] == "exit_code" and f["value"] == 1 for f in findings)


def test_score_state_exited_zero_is_ok() -> None:
    inspect = ic._extract_inspect(_inspect_blob(
        status="exited", running=False, exit_code=0
    ))
    sev, findings = ic._score_state(inspect)
    assert sev == "ok"
    assert findings == []


def test_score_state_crit_short_circuits_warn() -> None:
    """OOMKilled + restart_count=10 -> only crit findings, no warn-tier dup."""
    inspect = ic._extract_inspect(_inspect_blob(
        status="exited", running=False, oom_killed=True, exit_code=137,
        restart_count=10,
    ))
    sev, findings = ic._score_state(inspect)
    assert sev == "crit"
    # restart_count finding should NOT be emitted in the crit branch.
    assert all(f["kind"] != "restart_count" for f in findings)


# -----------------------------------------------------------------------------
# _score_config
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("user", ["", "root", "0", "0:0"])
def test_score_config_root_emits_info(user: str) -> None:
    inspect = ic._extract_inspect(_inspect_blob(user=user))
    sev, findings = ic._score_config(inspect)
    # Config always ok in v1.0.
    assert sev == "ok"
    assert any(f["kind"] == "running_as_root" for f in findings)
    assert all(f["severity"] == "info" for f in findings)


def test_score_config_non_root_no_findings() -> None:
    inspect = ic._extract_inspect(_inspect_blob(user="1000:1000"))
    sev, findings = ic._score_config(inspect)
    assert sev == "ok"
    assert findings == []


# -----------------------------------------------------------------------------
# _score_logs
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("text,kw", [
    ("nothing wrong here\n", None),
    ("", None),
    ("got a panic at line 42\n", "panic"),
    ("FATAL: out of memory\n", "fatal"),
    ("OOM-killer summoned\n", "OOM"),
    ("OutOfMemoryError caught\n", "OutOfMemory"),
    ("Killed by signal\n", "Killed"),
    ("Traceback (most recent call last):\n  stack trace begins\n", "stack trace"),
])
def test_score_logs_keyword_detection(text: str, kw: str | None) -> None:
    sev, findings = ic._score_logs(text)
    if kw is None:
        assert sev == "ok"
        assert findings == []
    else:
        assert sev == "warn"
        assert findings[0]["kind"] == "log_keyword"
        assert any(kw.lower() == m.lower() for m in findings[0]["value"])


# -----------------------------------------------------------------------------
# _severity_rank ordering
# -----------------------------------------------------------------------------


def test_severity_rank_ordering() -> None:
    assert ic._severity_rank("crit") < ic._severity_rank("warn")
    assert ic._severity_rank("warn") < ic._severity_rank("info")
    assert ic._severity_rank("info") < ic._severity_rank("unknown")


# -----------------------------------------------------------------------------
# Local route: argv shape
# -----------------------------------------------------------------------------


def test_inspect_one_local_argv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(0, stdout=json.dumps(_inspect_blob()), stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    env = ic._inspect_one("local", "my-ctr", "docker", 8)
    assert env["success"] is True
    assert captured["argv"] == [
        "docker", "inspect", "--format", "{{json .}}", "my-ctr"
    ]
    # ADR-spec invariant: argv list, no shell.
    assert isinstance(captured["argv"], list)
    assert captured["kwargs"].get("shell") in (None, False)


def test_inspect_one_local_argv_uses_runtime_podman(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _completed(0, stdout=json.dumps(_inspect_blob()), stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    ic._inspect_one("local", "my-ctr", "podman", 8)
    assert captured["argv"][0] == "podman"


def test_logs_one_local_argv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _completed(0, stdout="log line\n", stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    ic._logs_one("local", "my-ctr", 50, "docker", 8)
    assert captured["argv"] == ["docker", "logs", "--tail", "50", "my-ctr"]


def test_local_command_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `docker` is missing on local PATH, _run_local returns a clean
    failure envelope rather than raising."""
    def raise_fnf(*_args, **_kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(ic.subprocess, "run", raise_fnf)
    env = ic._inspect_one("local", "x", "docker", 8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "remote_error"


# -----------------------------------------------------------------------------
# Remote route: argv shape + shlex quoting
# -----------------------------------------------------------------------------


def test_inspect_one_remote_via_ssh_execute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict = {}
    payload = _envelope_json(
        success=True, exit_code=0,
        stdout=json.dumps(_inspect_blob()),
    )
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    monkeypatch.setattr(ic, "_ssh_execute_path", lambda: fake_ssh)

    env = ic._inspect_one("pai", "my-ctr", "docker", 8)
    assert env["success"] is True
    # First positional after the python interp + script + host is the remote command.
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1] == str(fake_ssh)
    assert captured["argv"][2] == "pai"
    expected_cmd = 'docker inspect --format \'{{json .}}\' my-ctr'
    assert captured["argv"][3] == expected_cmd
    assert "--json" in captured["argv"]
    assert "--connect-timeout" in captured["argv"]


def test_inspect_one_remote_shlex_quotes_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict = {}
    payload = _envelope_json(exit_code=0, stdout=json.dumps(_inspect_blob()))
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    monkeypatch.setattr(ic, "_ssh_execute_path", lambda: fake_ssh)

    ic._inspect_one("pai", "name with space; rm -rf /", "docker", 8)
    cmd = captured["argv"][3]
    # The whole name with spaces and `;` must end up as a single quoted token.
    assert "'name with space; rm -rf /'" in cmd


def test_remote_route_precondition_when_ssh_execute_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(ic.subprocess, "run", fail)
    monkeypatch.setattr(
        ic, "_ssh_execute_path", lambda: Path("/nonexistent/ssh_execute.py")
    )

    env = ic._inspect_one("pai", "x", "docker", 8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "precondition"


def test_remote_route_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(ic.subprocess, "run", raise_timeout)
    monkeypatch.setattr(ic, "_ssh_execute_path", lambda: fake_ssh)
    env = ic._inspect_one("pai", "x", "docker", 8)
    assert env["exit_code"] == EXIT_TIMEOUT
    assert env["data"]["failure_class"] == "timeout"


def test_remote_route_non_json_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="not json", stderr="something")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)
    monkeypatch.setattr(ic, "_ssh_execute_path", lambda: fake_ssh)
    env = ic._inspect_one("pai", "x", "docker", 8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "ssh_execute_broken"


# -----------------------------------------------------------------------------
# ssh_execute discovery (this side is for sanity; the helper above mocks it)
# -----------------------------------------------------------------------------


def test_ssh_execute_path_discovery_finds_file() -> None:
    path = ic._ssh_execute_path()
    assert path.name == "ssh_execute.py"
    assert path.exists(), f"expected ssh_execute.py at {path}"


# -----------------------------------------------------------------------------
# main(): argv + envelope shape
# -----------------------------------------------------------------------------


def test_main_argparse_missing_host_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        ic.main([])
    assert excinfo.value.code == EXIT_ARGS


def test_main_argparse_missing_name_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        ic.main(["local"])
    assert excinfo.value.code == EXIT_ARGS


def test_main_argparse_invalid_runtime_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        ic.main(["local", "ctr", "--runtime", "rkt"])
    assert excinfo.value.code == EXIT_ARGS


def test_main_happy_running_container(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end main(): inspect returns running container, logs are clean."""
    inspect_payload = json.dumps(_inspect_blob())
    logs_payload = "starting up\nready to serve\n"

    call_idx = {"i": 0}

    def fake_run(argv, **_kwargs):
        # First call: inspect; second: logs.
        idx = call_idx["i"]
        call_idx["i"] += 1
        if idx == 0:
            return _completed(0, stdout=inspect_payload, stderr="")
        return _completed(0, stdout=logs_payload, stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "my-ctr", "--json"])
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["summary"]["state"] == "ok"
    assert parsed["data"]["summary"]["logs"] == "ok"
    assert parsed["data"]["raw"]["inspect"]["state"]["running"] is True
    assert parsed["data"]["raw"]["logs"] == logs_payload
    assert parsed["data"]["failure_class"] is None
    # findings list may contain an info finding if user==1000:1000 isn't root
    # — our default test user is "1000:1000" so no root finding expected.
    assert all(f["severity"] != "crit" for f in parsed["data"]["findings"])


def test_main_oomkilled_returns_crit_and_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob(
        status="exited", running=False, oom_killed=True, exit_code=137
    ))
    logs_payload = "ran out of memory\n"

    call_idx = {"i": 0}

    def fake_run(*_args, **_kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return _completed(0,
                          stdout=inspect_payload if idx == 0 else logs_payload,
                          stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "oom-ctr", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert parsed["data"]["summary"]["state"] == "crit"
    kinds = {f["kind"] for f in parsed["data"]["findings"]}
    assert "oomkilled" in kinds


def test_main_restart_count_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob(restart_count=10))
    logs_payload = ""

    call_idx = {"i": 0}

    def fake_run(*_args, **_kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return _completed(0,
                          stdout=inspect_payload if idx == 0 else logs_payload,
                          stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "restart-ctr", "--json"])
    assert rc == EXIT_OK  # warn doesn't fail success
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["summary"]["state"] == "warn"


def test_main_unhealthy_returns_crit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob(
        health_status="unhealthy",
        health_log=[{"Output": "probe failed: refused"}],
    ))
    call_idx = {"i": 0}

    def fake_run(*_args, **_kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return _completed(0,
                          stdout=inspect_payload if idx == 0 else "",
                          stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "unhealthy-ctr", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["summary"]["state"] == "crit"
    health_finding = next(
        f for f in parsed["data"]["findings"] if f["kind"] == "health_unhealthy"
    )
    assert "refused" in health_finding["value"]


def test_main_log_keyword_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob())
    logs_payload = "INFO ok\nFATAL: something\n"

    call_idx = {"i": 0}

    def fake_run(*_args, **_kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return _completed(0,
                          stdout=inspect_payload if idx == 0 else logs_payload,
                          stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "ctr", "--json", "--tail", "50"])
    assert rc == EXIT_OK  # logs warn alone doesn't crit
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["summary"]["logs"] == "warn"


def test_main_logs_failure_keeps_inspect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob())
    call_idx = {"i": 0}

    def fake_run(*_args, **_kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        if idx == 0:
            return _completed(0, stdout=inspect_payload, stderr="")
        return _completed(1, stdout="", stderr="docker daemon stopped logging")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "ctr", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert parsed["data"]["failure_class"] == "logs_unavailable"
    # Inspect data is still present.
    assert parsed["data"]["raw"]["inspect"]["state"]["status"] == "running"


def test_main_inspect_nonexistent_container_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr="Error: No such object: nope")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "nope", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert parsed["data"]["failure_class"] == "remote_error"


def test_main_parse_error_when_inspect_not_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout="not actually json", stderr="")

    monkeypatch.setattr(ic.subprocess, "run", fake_run)

    rc = ic.main(["local", "ctr", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["failure_class"] == "parse_error"
    # Raw stdout is preserved (capped).
    assert "not actually json" in parsed["data"]["raw"]["inspect_stdout"]


def test_main_envelope_shape_complete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob())
    monkeypatch.setattr(
        ic.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=inspect_payload, stderr=""),
    )

    ic.main(["local", "ctr", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert set(parsed.keys()) >= {"success", "exit_code", "stdout", "stderr", "data"}
    data = parsed["data"]
    for key in ("host", "target", "runtime", "summary", "findings", "raw"):
        assert key in data, f"missing data.{key}"
    for sub in ("state", "config", "logs"):
        assert sub in data["summary"]


def test_main_human_stdout_no_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inspect_payload = json.dumps(_inspect_blob())
    monkeypatch.setattr(
        ic.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=inspect_payload, stderr=""),
    )

    rc = ic.main(["local", "ctr"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    # Human-readable summary, not JSON.
    assert "container:" in out
    assert "summary:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
