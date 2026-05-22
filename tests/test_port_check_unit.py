"""Unit tests for remote-debug/scripts/port_check.py v1.0.

The script is exercised through its in-process API (``_parse_ports``,
``_classify_status``, ``_build_probe_command``, ``main``) with
``subprocess.run`` mocked at the module level. The integration counterpart
lives in ``tests/test_port_check_integration.py`` (marker: ``live_ssh``).

The architectural claim under test is CONTRIBUTING.md line 29 -- that
remote-debug invokes ssh-core via ``subprocess`` and never via a Python
import. The mocked subprocess.run stand-in lets us assert the argv
shape *and* feed back synthetic JSON envelopes.
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
    REPO_ROOT / "plugins" / "remote-debug" / "skills" / "remote-debug" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import port_check  # noqa: E402, I001

# Magic-value constants (avoids PLR2004 in assertions).
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ARGS = 2
EXIT_TIMEOUT = 124

# Reference values used across tests.
PORT_SSH = 22
PORT_HTTP = 80
PORT_PG = 5432
PORT_REDIS = 6379
PORT_HIGH = 9999
DEFAULT_PORT_TIMEOUT = 2


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
    """Build the JSON string ssh_execute.py would print on --json."""
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


def _envelope_dict(
    success: bool = True,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    data: dict | None = None,
) -> dict:
    return {
        "success": success,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "data": data or {},
    }


# -----------------------------------------------------------------------------
# _parse_ports
# -----------------------------------------------------------------------------


def test_parse_ports_single() -> None:
    assert port_check._parse_ports("5432") == [PORT_PG]


def test_parse_ports_csv() -> None:
    assert port_check._parse_ports("5432,6379") == [PORT_PG, PORT_REDIS]


def test_parse_ports_range_inclusive() -> None:
    assert port_check._parse_ports("8000:8002") == [8000, 8001, 8002]


def test_parse_ports_mixed_csv_and_range() -> None:
    assert port_check._parse_ports("22,80:82,443") == [
        PORT_SSH,
        PORT_HTTP,
        81,
        82,
        443,
    ]


def test_parse_ports_dedup_preserves_order() -> None:
    # 22 appears twice (literal + start of range); only the first occurrence kept.
    assert port_check._parse_ports("22,22:24,23") == [PORT_SSH, 23, 24]


def test_parse_ports_strips_whitespace() -> None:
    assert port_check._parse_ports(" 22 , 80 ") == [PORT_SSH, PORT_HTTP]


def test_parse_ports_rejects_zero() -> None:
    with pytest.raises(ValueError):
        port_check._parse_ports("0")


def test_parse_ports_rejects_above_max() -> None:
    with pytest.raises(ValueError):
        port_check._parse_ports("65536")


def test_parse_ports_rejects_reversed_range() -> None:
    with pytest.raises(ValueError):
        port_check._parse_ports("80:22")


def test_parse_ports_rejects_non_numeric() -> None:
    with pytest.raises(ValueError):
        port_check._parse_ports("abc")


def test_parse_ports_rejects_range_with_non_numeric_bound() -> None:
    with pytest.raises(ValueError):
        port_check._parse_ports("80:abc")


# -----------------------------------------------------------------------------
# _build_probe_command
# -----------------------------------------------------------------------------


def test_build_probe_command_contains_both_branches() -> None:
    cmd = port_check._build_probe_command("db.example.com", PORT_PG, DEFAULT_PORT_TIMEOUT)
    assert "nc -zv -w 2 db.example.com 5432" in cmd
    assert "/dev/tcp/db.example.com/5432" in cmd
    assert "command -v nc" in cmd


def test_build_probe_command_shell_quotes_target() -> None:
    # A target with shell metacharacters must be quoted so the remote shell
    # treats it as a single token (ADR-001 D7 spirit, applied client-side).
    cmd = port_check._build_probe_command("a;rm -rf /", PORT_SSH, DEFAULT_PORT_TIMEOUT)
    # Quoted as a single shell word -- the `;` is now inside quotes.
    assert "'a;rm -rf /'" in cmd


# -----------------------------------------------------------------------------
# _classify_status
# -----------------------------------------------------------------------------


def test_classify_status_open_when_exit_zero() -> None:
    env = _envelope_dict(exit_code=0, stdout="Connection to ::1 22 port [tcp/ssh] succeeded!")
    assert port_check._classify_status(env) == "open"


def test_classify_status_refused() -> None:
    env = _envelope_dict(exit_code=1, stdout="nc: connect to 127.0.0.1 port 9999 (tcp) failed: Connection refused")
    assert port_check._classify_status(env) == "refused"


def test_classify_status_refused_even_when_exit_zero() -> None:
    # Defensive: some nc variants exit 0 even on refusal.
    env = _envelope_dict(exit_code=0, stdout="Connection refused")
    assert port_check._classify_status(env) == "refused"


def test_classify_status_host_error() -> None:
    env = _envelope_dict(exit_code=1, stdout="nc: nope.invalid: Name or service not known")
    assert port_check._classify_status(env) == "host-error"


def test_classify_status_filtered_via_text() -> None:
    env = _envelope_dict(exit_code=1, stdout="nc: connect to 10.0.0.1 port 5432 (tcp) failed: Operation timed out")
    assert port_check._classify_status(env) == "filtered"


def test_classify_status_filtered_via_remote_124() -> None:
    # /dev/tcp fallback driven by `timeout(1)` returns 124 on the remote.
    env = _envelope_dict(exit_code=EXIT_TIMEOUT, stdout="")
    assert port_check._classify_status(env) == "filtered"


def test_classify_status_source_error_auth() -> None:
    env = _envelope_dict(
        success=False,
        exit_code=255,
        data={"failure_class": "auth"},
    )
    assert port_check._classify_status(env) == "source-error"


def test_classify_status_source_error_network() -> None:
    env = _envelope_dict(
        success=False,
        exit_code=255,
        data={"failure_class": "network"},
    )
    assert port_check._classify_status(env) == "source-error"


def test_classify_status_source_error_broken() -> None:
    env = _envelope_dict(
        success=False,
        exit_code=1,
        data={"failure_class": "ssh_execute_broken"},
    )
    assert port_check._classify_status(env) == "source-error"


def test_classify_status_timeout_failure_class_becomes_filtered() -> None:
    # Subprocess-level timeout (ssh_execute itself), not a port probe timeout.
    env = _envelope_dict(
        success=False,
        exit_code=EXIT_TIMEOUT,
        data={"failure_class": "timeout"},
    )
    assert port_check._classify_status(env) == "filtered"


def test_classify_status_unknown_nonzero_leans_filtered() -> None:
    env = _envelope_dict(exit_code=42, stdout="weird output we don't recognise")
    assert port_check._classify_status(env) == "filtered"


# -----------------------------------------------------------------------------
# _run_remote_probe: argv shape, JSON parse, timeout
# -----------------------------------------------------------------------------


def test_run_remote_probe_argv_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    payload = _envelope_json(exit_code=0, stdout="ok")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(port_check.subprocess, "run", fake_run)

    ssh_exec = Path("/fake/ssh_execute.py")
    env = port_check._run_remote_probe(ssh_exec, "pai", "nc -zv -w 2 a 22", 8)

    assert env["exit_code"] == EXIT_OK
    # Shell-out contract: python -> ssh_execute.py -> host -> command -> --json ... .
    assert captured["argv"][0] == sys.executable
    assert captured["argv"][1] == str(ssh_exec)
    assert captured["argv"][2] == "pai"
    assert captured["argv"][3] == "nc -zv -w 2 a 22"
    assert "--json" in captured["argv"]
    assert "--connect-timeout" in captured["argv"]
    # ADR-001 D1: argv list, no shell.
    assert isinstance(captured["argv"], list)
    assert captured["kwargs"].get("shell") in (None, False)


def test_run_remote_probe_handles_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="not actually json", stderr="broken")

    monkeypatch.setattr(port_check.subprocess, "run", fake_run)
    env = port_check._run_remote_probe(Path("/x"), "pai", "cmd", 8)
    assert env["success"] is False
    assert env["data"]["failure_class"] == "ssh_execute_broken"
    assert "non-JSON" in env["stderr"]


def test_run_remote_probe_handles_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(port_check.subprocess, "run", raise_timeout)
    env = port_check._run_remote_probe(Path("/x"), "pai", "cmd", 8)
    assert env["exit_code"] == EXIT_TIMEOUT
    assert env["data"]["failure_class"] == "timeout"


# -----------------------------------------------------------------------------
# ssh_execute.py discovery and precondition envelope
# -----------------------------------------------------------------------------


def test_ssh_execute_path_discovery_finds_file() -> None:
    path = port_check._ssh_execute_path()
    assert path.name == "ssh_execute.py"
    assert path.exists(), f"expected ssh_execute.py at {path}"


def test_main_returns_precondition_envelope_when_ssh_execute_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_path = Path("/nonexistent/ssh_execute.py")
    monkeypatch.setattr(port_check, "_ssh_execute_path", lambda: fake_path)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called when ssh_execute is missing")

    monkeypatch.setattr(port_check.subprocess, "run", fail_if_called)

    rc = port_check.main(["pai", "--target", "localhost", "--ports", "22", "--json"])

    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    assert parsed["data"]["failure_class"] == "precondition"
    assert "ssh_execute.py" in parsed["stderr"]


# -----------------------------------------------------------------------------
# main(): argv error paths
# -----------------------------------------------------------------------------


def test_main_rejects_missing_source(capsys: pytest.CaptureFixture[str]) -> None:
    rc = port_check.main(["--target", "localhost", "--ports", "22"])
    assert rc == EXIT_ARGS
    assert "need either" in capsys.readouterr().err


def test_main_rejects_missing_target(capsys: pytest.CaptureFixture[str]) -> None:
    rc = port_check.main(["pai", "--ports", "22"])
    assert rc == EXIT_ARGS
    assert "--target" in capsys.readouterr().err


def test_main_rejects_bad_ports(capsys: pytest.CaptureFixture[str]) -> None:
    rc = port_check.main(["pai", "--target", "localhost", "--ports", "abc"])
    assert rc == EXIT_ARGS
    assert "invalid port" in capsys.readouterr().err


def test_main_rejects_empty_ports(capsys: pytest.CaptureFixture[str]) -> None:
    rc = port_check.main(["pai", "--target", "localhost", "--ports", ","])
    assert rc == EXIT_ARGS
    err = capsys.readouterr().err
    assert "empty" in err


# -----------------------------------------------------------------------------
# main(): happy path (mocked subprocess.run)
# -----------------------------------------------------------------------------


def test_main_single_source_single_target_all_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One alias, one target, 2 ports, both open -> exit 0, success=true."""
    payload_open = _envelope_json(
        success=True,
        exit_code=0,
        stdout="Connection to localhost 22 port [tcp/ssh] succeeded!\n",
        data={"route": "remote", "failure_class": None},
    )

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout=payload_open, stderr="")

    monkeypatch.setattr(port_check.subprocess, "run", fake_run)

    rc = port_check.main(
        ["pai", "--target", "localhost", "--ports", "22,80", "--json"]
    )
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is True
    assert parsed["exit_code"] == EXIT_OK
    assert parsed["data"]["summary"]["open"] == 2  # noqa: PLR2004
    assert parsed["data"]["summary"]["total"] == 2  # noqa: PLR2004
    matrix = parsed["data"]["matrix"]
    assert len(matrix) == 2  # noqa: PLR2004
    assert {c["status"] for c in matrix} == {"open"}
    assert all("elapsed_ms" in c for c in matrix)


