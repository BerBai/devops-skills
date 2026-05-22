"""Unit tests for remote-debug/scripts/tail_log.py v1.0.

The script is exercised through its in-process API (``_build_remote_command``,
``_split_lines``, ``_apply_grep``, ``_fetch_one_host``, ``main``) with
``subprocess.run`` mocked at the module level. The integration counterpart
lives in ``tests/test_tail_log_integration.py`` (marker: ``live_ssh``).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "remote-debug" / "skills" / "remote-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import tail_log  # noqa: E402, I001

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _envelope_json(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: dict | None = None,
) -> str:
    return json.dumps(
        {
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "data": data or {},
        }
    )


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# -----------------------------------------------------------------------------
# _split_lines
# -----------------------------------------------------------------------------


def test_split_lines_strips_trailing_newline() -> None:
    assert tail_log._split_lines("a\nb\nc\n") == ["a", "b", "c"]


def test_split_lines_keeps_internal_blanks() -> None:
    expected = 3
    result = tail_log._split_lines("a\n\nc\n")
    assert result == ["a", "", "c"]
    assert len(result) == expected


def test_split_lines_empty_text() -> None:
    assert tail_log._split_lines("") == []


def test_split_lines_no_trailing_newline() -> None:
    assert tail_log._split_lines("a\nb") == ["a", "b"]


# -----------------------------------------------------------------------------
# _apply_grep
# -----------------------------------------------------------------------------


def test_apply_grep_none_pattern_returns_all() -> None:
    assert tail_log._apply_grep(["x", "y"], None) == ["x", "y"]


def test_apply_grep_search_semantics() -> None:
    pattern = re.compile(r"foo")
    assert tail_log._apply_grep(["a foo", "bar", "foo b"], pattern) == ["a foo", "foo b"]


def test_apply_grep_regex_character_class() -> None:
    pattern = re.compile(r"5[0-9][0-9]")
    lines = ["GET /api 200", "GET /api 500", "GET /api 404", "GET /api 503"]
    expected = ["GET /api 500", "GET /api 503"]
    assert tail_log._apply_grep(lines, pattern) == expected


# -----------------------------------------------------------------------------
# _build_remote_command
# -----------------------------------------------------------------------------


def test_build_remote_command_path_mode() -> None:
    cmd = tail_log._build_remote_command(
        path="/var/log/nginx/access.log", unit=None, lines=200, since=None
    )
    assert cmd == "tail -n 200 /var/log/nginx/access.log"


def test_build_remote_command_path_with_spaces_quoted() -> None:
    cmd = tail_log._build_remote_command(
        path="/var/log/my app.log", unit=None, lines=10, since=None
    )
    assert "'/var/log/my app.log'" in cmd
    assert cmd.startswith("tail -n 10 ")


def test_build_remote_command_unit_mode() -> None:
    cmd = tail_log._build_remote_command(
        path=None, unit="nginx", lines=100, since=None
    )
    assert cmd == "journalctl -u nginx -n 100 --no-pager"


def test_build_remote_command_unit_with_since() -> None:
    cmd = tail_log._build_remote_command(
        path=None, unit="nginx", lines=100, since="5min ago"
    )
    assert cmd == "journalctl -u nginx -n 100 --no-pager --since '5min ago'"


def test_build_remote_command_unit_quoted() -> None:
    # Unit names shouldn't contain shell metacharacters, but the function
    # quotes them defensively. Use a plain `;` to verify the quoting.
    cmd = tail_log._build_remote_command(
        path=None, unit="nginx;evil", lines=1, since=None
    )
    assert "'nginx;evil'" in cmd


# -----------------------------------------------------------------------------
# _run_remote_fetch: argv shape, JSON parse, timeout
# -----------------------------------------------------------------------------


def test_run_remote_fetch_argv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    payload = _envelope_json(exit_code=0, stdout="line1\nline2\n")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    env = tail_log._run_remote_fetch(Path("/fake/ssh_execute.py"), "pai", "tail -n 5 /etc/hosts", 8)

    assert env["exit_code"] == EXIT_OK
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][2] == "pai"
    assert captured["argv"][3] == "tail -n 5 /etc/hosts"
    assert "--json" in captured["argv"]
    assert "--connect-timeout" in captured["argv"]
    assert captured["kwargs"].get("shell") in (None, False)


def test_run_remote_fetch_handles_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="not json", stderr="broken")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)
    env = tail_log._run_remote_fetch(Path("/x"), "pai", "cmd", 8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "ssh_execute_broken"


def test_run_remote_fetch_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(tail_log.subprocess, "run", raise_timeout)
    env = tail_log._run_remote_fetch(Path("/x"), "pai", "cmd", 8)
    assert env["exit_code"] == EXIT_TIMEOUT
    assert env["data"]["failure_class"] == "timeout"


# -----------------------------------------------------------------------------
# _fetch_one_host: success / failure record shape
# -----------------------------------------------------------------------------


def test_fetch_one_host_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(success=True, exit_code=0, stdout="a\nb\nc\n")
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )
    rec = tail_log._fetch_one_host(
        Path("/x"), "pai", "tail -n 3 /etc/hosts", 8, grep_pattern=None
    )
    assert rec["success"] is True
    assert rec["lines"] == ["a", "b", "c"]
    expected_total = 3
    assert rec["total_lines_before_grep"] == expected_total
    assert rec["matched_lines"] == expected_total
    assert rec["failure_class"] is None


def test_fetch_one_host_grep_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=True, exit_code=0,
        stdout="GET 200\nGET 500\nGET 404\n",
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )
    pattern = re.compile(r"5[0-9][0-9]")
    rec = tail_log._fetch_one_host(Path("/x"), "pai", "cmd", 8, pattern)
    assert rec["lines"] == ["GET 500"]
    expected_total = 3
    assert rec["total_lines_before_grep"] == expected_total
    assert rec["matched_lines"] == 1


def test_fetch_one_host_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=False, exit_code=1,
        stderr="tail: /missing: No such file or directory",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )
    rec = tail_log._fetch_one_host(Path("/x"), "pai", "cmd", 8, None)
    assert rec["success"] is False
    assert rec["remote_exit_code"] == EXIT_FAIL
    assert rec["failure_class"] == "remote_error"


def test_fetch_one_host_auth_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _envelope_json(
        success=False, exit_code=255,
        data={"failure_class": "auth"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(255, stdout=payload, stderr=""),
    )
    rec = tail_log._fetch_one_host(Path("/x"), "pai", "cmd", 8, None)
    assert rec["success"] is False
    assert rec["failure_class"] == "auth"


# -----------------------------------------------------------------------------
# ssh_execute.py discovery + precondition
# -----------------------------------------------------------------------------


def test_ssh_execute_path_discovery_finds_file() -> None:
    path = tail_log._ssh_execute_path()
    assert path.name == "ssh_execute.py"
    assert path.exists(), f"expected ssh_execute.py at {path}"


def test_main_precondition_when_ssh_execute_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_path = Path("/nonexistent/ssh_execute.py")
    monkeypatch.setattr(tail_log, "_ssh_execute_path", lambda: fake_path)

    def fail(*_a, **_k):
        raise AssertionError("subprocess.run must not be called")

    monkeypatch.setattr(tail_log.subprocess, "run", fail)

    rc = tail_log.main(["pai", "/etc/hosts", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["failure_class"] == "precondition"


# -----------------------------------------------------------------------------
# main(): argv error paths
# -----------------------------------------------------------------------------


def test_main_rejects_no_host(capsys: pytest.CaptureFixture[str]) -> None:
    rc = tail_log.main(["--unit", "nginx"])
    assert rc == EXIT_ARGS
    assert "alias" in capsys.readouterr().err


def test_main_rejects_no_source(capsys: pytest.CaptureFixture[str]) -> None:
    rc = tail_log.main(["pai"])
    assert rc == EXIT_ARGS
    assert "path" in capsys.readouterr().err or "--unit" in capsys.readouterr().err


def test_main_rejects_both_path_and_unit(capsys: pytest.CaptureFixture[str]) -> None:
    rc = tail_log.main(["pai", "/var/log/foo.log", "--unit", "nginx"])
    assert rc == EXIT_ARGS
    assert "both" in capsys.readouterr().err


def test_main_rejects_bad_grep_regex(capsys: pytest.CaptureFixture[str]) -> None:
    rc = tail_log.main(["pai", "/etc/hosts", "--grep", "[unterminated"])
    assert rc == EXIT_ARGS
    assert "regex" in capsys.readouterr().err


# -----------------------------------------------------------------------------
# main(): happy paths
# -----------------------------------------------------------------------------


def test_main_single_host_path_happy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _envelope_json(success=True, exit_code=0, stdout="line1\nline2\n")
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    rc = tail_log.main(["pai", "/etc/hosts", "--lines", "5", "--json"])
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["host"] == "pai"
    assert parsed["data"]["lines"] == ["line1", "line2"]
    assert parsed["data"]["source"]["kind"] == "path"
    assert parsed["data"]["source"]["value"] == "/etc/hosts"


def test_main_single_host_unit_happy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _envelope_json(
        success=True, exit_code=0,
        stdout="Jan 01 12:00:00 host nginx[1234]: log line\n",
    )
    captured: dict = {}

    def fake_run(argv, **_kwargs):
        captured["cmd"] = argv[3]  # the remote command string
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(["pai", "--unit", "nginx", "--lines", "10", "--json"])
    assert rc == EXIT_OK
    assert "journalctl -u nginx -n 10 --no-pager" in captured["cmd"]
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["source"]["kind"] == "unit"
    assert parsed["data"]["source"]["value"] == "nginx"


def test_main_unit_with_since_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _envelope_json(exit_code=0, stdout="")
    captured: dict = {}

    def fake_run(argv, **_kwargs):
        captured["cmd"] = argv[3]
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    tail_log.main(["pai", "--unit", "nginx", "--since", "5min ago", "--json"])
    assert "--since '5min ago'" in captured["cmd"]


def test_main_since_without_unit_warns_and_ignores(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _envelope_json(exit_code=0, stdout="x\n")
    captured: dict = {}

    def fake_run(argv, **_kwargs):
        captured["cmd"] = argv[3]
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(
        ["pai", "/etc/hosts", "--since", "1h ago", "--json"]
    )
    cap = capsys.readouterr()
    assert rc == EXIT_OK
    assert "since" in cap.err.lower()
    # The remote command must NOT contain --since.
    assert "--since" not in captured["cmd"]


def test_main_grep_filters_locally(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _envelope_json(
        exit_code=0,
        stdout="GET /a 200\nGET /b 500\nGET /c 404\n",
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    rc = tail_log.main(
        ["pai", "/var/log/access.log", "--grep", "5[0-9][0-9]", "--json"]
    )
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["data"]["lines"] == ["GET /b 500"]
    assert parsed["data"]["filter"]["grep"] == "5[0-9][0-9]"
    assert parsed["data"]["filter"]["matched"] == 1
    expected_total = 3
    assert parsed["data"]["filter"]["total"] == expected_total


def test_main_follow_writes_deferred_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = _envelope_json(exit_code=0, stdout="x\n")
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    rc = tail_log.main(["pai", "/etc/hosts", "--follow", "--json"])
    cap = capsys.readouterr()
    assert rc == EXIT_OK
    assert "follow" in cap.err.lower()
    assert "deferred" in cap.err.lower()


def test_main_multi_host_concurrent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two hosts pulled concurrently; combined stdout has both prefixes."""
    payload_a = _envelope_json(exit_code=0, stdout="alpha-1\nalpha-2\n")
    payload_b = _envelope_json(exit_code=0, stdout="bravo-1\n")
    payloads = {"pai": payload_a, "local": payload_b}

    def fake_run(argv, **_kwargs):
        alias = argv[2]
        return _completed(0, stdout=payloads[alias], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(
        ["--hosts", "pai,local", "/etc/hosts", "--lines", "5", "--json"]
    )
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert set(parsed["data"]["hosts"].keys()) == {"pai", "local"}
    assert parsed["data"]["hosts"]["pai"]["lines"] == ["alpha-1", "alpha-2"]
    assert parsed["data"]["hosts"]["local"]["lines"] == ["bravo-1"]
    # Combined human stdout has both alias prefixes (order: sorted by alias).
    assert "pai| alpha-1" in parsed["stdout"]
    assert "local| bravo-1" in parsed["stdout"]


def test_main_multi_host_one_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When one host fails, others still report; top-level success=false."""
    payload_ok = _envelope_json(exit_code=0, stdout="ok\n")
    payload_bad = _envelope_json(
        success=False, exit_code=1,
        stderr="permission denied",
        data={"failure_class": "remote_error"},
    )
    payloads = {"pai": payload_ok, "local": payload_bad}

    def fake_run(argv, **_kwargs):
        alias = argv[2]
        rc = 0 if alias == "pai" else 1
        return _completed(rc, stdout=payloads[alias], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(
        ["--hosts", "pai,local", "/etc/hosts", "--json"]
    )
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    # Both hosts present in data.
    assert "pai" in parsed["data"]["hosts"]
    assert "local" in parsed["data"]["hosts"]
    assert parsed["data"]["hosts"]["local"]["failure_class"] == "remote_error"
    assert parsed["data"]["hosts"]["pai"]["lines"] == ["ok"]


def test_main_envelope_shape_complete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Top-level envelope conforms to error-handling.md."""
    payload = _envelope_json(exit_code=0, stdout="x\n")
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    tail_log.main(["pai", "/etc/hosts", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert set(parsed.keys()) >= {"success", "exit_code", "stdout", "stderr", "data"}


def test_main_non_json_writes_prefixed_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload_a = _envelope_json(exit_code=0, stdout="alpha-1\n")
    payload_b = _envelope_json(exit_code=0, stdout="bravo-1\n")
    payloads = {"pai": payload_a, "local": payload_b}

    def fake_run(argv, **_kwargs):
        return _completed(0, stdout=payloads[argv[2]], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(["--hosts", "pai,local", "/etc/hosts"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "pai| alpha-1" in out
    assert "local| bravo-1" in out
