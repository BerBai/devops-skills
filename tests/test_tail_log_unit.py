"""Unit tests for remote-debug/scripts/tail_log.py v1.0.

The script is exercised through its in-process API (``_build_remote_command``,
``_split_lines``, ``_apply_grep``, ``_fetch_one_host``, ``main``) with
``subprocess.run`` mocked at the module level. The integration counterpart
lives in ``tests/test_tail_log_integration.py`` (marker: ``live_ssh``).
"""

from __future__ import annotations

import json
import os
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

import _local_exec  # noqa: E402, I001
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


def test_main_follow_streams_instead_of_snapshotting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--follow switches to the streaming route and reports what it saw."""
    seen: dict[str, object] = {}

    def fake_follow(ssh_exec, hosts, command, connect_timeout, follow_timeout, grep_pattern, *, echo=True, local_spec=None):
        seen["hosts"] = list(hosts)
        seen["command"] = command
        seen["timeout"] = follow_timeout
        seen["local_spec"] = local_spec
        return {
            host: {
                "alias": host,
                "success": True,
                "remote_exit_code": 0,
                "failure_class": None,
                "raw_text": "line-1\nline-2\n",
                "lines": ["line-1", "line-2"],
                "total_lines_before_grep": 2,
                "matched_lines": 2,
                "remote_stderr": "",
                "follow": True,
            }
            for host in hosts
        }

    monkeypatch.setattr(tail_log, "_run_follow_stream", fake_follow)

    rc = tail_log.main(["pai", "/var/log/app.log", "--follow", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert rc == EXIT_OK
    assert seen["hosts"] == ["pai"]
    assert "-F" in str(seen["command"]), "path mode must use `tail -F`"
    assert seen["timeout"] == tail_log.FOLLOW_DEFAULT_TIMEOUT_S
    assert parsed["data"]["lines"] == ["line-1", "line-2"]


def test_follow_command_uses_journalctl_f_in_unit_mode() -> None:
    cmd = tail_log._build_remote_command(
        path=None, unit="nginx", lines=100, since=None, follow=True
    )
    assert cmd.endswith("-f")
    assert "journalctl" in cmd


def test_follow_command_uses_tail_dash_f_in_path_mode() -> None:
    cmd = tail_log._build_remote_command(
        path="/var/log/app.log", unit=None, lines=100, since=None, follow=True
    )
    assert "-F" in cmd
    assert "tail -n 100" in cmd


def test_follow_timeout_below_the_floor_is_an_argv_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = tail_log.main(
        ["pai", "/var/log/app.log", "--follow", "--follow-timeout", "1"]
    )
    assert rc == EXIT_ARGS
    assert "follow-timeout" in capsys.readouterr().err


def test_main_multi_host_concurrent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two hosts pulled concurrently; combined stdout has both prefixes."""
    payload_a = _envelope_json(exit_code=0, stdout="alpha-1\nalpha-2\n")
    payload_b = _envelope_json(exit_code=0, stdout="bravo-1\n")
    payloads = {"pai": payload_a, "web-2": payload_b}

    def fake_run(argv, **_kwargs):
        alias = argv[2]
        return _completed(0, stdout=payloads[alias], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(
        ["--hosts", "pai,web-2", "/etc/hosts", "--lines", "5", "--json"]
    )
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert set(parsed["data"]["hosts"].keys()) == {"pai", "web-2"}
    assert parsed["data"]["hosts"]["pai"]["lines"] == ["alpha-1", "alpha-2"]
    assert parsed["data"]["hosts"]["web-2"]["lines"] == ["bravo-1"]
    # Combined human stdout has both alias prefixes (order: sorted by alias).
    assert "pai| alpha-1" in parsed["stdout"]
    assert "web-2| bravo-1" in parsed["stdout"]


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
    payloads = {"pai": payload_ok, "web-2": payload_bad}

    def fake_run(argv, **_kwargs):
        alias = argv[2]
        rc = 0 if alias == "pai" else 1
        return _completed(rc, stdout=payloads[alias], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(
        ["--hosts", "pai,web-2", "/etc/hosts", "--json"]
    )
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    # Both hosts present in data.
    assert "pai" in parsed["data"]["hosts"]
    assert "web-2" in parsed["data"]["hosts"]
    assert parsed["data"]["hosts"]["web-2"]["failure_class"] == "remote_error"
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
    payloads = {"pai": payload_a, "web-2": payload_b}

    def fake_run(argv, **_kwargs):
        return _completed(0, stdout=payloads[argv[2]], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(["--hosts", "pai,web-2", "/etc/hosts"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "pai| alpha-1" in out
    assert "web-2| bravo-1" in out


# -----------------------------------------------------------------------------
# main(): remote_stderr surfacing on failure (Finding #2 fix)
# -----------------------------------------------------------------------------


def test_main_single_host_failure_surfaces_remote_stderr_to_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-JSON single-host failure writes the remote stderr first line to local stderr."""
    payload = _envelope_json(
        success=False,
        exit_code=1,
        stderr="tail: cannot open '/missing': No such file or directory",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )

    rc = tail_log.main(["pai", "/missing"])
    assert rc == EXIT_FAIL
    cap = capsys.readouterr()
    assert (
        "tail_log: pai: tail: cannot open '/missing': No such file or directory"
        in cap.err
    )


def test_main_single_host_failure_envelope_stderr_in_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Same failure under --json: envelope.stderr carries the same message."""
    payload = _envelope_json(
        success=False,
        exit_code=1,
        stderr="tail: cannot open '/missing': No such file or directory",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )

    rc = tail_log.main(["pai", "/missing", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert (
        "tail_log: pai: tail: cannot open '/missing': No such file or directory"
        in parsed["stderr"]
    )
    # data payload shape stays intact (regression guard).
    assert parsed["data"]["remote_stderr"] == (
        "tail: cannot open '/missing': No such file or directory"
    )


def test_main_multi_host_one_failure_only_failing_alias_in_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multi-host with 1 ok + 1 failing host: only the failing alias appears in stderr."""
    payload_ok = _envelope_json(success=True, exit_code=0, stdout="ok\n")
    payload_bad = _envelope_json(
        success=False,
        exit_code=1,
        stderr="permission denied",
        data={"failure_class": "remote_error"},
    )
    payloads = {"pai": payload_ok, "web-2": payload_bad}

    def fake_run(argv, **_kwargs):
        alias = argv[2]
        rc = 0 if alias == "pai" else 1
        return _completed(rc, stdout=payloads[alias], stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(["--hosts", "pai,web-2", "/etc/hosts", "--json"])
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert "tail_log: web-2: permission denied" in parsed["stderr"]
    assert "tail_log: pai:" not in parsed["stderr"]


def test_main_multi_line_remote_stderr_keeps_only_first_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Multi-line remote stderr: only the first non-empty line is surfaced."""
    payload = _envelope_json(
        success=False,
        exit_code=1,
        stderr="line1\nline2\nline3",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )

    tail_log.main(["pai", "/missing", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert "line1" in parsed["stderr"]
    assert "line2" not in parsed["stderr"]
    assert "line3" not in parsed["stderr"]


def test_main_long_remote_stderr_truncated_to_cap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """remote_stderr > _STDERR_HEAD_CAP chars is truncated with '...' suffix."""
    long_err = "x" * 300
    payload = _envelope_json(
        success=False,
        exit_code=1,
        stderr=long_err,
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )

    tail_log.main(["pai", "/missing", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    stderr_lines = parsed["stderr"].splitlines()
    assert len(stderr_lines) == 1
    line = stderr_lines[0]
    assert line.endswith("...")
    prefix = "tail_log: pai: "
    expected_max = len(prefix) + tail_log._STDERR_HEAD_CAP + len("...")
    assert len(line) <= expected_max


def test_main_happy_path_leaves_envelope_stderr_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Successful single-host run leaves envelope.stderr empty (regression guard)."""
    payload = _envelope_json(success=True, exit_code=0, stdout="line1\n")
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    tail_log.main(["pai", "/etc/hosts", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["stderr"] == ""


def test_main_empty_remote_stderr_falls_back_to_failure_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Failure with empty remote_stderr still surfaces an alias + failure_class line."""
    payload = _envelope_json(
        success=False,
        exit_code=1,
        stderr="",
        data={"failure_class": "remote_error"},
    )
    monkeypatch.setattr(
        tail_log.subprocess, "run",
        lambda *_a, **_k: _completed(1, stdout=payload, stderr=""),
    )

    tail_log.main(["pai", "/missing", "--json"])
    parsed = json.loads(capsys.readouterr().out)
    assert "tail_log: pai:" in parsed["stderr"]
    assert "failure_class=remote_error" in parsed["stderr"]
    assert "remote_exit=1" in parsed["stderr"]


# -----------------------------------------------------------------------------
# OPT-004: the local route is platform-aware
#
# `/etc/hostname` on a Windows local route used to become `\etc\hostname` and
# be reported `local_not_found` -- a platform mismatch dressed up as a
# missing file, which also turned a multi-host run red. These tests simulate
# the platform (``_local_exec.is_windows`` is the module's own test hook, same
# pattern as ``run_shell(windows=...)``) so the Windows branch runs on Linux CI
# and the POSIX branch runs on this Windows box.
# -----------------------------------------------------------------------------


def _fake_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the local route believe it is on Windows."""
    monkeypatch.setattr(_local_exec, "is_windows", lambda: True)


def _drive_letter_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    """Create a file reachable through a drive-letter spelling of its path.

    On Windows a real temporary file already has a drive-letter path
    (``C:/Users/.../win.ini``), so the test reads the genuine thing. On POSIX
    the same spelling is a *relative* path, so the file is created under the
    temporary cwd -- which is what lets the Windows branch be exercised on
    Linux CI without pretending ``C:`` is mounted.
    """
    parts = ("C:", "Windows", "win.ini")
    target = tmp_path.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    if os.name == "nt":
        return target.as_posix(), target
    monkeypatch.chdir(tmp_path)
    return "/".join(parts), target


def test_local_posix_path_on_windows_is_a_skip_not_a_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reproducer: `local` + `/etc/hostname` on Windows is skipped."""
    _fake_windows(monkeypatch)
    env = _local_exec.tail_file("/etc/hostname", 10)

    assert env["success"] is False
    assert env["exit_code"] == 0, "a skip is not a failure exit"
    assert env["data"]["skipped"] is True
    assert env["data"]["skipped_reason"] == "posix_path_on_windows"
    assert env["data"]["failure_class"] is None
    assert "local_not_found" not in json.dumps(env)
    # The message must name the path and the remedy, not blame a missing file.
    assert "/etc/hostname" in env["stderr"]
    assert "C:/Windows/win.ini" in env["stderr"]


def test_local_posix_path_on_posix_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The POSIX branch keeps working exactly as before (simulated on Windows)."""
    monkeypatch.setattr(_local_exec, "is_windows", lambda: False)
    target = tmp_path / "app.log"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")

    env = _local_exec.tail_file(str(target), 2)

    assert env["success"] is True
    assert env["stdout"] == "two\nthree\n"
    assert "skipped" not in env["data"]
    assert env["data"]["failure_class"] is None


def test_drive_letter_path_reads_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`C:/Windows/win.ini` is a normal source on the Windows local route."""
    drive_path, target = _drive_letter_file(tmp_path, monkeypatch)
    _fake_windows(monkeypatch)

    env = _local_exec.tail_file(drive_path, 10)

    assert env["success"] is True, env
    assert env["stdout"] == "alpha\nbeta\n"
    assert env["data"].get("skipped") is not True
    assert target.exists()


def test_file_url_local_drive_letter_reads_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`file://local/C:/...` (log-aggregator's spelling) also resolves."""
    drive_path, _target = _drive_letter_file(tmp_path, monkeypatch)
    _fake_windows(monkeypatch)

    env = _local_exec.tail_file("file://local/" + drive_path, 10)

    assert env["success"] is True, env
    assert env["stdout"] == "alpha\nbeta\n"


def test_backslash_drive_letter_path_is_not_mangled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows-native spelling passes through untouched."""
    _fake_windows(monkeypatch)
    assert _local_exec.local_path_payload(
        "C:\\Windows\\win.ini", windows=True
    ) == "C:\\Windows\\win.ini"
    assert _local_exec.is_posix_path_on_windows(
        "C:\\Windows\\win.ini", windows=True
    ) is False


def test_missing_local_file_still_names_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the platform mismatch is a skip; a real missing file still fails."""
    monkeypatch.setattr(_local_exec, "is_windows", lambda: False)
    missing = tmp_path / "gone.log"

    env = _local_exec.tail_file(str(missing), 3)

    assert env["success"] is False
    assert env["data"]["failure_class"] == "local_not_found"
    assert env["data"].get("skipped") is not True
    assert "gone.log" in env["stderr"]
    assert "no such file" in env["stderr"].lower()


def test_main_multi_host_posix_path_on_windows_local_is_not_red(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every non-skipped host succeeded -> success true, exit 0, host listed."""
    _fake_windows(monkeypatch)
    payload = _envelope_json(exit_code=0, stdout="localhost.localdomain\n")
    monkeypatch.setattr(
        tail_log.subprocess,
        "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    rc = tail_log.main(["--hosts", "lab-240,local", "/etc/hostname", "--json"])

    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["exit_code"] == EXIT_OK
    assert parsed["data"]["ok_hosts"] == ["lab-240"]
    assert parsed["data"]["failed_hosts"] == []
    assert parsed["data"]["skipped_hosts"] == ["local"]

    local = parsed["data"]["hosts"]["local"]
    assert local["skipped"] is True
    assert local["skipped_reason"] == "posix_path_on_windows"
    assert local["failure_class"] is None
    assert local["lines"] == []

    remote = parsed["data"]["hosts"]["lab-240"]
    assert remote["success"] is True
    assert remote["lines"] == ["localhost.localdomain"]
    # The skip is reported, but as a skip -- not as a failure line.
    assert "tail_log: local: skipped (posix_path_on_windows)" in parsed["stderr"]
    assert "local_not_found" not in parsed["stderr"]


def test_main_multi_host_real_failure_is_still_red(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A genuine failure still fails the run, even next to a skip."""
    _fake_windows(monkeypatch)

    def fake_run(argv, **_kwargs):
        if argv[2] == "local":
            raise AssertionError("the local route must not shell out")
        return _completed(0, stdout=_envelope_json(
            success=False,
            exit_code=1,
            stderr="tail: cannot open '/etc/hostname': No such file",
            data={"failure_class": "remote_error"},
        ), stderr="")

    monkeypatch.setattr(tail_log.subprocess, "run", fake_run)

    rc = tail_log.main(["--hosts", "pai,local", "/etc/hostname", "--json"])

    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert parsed["data"]["failed_hosts"] == ["pai"]
    assert parsed["data"]["skipped_hosts"] == ["local"]


def test_main_single_local_posix_path_on_windows_is_a_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Single-host local: the skip is structured, not a local_not_found."""
    _fake_windows(monkeypatch)

    rc = tail_log.main(["local", "/etc/hostname", "--json"])

    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["skipped"] is True
    assert parsed["data"]["skipped_reason"] == "posix_path_on_windows"
    assert "failure_class" not in parsed["data"]
    assert parsed["data"]["skipped_hosts"] == ["local"]


def test_main_local_drive_letter_path_reads_the_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`--hosts local C:/Windows/win.ini` works on the native local reader."""
    drive_path, _target = _drive_letter_file(tmp_path, monkeypatch)
    _fake_windows(monkeypatch)
    monkeypatch.setattr(
        tail_log.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("local path mode shelled out")
        ),
    )

    rc = tail_log.main(["--hosts", "local", drive_path, "--json"])

    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["lines"] == ["alpha", "beta"]
    assert parsed["data"]["skipped_hosts"] == []
    assert parsed["data"]["ok_hosts"] == ["local"]


def test_main_follow_local_posix_path_on_windows_is_a_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `--follow` local route reports the same skip, not a failure."""
    _fake_windows(monkeypatch)

    rc = tail_log.main(
        ["--hosts", "local", "/etc/hostname", "--follow",
         "--follow-timeout", "5", "--json"]
    )

    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["data"]["skipped_hosts"] == ["local"]
    assert parsed["data"]["skipped_reason"] == "posix_path_on_windows"


def test_bucket_hosts_is_disjoint_and_total_preserving() -> None:
    """`ok + failed + skipped == total`, like ssh_cluster._bucket_hosts."""
    per_host = {
        "a": {"success": True, "skipped": False},
        "b": {"success": False, "skipped": True},
        "c": {"success": False, "skipped": False},
    }
    ok_hosts, failed_hosts, skipped_hosts = tail_log._bucket_hosts(per_host)
    assert ok_hosts == ["a"]
    assert failed_hosts == ["c"]
    assert skipped_hosts == ["b"]
    assert len(ok_hosts) + len(failed_hosts) + len(skipped_hosts) == len(per_host)