def test_main_mixed_open_and_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two ports: first open, second refused -> exit 1."""
    payloads = [
        _envelope_json(exit_code=0, stdout="succeeded!"),
        _envelope_json(exit_code=1, stdout="Connection refused"),
    ]
    call_idx = {"i": 0}

    def fake_run(*_args, **_kwargs):
        out = payloads[call_idx["i"]]
        call_idx["i"] += 1
        return _completed(0, stdout=out, stderr="")

    monkeypatch.setattr(port_check.subprocess, "run", fake_run)

    rc = port_check.main(
        ["pai", "--target", "localhost", "--ports", "22,9999", "--json"]
    )
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["success"] is False
    statuses = [c["status"] for c in parsed["data"]["matrix"]]
    assert statuses == ["open", "refused"]


def test_main_matrix_mode_generates_all_cells(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--from a,b --to x,y --ports p1,p2 -> 8 cells."""
    payload = _envelope_json(exit_code=0, stdout="succeeded!")

    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return _completed(0, stdout=payload, stderr="")

    monkeypatch.setattr(port_check.subprocess, "run", fake_run)

    rc = port_check.main(
        [
            "--from", "src1,src2",
            "--to", "tgt1,tgt2",
            "--ports", "22,80",
            "--json",
        ]
    )
    assert rc == EXIT_OK
    parsed = json.loads(capsys.readouterr().out)
    # 2 sources x 2 targets x 2 ports = 8 cells
    expected_cells = 8
    assert len(parsed["data"]["matrix"]) == expected_cells
    # All 8 subprocess invocations were made.
    assert len(calls) == expected_cells
    # Sanity: first source was used in the first 4 calls (one source iterated
    # over targets x ports before moving on).
    assert calls[0][2] == "src1"
    assert calls[expected_cells - 1][2] == "src2"


