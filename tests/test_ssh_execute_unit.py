"""Unit tests for ssh-core/scripts/ssh_execute.py v1.0.

Subprocess is mocked via ``monkeypatch.setattr`` so these run without any
real ssh / network / agent. The live counterpart lives in
``tests/test_ssh_execute_integration.py`` (marker: ``live_ssh``).

ADR-001 (.trellis/spec/backend/adr-001-ssh-execute.md) is the contract
under test. Each assertion below references the relevant decision (D1-D8)
or PoC hypothesis (H1-H6) when applicable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_execute  # noqa: E402, I001

# Named exit codes used in assertions (avoids PLR2004 magic-value warnings).
EXIT_OK = 0
EXIT_REMOTE_ERROR = 1
EXIT_CUSTOM = 42
EXIT_TIMEOUT = 124
EXIT_NO_COMMAND = 127
EXIT_SSH_CLIENT_FAILURE = 255

# Default --timeout used by ssh_execute.main(); checked in dispatch tests.
TIMEOUT_DEFAULT = 120
TIMEOUT_BUDGET = 3.0


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    """Stand-in for subprocess.CompletedProcess that satisfies attribute access."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# -----------------------------------------------------------------------------
# local route — happy paths
# -----------------------------------------------------------------------------


def test_local_route_simple_command_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """argv list, no shell — basic happy path."""
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _completed(0, stdout="hi\n", stderr="")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_local("echo hi", timeout=5)

    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "hi\n"
    assert result["data"]["route"] == "local"
    assert result["data"]["shlex_argv"] == ["echo", "hi"]
    assert result["data"]["failure_class"] is None
    # ADR-001 D1: argv is a list, no shell=True
    assert isinstance(seen["argv"], list)
    assert seen["kwargs"].get("shell") in (None, False)
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True


