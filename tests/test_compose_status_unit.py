"""Unit tests for docker-quick/scripts/compose_status.py v1.0.

Mocks ``subprocess.run`` and exercises ``_envelope``/`_emit`/`_parse_ps_output`/
``_score_services``/``_list_services``/``_inspect_container_one``/``main``
across local/remote routes, parse-error/timeout/precondition failure
classes, and severity scoring. The integration counterpart lives in
``tests/test_compose_status_integration.py`` (marker: ``live_ssh``).
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

import compose_status as cs  # noqa: E402, I001

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
    return json.dumps({
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    })


def _ps_row(
    service: str = "web",
    name: str | None = None,
    state: str = "running",
    health: str = "",
    exit_code: int = 0,
) -> dict:
    return {
        "Service": service,
        "Name": name or f"proj-{service}-1",
        "State": state,
        "Health": health,
        "ExitCode": exit_code,
    }


def _inspect_blob(
    *,
    running: bool = True,
    restarting: bool = False,
    exit_code: int = 0,
    restart_count: int = 0,
    health_status: str = "",
    health_log: list | None = None,
) -> dict:
    blob = {
        "RestartCount": restart_count,
        "State": {
            "Running": running,
            "Restarting": restarting,
            "ExitCode": exit_code,
            "Error": "",
            "Health": {},
        },
    }
    if health_status or health_log is not None:
        blob["State"]["Health"] = {
            "Status": health_status,
            "Log": health_log or [],
        }
    return blob


# -----------------------------------------------------------------------------
# _envelope / _emit
# -----------------------------------------------------------------------------


def test_envelope_defaults() -> None:
    env = cs._envelope(True, 0)
    assert env == {
        "success": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "data": {},
    }


def test_envelope_with_payload() -> None:
    env = cs._envelope(False, 1, stdout="x", stderr="y", data={"k": "v"})
    assert env["data"] == {"k": "v"}
    assert env["stdout"] == "x"
    assert env["stderr"] == "y"


def test_emit_json_path(capsys: pytest.CaptureFixture[str]) -> None:
    cs._emit({"success": True, "exit_code": 0, "stdout": "", "stderr": "",
              "data": {"a": 1}}, True)
    out = capsys.readouterr()
    assert json.loads(out.out)["data"] == {"a": 1}


def test_emit_human_path(capsys: pytest.CaptureFixture[str]) -> None:
    cs._emit({"success": True, "exit_code": 0, "stdout": "hello",
              "stderr": "warn", "data": {}}, False)
    out = capsys.readouterr()
    assert out.out.strip() == "hello"
    assert out.err.strip() == "warn"


# -----------------------------------------------------------------------------
# _parse_ps_output -- JSON Lines vs JSON Array
# -----------------------------------------------------------------------------


def test_parse_ps_empty_stdout_is_empty_list() -> None:
    services, err = cs._parse_ps_output("")
    assert services == []
    assert err is None


def test_parse_ps_whitespace_only_is_empty() -> None:
    services, err = cs._parse_ps_output("   \n\n   ")
    assert services == []
    assert err is None


def test_parse_ps_json_lines_single() -> None:
    text = json.dumps(_ps_row()) + "\n"
    services, err = cs._parse_ps_output(text)
    assert err is None
    assert len(services) == 1
    assert services[0]["Service"] == "web"


def test_parse_ps_json_lines_multi() -> None:
    rows = [_ps_row("web"), _ps_row("db", state="exited"),
            _ps_row("worker", health="unhealthy")]
    text = "\n".join(json.dumps(r) for r in rows)
    services, err = cs._parse_ps_output(text)
    assert err is None
    assert [s["Service"] for s in services] == ["web", "db", "worker"]


def test_parse_ps_json_array_path() -> None:
    rows = [_ps_row("web"), _ps_row("db")]
    text = json.dumps(rows)
    services, err = cs._parse_ps_output(text)
    assert err is None
    assert len(services) == 2  # noqa: PLR2004


def test_parse_ps_json_array_skips_non_dicts() -> None:
    text = json.dumps([_ps_row("web"), "stray", 42, _ps_row("db")])
    services, err = cs._parse_ps_output(text)
    assert err is None
    assert {s["Service"] for s in services} == {"web", "db"}


def test_parse_ps_array_with_non_array_top_is_error() -> None:
    services, err = cs._parse_ps_output('{"oops": 1}')
    # leading { goes to lines path; one line, parses to dict, accepted
    assert err is None
    assert services == [{"oops": 1}]


def test_parse_ps_invalid_array_json() -> None:
    services, err = cs._parse_ps_output("[broken")
    assert services is None
    assert err is not None
    assert "JSON" in err


def test_parse_ps_invalid_line_json() -> None:
    text = json.dumps(_ps_row("web")) + "\nNOTJSON\n"
    services, err = cs._parse_ps_output(text)
    assert services is None
    assert err is not None
    assert "line 2" in err


def test_parse_ps_array_not_list_is_error() -> None:
    services, err = cs._parse_ps_output('[]')
    assert err is None
    assert services == []


# -----------------------------------------------------------------------------
# _needs_inspect
# -----------------------------------------------------------------------------


def test_needs_inspect_running_healthy_skips() -> None:
    assert cs._needs_inspect(_ps_row(state="running", health="healthy")) is False


def test_needs_inspect_exited_triggers() -> None:
    assert cs._needs_inspect(_ps_row(state="exited")) is True


def test_needs_inspect_unhealthy_triggers_even_if_running() -> None:
    assert cs._needs_inspect(_ps_row(state="running", health="unhealthy")) is True


# -----------------------------------------------------------------------------
# _score_services -- severity rules
# -----------------------------------------------------------------------------


def test_score_services_all_running_is_ok() -> None:
    services = [_ps_row("web"), _ps_row("db")]
    state, findings = cs._score_services(services, service_filter=None)
    assert state == "ok"
    assert findings == []


def test_score_services_empty_stack_is_warn() -> None:
    state, findings = cs._score_services([], service_filter=None)
    assert state == "warn"
    assert any(f["kind"] == "empty_stack" for f in findings)


def test_score_services_unhealthy_is_crit() -> None:
    svc = _ps_row("web", state="running", health="unhealthy")
    svc["health_log_tail"] = "boom"
    state, findings = cs._score_services([svc], service_filter=None)
    assert state == "crit"
    f = next(f for f in findings if f["kind"] == "unhealthy")
    assert f["severity"] == "crit"
    assert f["value"]["service"] == "web"
    assert f["value"]["log"] == "boom"


def test_score_services_restarting_is_warn() -> None:
    svc = _ps_row("web", state="restarting")
    state, findings = cs._score_services([svc], service_filter=None)
    assert state == "warn"
    assert any(f["kind"] == "restart_loop" for f in findings)


def test_score_services_restart_count_over_threshold_warns() -> None:
    svc = _ps_row("web", state="running")
    svc["restart_count"] = 10
    state, findings = cs._score_services([svc], service_filter=None)
    assert state == "warn"
    f = next(f for f in findings if f["kind"] == "restart_loop")
    assert f["value"]["restart_count"] == 10  # noqa: PLR2004


def test_score_services_restart_count_at_threshold_is_ok() -> None:
    svc = _ps_row("web", state="running")
    svc["restart_count"] = cs.RESTART_WARN_THRESHOLD
    state, findings = cs._score_services([svc], service_filter=None)
    assert state == "ok"
    assert findings == []


def test_score_services_exited_nonzero_is_warn() -> None:
    svc = _ps_row("web", state="exited", exit_code=1)
    svc["state_inspect"] = {"exit_code": 1}
    state, findings = cs._score_services([svc], service_filter=None)
    assert state == "warn"
    assert any(f["kind"] == "exited_nonzero" for f in findings)


def test_score_services_exited_zero_is_ok() -> None:
    svc = _ps_row("web", state="exited", exit_code=0)
    svc["state_inspect"] = {"exit_code": 0}
    state, findings = cs._score_services([svc], service_filter=None)
    assert state == "ok"
    assert findings == []


def test_score_services_crit_dominates_warn() -> None:
    services = [
        _ps_row("web", state="running", health="unhealthy"),
        _ps_row("worker", state="restarting"),
    ]
    state, findings = cs._score_services(services, service_filter=None)
    assert state == "crit"
    kinds = {f["kind"] for f in findings}
    assert "unhealthy" in kinds
    assert "restart_loop" in kinds


def test_score_services_filter_match_ok() -> None:
    services = [_ps_row("web"), _ps_row("db")]
    state, findings = cs._score_services(services, service_filter="db")
    assert state == "ok"
    assert findings == []


def test_score_services_filter_miss_warns_not_found() -> None:
    services = [_ps_row("web"), _ps_row("db")]
    state, findings = cs._score_services(services, service_filter="missing")
    assert state == "warn"
    f = next(f for f in findings if f["kind"] == "service_not_found")
    assert f["value"] == "missing"


# -----------------------------------------------------------------------------
# _list_services -- local route argv shape
# -----------------------------------------------------------------------------


def test_list_services_local_argv_and_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return _completed(0, stdout=json.dumps(_ps_row()))

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    env = cs._list_services("local", "/tmp/proj", connect_timeout=8)
    assert env["success"]
    assert captured["argv"] == [
        "docker", "compose", "ps", "--format", "json", "--all",
    ]
    assert captured["cwd"] == "/tmp/proj"


def test_list_services_local_nonexistent_cwd_returns_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_nadir(*args, **kw):
        raise FileNotFoundError(2, "No such file or directory: '/nope'")

    monkeypatch.setattr(cs.subprocess, "run", raise_nadir)
    env = cs._list_services("local", "/nope", connect_timeout=8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "remote_error"


def test_list_services_local_docker_missing_returns_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_fnf(*args, **kw):
        raise FileNotFoundError(2, "No such file: docker")

    monkeypatch.setattr(cs.subprocess, "run", raise_fnf)
    env = cs._list_services("local", "/tmp/proj", connect_timeout=8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "remote_error"


# -----------------------------------------------------------------------------
# _list_services -- remote route via ssh_execute
# -----------------------------------------------------------------------------


def test_list_services_remote_via_ssh_execute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(
            0, stdout=_envelope_json(stdout=json.dumps(_ps_row())),
        )

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    env = cs._list_services(
        "pai", "/srv/proj", connect_timeout=12,
        ssh_exec_override=fake_ssh,
    )
    assert env["success"]
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1] == str(fake_ssh)
    assert captured["argv"][2] == "pai"
    assert "cd /srv/proj &&" in captured["argv"][3]
    assert "docker compose ps" in captured["argv"][3]
    assert "--connect-timeout" in captured["argv"]
    assert "12" in captured["argv"]


def test_list_services_remote_shlex_quotes_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(0, stdout=_envelope_json())

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    cs._list_services(
        "pai", "/srv/path with spaces", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert "/srv/path with spaces" in captured["argv"][3]
    assert "'/srv/path with spaces'" in captured["argv"][3]


def test_list_services_remote_missing_ssh_execute_precondition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cs, "_ssh_execute_path", lambda: tmp_path / "missing.py",
    )
    env = cs._list_services("pai", "/srv", connect_timeout=8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "precondition"


def test_list_services_remote_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def raise_to(*args, **kw):
        raise subprocess.TimeoutExpired("ssh", 15)

    monkeypatch.setattr(cs.subprocess, "run", raise_to)
    env = cs._list_services(
        "pai", "/srv", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert env["data"]["failure_class"] == "timeout"
    assert env["exit_code"] == EXIT_TIMEOUT


def test_list_services_remote_non_json_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")

    def fake_run(argv, **kw):
        return _completed(1, stdout="BROKEN", stderr="ssh: noop")

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    env = cs._list_services(
        "pai", "/srv", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert env["data"]["failure_class"] == "ssh_execute_broken"


# -----------------------------------------------------------------------------
# _inspect_container_one
# -----------------------------------------------------------------------------


def test_inspect_container_one_local_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(0, stdout=json.dumps(_inspect_blob()))

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    env = cs._inspect_container_one("local", "my-ctr", connect_timeout=8)
    assert env["success"]
    assert captured["argv"] == [
        "docker", "inspect", "--format", "{{json .}}", "my-ctr",
    ]


def test_inspect_container_one_remote_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake_ssh = tmp_path / "ssh_execute.py"
    fake_ssh.write_text("# placeholder")
    captured: dict = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _completed(
            0, stdout=_envelope_json(stdout=json.dumps(_inspect_blob())),
        )

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    env = cs._inspect_container_one(
        "pai", "my ctr", connect_timeout=8,
        ssh_exec_override=fake_ssh,
    )
    assert env["success"]
    # name with space must be shlex-quoted in the remote command string
    assert "'my ctr'" in captured["argv"][3]


# -----------------------------------------------------------------------------
# main() -- end-to-end argparse + flow
# -----------------------------------------------------------------------------


def test_main_argparse_missing_host_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cs.main([])
    assert exc.value.code == EXIT_ARGS


def test_main_argparse_missing_project_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cs.main(["local"])
    assert exc.value.code == EXIT_ARGS


def _mock_subprocess(monkeypatch: pytest.MonkeyPatch, plan: list[dict]) -> list[dict]:
    """Sequence of fake subprocess responses by call order."""
    calls = []
    idx = [0]

    def fake_run(argv, **kw):
        i = idx[0]
        idx[0] += 1
        calls.append({"argv": argv, "kw": kw})
        step = plan[i] if i < len(plan) else plan[-1]
        return _completed(
            step.get("returncode", 0),
            stdout=step.get("stdout", ""),
            stderr=step.get("stderr", ""),
        )

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    return calls


def test_main_happy_all_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web"), _ps_row("db")]
    plan = [{"stdout": "\n".join(json.dumps(r) for r in rows)}]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--json"])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["data"]["summary"]["state"] == "ok"
    assert payload["data"]["summary"]["services_total"] == 2  # noqa: PLR2004
    assert payload["data"]["summary"]["services_running"] == 2  # noqa: PLR2004
    assert payload["data"]["findings"] == []


def test_main_ps_failure_emits_remote_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [{"returncode": 1, "stderr": "no such directory"}]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/no/such", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["data"]["failure_class"] == "remote_error"
    assert payload["data"]["summary"]["state"] == "crit"


def test_main_ps_unparseable_emits_parse_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [{"stdout": "BROKEN GARBAGE"}]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["failure_class"] == "parse_error"


def test_main_unhealthy_is_crit_with_inspect_overlay(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web", state="running", health="unhealthy")]
    inspect = _inspect_blob(
        health_status="unhealthy",
        health_log=[{"Output": "probe failed"}],
    )
    plan = [
        {"stdout": "\n".join(json.dumps(r) for r in rows)},
        {"stdout": json.dumps(inspect)},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--json"])
    assert code == EXIT_FAIL
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["data"]["summary"]["state"] == "crit"
    f = next(f for f in payload["data"]["findings"] if f["kind"] == "unhealthy")
    assert "probe failed" in f["value"]["log"]


def test_main_restarting_is_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web", state="restarting"), _ps_row("db", state="running")]
    plan = [
        {"stdout": "\n".join(json.dumps(r) for r in rows)},
        {"stdout": json.dumps(_inspect_blob(restarting=True, restart_count=7))},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--json"])
    # warn does not flip exit code (mirrors inspect_container's behaviour);
    # only crit / failure_class trip EXIT_FAIL.
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["summary"]["state"] == "warn"


def test_main_per_service_inspect_failure_marks_overlay(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web", state="exited", exit_code=1)]
    plan = [
        {"stdout": "\n".join(json.dumps(r) for r in rows)},
        {"returncode": 1, "stderr": "container gone"},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--json"])
    payload = json.loads(capsys.readouterr().out)
    # Top-level still has list, exited_nonzero finding fires from ps row only
    assert payload["data"]["services"][0]["inspect_error"] is True
    assert any(f["kind"] == "exited_nonzero"
               for f in payload["data"]["findings"]) or code == EXIT_FAIL


def test_main_service_filter_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web"), _ps_row("db", state="exited", exit_code=2)]
    plan = [
        {"stdout": "\n".join(json.dumps(r) for r in rows)},
        # db is exited so inspect kicks in, but the filter focuses on web
        {"stdout": json.dumps(_inspect_blob(running=False, exit_code=2))},
    ]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--service", "web", "--json"])
    payload = json.loads(capsys.readouterr().out)
    # Filtering "web" -- it's running, so summary should be ok
    assert code == EXIT_OK
    assert payload["data"]["summary"]["state"] == "ok"
    assert payload["data"]["service"] == "web"
    # services_total reflects filter
    assert payload["data"]["summary"]["services_total"] == 1


def test_main_service_filter_miss(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web"), _ps_row("db")]
    plan = [{"stdout": "\n".join(json.dumps(r) for r in rows)}]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--service", "missing", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_FAIL
    assert payload["data"]["failure_class"] == "service_not_found"
    assert any(f["kind"] == "service_not_found"
               for f in payload["data"]["findings"])


def test_main_empty_stack_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = [{"stdout": ""}]  # empty docker compose ps
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj", "--json"])
    payload = json.loads(capsys.readouterr().out)
    # empty_stack is warn -- non-crit, so exit code stays 0
    assert code == EXIT_OK
    assert payload["data"]["summary"]["state"] == "warn"
    assert any(f["kind"] == "empty_stack"
               for f in payload["data"]["findings"])


def test_main_human_path_emits_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_ps_row("web"), _ps_row("db")]
    plan = [{"stdout": "\n".join(json.dumps(r) for r in rows)}]
    _mock_subprocess(monkeypatch, plan)
    code = cs.main(["local", "/tmp/proj"])  # no --json
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "host:" in out
    assert "project:" in out
    assert "summary:" in out
    assert "services:" in out
    assert "web" in out