def test_main_udp_flag_writes_deferred_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--udp must not crash; v0.2 surface compatibility (R2)."""
    payload = _envelope_json(exit_code=0, stdout="succeeded!")
    monkeypatch.setattr(
        port_check.subprocess,
        "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    rc = port_check.main(
        ["pai", "--target", "localhost", "--ports", "22", "--udp", "--json"]
    )

    cap = capsys.readouterr()
    assert rc == EXIT_OK  # TCP probe still ran; UDP only writes warning
    assert "udp" in cap.err.lower()
    assert "deferred" in cap.err.lower()
    parsed = json.loads(cap.out)
    assert parsed["success"] is True


def test_main_propagates_source_error_to_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ssh_execute returns failure_class=auth, cell status=source-error
    and overall exit=1."""
    payload = _envelope_json(
        success=False,
        exit_code=255,
        stderr="Permission denied (publickey)",
        data={"failure_class": "auth"},
    )
    monkeypatch.setattr(
        port_check.subprocess,
        "run",
        lambda *_a, **_k: _completed(255, stdout=payload, stderr=""),
    )

    rc = port_check.main(
        ["pai", "--target", "localhost", "--ports", "22", "--json"]
    )
    assert rc == EXIT_FAIL
    parsed = json.loads(capsys.readouterr().out)
    cell = parsed["data"]["matrix"][0]
    assert cell["status"] == "source-error"
    assert cell["failure_class"] == "auth"
    assert parsed["data"]["summary"]["any_source_error"] is True
    assert parsed["data"]["failure_class"] == "source"


def test_main_envelope_shape_complete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The top-level envelope conforms to error-handling.md."""
    payload = _envelope_json(exit_code=0, stdout="succeeded!")
    monkeypatch.setattr(
        port_check.subprocess,
        "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    port_check.main(["pai", "--target", "localhost", "--ports", "22", "--json"])

    parsed = json.loads(capsys.readouterr().out)
    # error-handling.md: success, exit_code, stdout, stderr, data are mandatory.
    assert set(parsed.keys()) >= {"success", "exit_code", "stdout", "stderr", "data"}
    # No top-level keys other than the contract (allowed: subset).
    data = parsed["data"]
    assert "matrix" in data
    assert "summary" in data
    assert "sources" in data
    assert "targets" in data
    assert "ports" in data


def test_main_non_json_writes_human_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --json, stdout is a human-readable table."""
    payload = _envelope_json(exit_code=0, stdout="succeeded!")
    monkeypatch.setattr(
        port_check.subprocess,
        "run",
        lambda *_a, **_k: _completed(0, stdout=payload, stderr=""),
    )

    rc = port_check.main(["pai", "--target", "localhost", "--ports", "22"])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "pai" in out
    assert "localhost:22" in out
    assert "open" in out
    # Should not be JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