# -----------------------------------------------------------------------------
# local route — shell-metachar refusal (ADR-001 D7)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected_meta"),
    [
        ("ls /tmp | head", "|"),
        ("echo hi > /tmp/x", ">"),
        ("cat < /tmp/x", "<"),
        ("true; false", ";"),
        ("true && false", "&&"),
        ("true || false", "||"),
        ("echo `whoami`", "`"),
        ("echo $(date)", "$("),
    ],
)
def test_local_route_refuses_metachars(
    command: str, expected_meta: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-001 D7: refuse |, >, <, ;, &&, ||, `, $( before any subprocess call.

    Mock subprocess.run to a sentinel that fails the test if it's ever
    reached — the refusal must short-circuit before that.
    """

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called when metachar is refused")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fail_if_called)
    result = ssh_execute._run_local(command, timeout=5)

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["data"]["route"] == "local"
    assert result["data"]["refused_metachar"] == expected_meta
    assert expected_meta in result["stderr"]
    assert "bash -c" in result["stderr"]


def test_local_route_refuses_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit named test required by implement.md C4."""

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called for pipe")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fail_if_called)
    result = ssh_execute._run_local("ls /tmp | head", timeout=5)
    assert result["data"]["refused_metachar"] == "|"


def test_local_route_refuses_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit named test required by implement.md C4."""

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called for redirect")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fail_if_called)
    result = ssh_execute._run_local("echo hi > /tmp/x", timeout=5)
    assert result["data"]["refused_metachar"] == ">"


def test_local_route_refuses_semicolon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit named test required by implement.md C4."""

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called for semicolon")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fail_if_called)
    result = ssh_execute._run_local("true; false", timeout=5)
    assert result["data"]["refused_metachar"] == ";"


# -----------------------------------------------------------------------------
# local route — error branches
# -----------------------------------------------------------------------------


def test_local_route_shlex_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """shlex raises on unbalanced quotes — surface as exit 1 with stderr."""

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("subprocess.run must not be called on shlex error")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fail_if_called)
    # Unbalanced quote, no shell metachar
    result = ssh_execute._run_local('echo "unterminated', timeout=5)
    assert result["success"] is False
    assert result["exit_code"] == 1
    assert "shlex parse error" in result["stderr"]
    assert result["data"]["route"] == "local"


def test_local_route_command_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """FileNotFoundError → exit_code=127, failure_class=remote_error."""

    def raise_fnf(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "no-such-binary-xyz")

    monkeypatch.setattr(ssh_execute.subprocess, "run", raise_fnf)
    result = ssh_execute._run_local("no-such-binary-xyz", timeout=5)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_NO_COMMAND
    assert "command not found" in result["stderr"]
    assert result["data"]["shlex_argv"] == ["no-such-binary-xyz"]


def test_local_route_timeout_returns_124(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess.TimeoutExpired → exit_code=124, failure_class=timeout."""

    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(ssh_execute.subprocess, "run", raise_timeout)
    result = ssh_execute._run_local("sleep 60", timeout=3)
    assert result["success"] is False
    assert result["exit_code"] == EXIT_TIMEOUT
    assert result["data"]["failure_class"] == "timeout"
    assert result["data"]["elapsed_s"] == TIMEOUT_BUDGET
    assert "timed out after 3s" in result["stderr"]


# -----------------------------------------------------------------------------
# remote route — envelope shape (ADR-001 D3)
# -----------------------------------------------------------------------------


def test_remote_route_envelope_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the v0.2 contract: top-level keys and data fields."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _completed(0, stdout="ok\n", stderr="")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_remote("pai", "true", timeout=5, connect_timeout=8, ssh_config=None)

    # Top-level
    assert set(result.keys()) == {"success", "exit_code", "stdout", "stderr", "data"}
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["stdout"] == "ok\n"

    # data payload (ADR-001 D3)
    data = result["data"]
    assert data["route"] == "remote"
    assert data["failure_class"] is None
    assert isinstance(data["ssh_argv"], list)
    assert isinstance(data["elapsed_s"], float)
    assert isinstance(data["ssh_noise_lines"], list)
    assert data["raw_stderr"] == ""

    # ADR-001 D2: required ssh flags every call
    assert captured["argv"][0] == "ssh"
    assert "BatchMode=yes" in captured["argv"]
    assert "ConnectTimeout=8" in captured["argv"]
    assert captured["argv"][-2:] == ["pai", "true"]
    # ADR-001 D1: argv is a list, no shell=True
    assert isinstance(captured["argv"], list)
    assert captured["kwargs"].get("shell") in (None, False)


def test_remote_route_ssh_config_adds_F_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _completed(0)

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    ssh_execute._run_remote(
        "pai", "true", timeout=5, connect_timeout=8, ssh_config="/tmp/poc-ssh-config"
    )
    assert "-F" in captured["argv"]
    f_idx = captured["argv"].index("-F")
    assert captured["argv"][f_idx + 1] == "/tmp/poc-ssh-config"


def test_remote_route_connect_timeout_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _completed(0)

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    ssh_execute._run_remote("pai", "true", timeout=5, connect_timeout=15, ssh_config=None)
    assert "ConnectTimeout=15" in captured["argv"]


# -----------------------------------------------------------------------------
# remote route — failure classification (ADR-001 D5)
# -----------------------------------------------------------------------------


def test_remote_route_timeout_returns_124(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-001 D6: TimeoutExpired → exit_code=124, failure_class=timeout."""

    def raise_timeout(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=kwargs.get("timeout", 1))

    monkeypatch.setattr(ssh_execute.subprocess, "run", raise_timeout)
    result = ssh_execute._run_remote(
        "pai", "sleep 60", timeout=3, connect_timeout=8, ssh_config=None
    )
    assert result["success"] is False
    assert result["exit_code"] == EXIT_TIMEOUT
    assert result["data"]["failure_class"] == "timeout"
    assert result["data"]["elapsed_s"] == TIMEOUT_BUDGET
    assert "timed out after 3s" in result["stderr"]


def test_remote_route_populates_failure_class_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """returncode=255 + DNS error in stderr → failure_class=network."""
    stderr = (
        "ssh: Could not resolve hostname nope.invalid: "
        "nodename nor servname provided, or not known\n"
    )

    def fake_run(*_args, **_kwargs):
        return _completed(255, stdout="", stderr=stderr)

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_remote(
        "nope.invalid", "uptime", timeout=5, connect_timeout=8, ssh_config=None
    )
    assert result["exit_code"] == EXIT_SSH_CLIENT_FAILURE
    assert result["data"]["failure_class"] == "network"


def test_remote_route_populates_failure_class_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """returncode=255 + 'Permission denied (publickey' → failure_class=auth."""
    stderr = "root@1.2.3.4: Permission denied (publickey).\n"

    def fake_run(*_args, **_kwargs):
        return _completed(255, stdout="", stderr=stderr)

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_remote(
        "locked-host", "uptime", timeout=5, connect_timeout=8, ssh_config=None
    )
    assert result["exit_code"] == EXIT_SSH_CLIENT_FAILURE
    assert result["data"]["failure_class"] == "auth"


def test_remote_route_populates_failure_class_remote_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-255 non-zero exit → failure_class=remote_error (the residual)."""

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr="")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_remote(
        "pai", "false", timeout=5, connect_timeout=8, ssh_config=None
    )
    assert result["exit_code"] == 1
    assert result["data"]["failure_class"] == "remote_error"


def test_remote_route_populates_failure_class_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """returncode=0 → failure_class is None even if stderr is non-empty."""

    def fake_run(*_args, **_kwargs):
        return _completed(0, stdout="ok\n", stderr="warning written to stderr\n")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_remote(
        "pai", "echo ok", timeout=5, connect_timeout=8, ssh_config=None
    )
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert result["data"]["failure_class"] is None


# -----------------------------------------------------------------------------
# remote route — stderr noise filtering (ADR-001 D4)
# -----------------------------------------------------------------------------


def test_remote_route_filters_ssh_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known-hosts warning moves to data.ssh_noise_lines; raw_stderr preserved."""
    stderr = (
        "Warning: Permanently added '10.0.0.1' (ED25519) to the list of known hosts.\n"
        "real-error: something went wrong\n"
    )

    def fake_run(*_args, **_kwargs):
        return _completed(1, stdout="", stderr=stderr)

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    result = ssh_execute._run_remote(
        "pai", "true", timeout=5, connect_timeout=8, ssh_config=None
    )
    # Top-level stderr keeps only the real error
    assert "real-error" in result["stderr"]
    assert "Permanently added" not in result["stderr"]
    # Noise captured into data
    assert len(result["data"]["ssh_noise_lines"]) == 1
    assert "Permanently added" in result["data"]["ssh_noise_lines"][0]
    # raw_stderr is the verbatim original (for audit)
    assert result["data"]["raw_stderr"] == stderr


# -----------------------------------------------------------------------------
# CLI surface
# -----------------------------------------------------------------------------


def test_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse --help exits 0 and prints the standard 'usage:' line."""
    with pytest.raises(SystemExit) as exc_info:
        ssh_execute.main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()
    # contract preservation: positional names + key flags
    assert "host" in captured.out
    assert "command" in captured.out
    assert "--timeout" in captured.out
    assert "--ssh-config" in captured.out
    assert "--json" in captured.out


def test_main_returns_exit_code_from_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() must return the envelope's exit_code so the shell sees it."""

    def fake_run(*_args, **_kwargs):
        return _completed(42, stdout="", stderr="")

    monkeypatch.setattr(ssh_execute.subprocess, "run", fake_run)
    rc = ssh_execute.main(["pai", "bash -c 'exit 42'", "--json"])
    assert rc == EXIT_CUSTOM


def test_main_local_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """main(['local', ...]) routes through _run_local."""
    called: dict = {}

    def fake_run_local(command, timeout):
        called["command"] = command
        called["timeout"] = timeout
        return {"success": True, "exit_code": 0, "stdout": "", "stderr": "", "data": {}}

    monkeypatch.setattr(ssh_execute, "_run_local", fake_run_local)
    monkeypatch.setattr(
        ssh_execute,
        "_run_remote",
        lambda *a, **kw: pytest.fail("_run_remote must not be called for host=local"),
    )
    rc = ssh_execute.main(["local", "echo hi", "--json"])
    assert rc == EXIT_OK
    assert called["command"] == "echo hi"
    assert called["timeout"] == TIMEOUT_DEFAULT  # default


def test_main_remote_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """main(['<host>', ...]) routes through _run_remote with all flags."""
    called: dict = {}

    def fake_run_remote(host, command, timeout, connect_timeout, ssh_config):
        called.update(
            host=host,
            command=command,
            timeout=timeout,
            connect_timeout=connect_timeout,
            ssh_config=ssh_config,
        )
        return {"success": True, "exit_code": 0, "stdout": "", "stderr": "", "data": {}}

    monkeypatch.setattr(ssh_execute, "_run_remote", fake_run_remote)
    monkeypatch.setattr(
        ssh_execute,
        "_run_local",
        lambda *a, **kw: pytest.fail("_run_local must not be called for non-local host"),
    )
    rc = ssh_execute.main(
        [
            "pai",
            "true",
            "--timeout", "30",
            "--connect-timeout", "5",
            "--ssh-config", "/tmp/x",
            "--json",
        ]
    )
    assert rc == EXIT_OK
    assert called == {
        "host": "pai",
        "command": "true",
        "timeout": 30,
        "connect_timeout": 5,
        "ssh_config": "/tmp/x",
    }
