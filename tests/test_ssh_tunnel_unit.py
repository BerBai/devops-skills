"""Unit tests for ssh_tunnel v1.0.

Every test mocks `subprocess.Popen`, `socket.socket`, `os.kill`,
`time.sleep` / `time.monotonic`, `sys.platform`, and `random.randint`
at the module level so no real ssh subprocess, no real bind, and no
sleep ever happens. State-file IO uses `tmp_path` via a monkeypatched
`tempfile.gettempdir`.

Coverage target per implement.md Phase T1: >=90% statement coverage on
`ssh_tunnel.py`; every failure_class in D7 exercised at least once.

The integration counterpart in `tests/test_ssh_tunnel_integration.py`
exercises the same code paths against a real host via the `live_ssh`
marker.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_tunnel  # noqa: E402

# Named exit codes mirror the failure-class table in design.md / PRD D7.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_POSIX_ONLY = 30
EXIT_PORT_BUSY = 31
EXIT_PORT_POOL_EXHAUSTED = 32
EXIT_SSH_CONNECT_FAILED = 33
EXIT_AUTH = 34
EXIT_FORWARD_SETUP_FAILED = 35
EXIT_TUNNEL_NOT_FOUND = 36
EXIT_AMBIGUOUS_STOP = 37
EXIT_STALE_STATE_ONLY = 38
EXIT_KILL_FAILED = 39

DUMMY_PID = 4242
ANOTHER_PID = 4243
REMOTE_PORT = 22
LOCAL_PORT_A = 13306
LOCAL_PORT_B = 13307
RANDOM_POOL_PORT = 15555
IDLE_TIMEOUT_USER_SET = 30
CONNECT_TIMEOUT_USER_SET = 12
STATE_FILE_MODE = 0o600
KEYBOARD_INTERRUPT_EXIT = 130


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Force ssh_tunnel state files into a tmp dir per test."""
    monkeypatch.setattr(ssh_tunnel.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / "ssh_tunnel"


@pytest.fixture
def _fake_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `_posix()` to return True so start can proceed."""
    monkeypatch.setattr(ssh_tunnel, "_posix", lambda: True)


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip `time.sleep` (still callable; just returns immediately)."""
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _run_main(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    rc = ssh_tunnel.main(argv)
    out = capsys.readouterr().out
    if not out.strip():
        return rc, {}
    return rc, json.loads(out)


class _FakePopen:
    """Drop-in for subprocess.Popen[bytes] with a configurable lifecycle."""

    def __init__(
        self,
        *,
        pid: int = DUMMY_PID,
        poll_sequence: list[int | None] | None = None,
        stderr_bytes: bytes = b"",
    ) -> None:
        self.pid = pid
        self._poll_sequence = poll_sequence if poll_sequence is not None else [None, None]
        self._poll_idx = 0
        self.stderr = MagicMock()
        self.stderr.read.return_value = stderr_bytes
        self.stderr.close = MagicMock()
        self._terminated = False
        self._killed = False
        self._waited = False

    def poll(self) -> int | None:
        if self._poll_idx >= len(self._poll_sequence):
            return self._poll_sequence[-1]
        rc = self._poll_sequence[self._poll_idx]
        self._poll_idx += 1
        return rc

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._killed = True

    def wait(self, timeout: float | None = None) -> int:
        self._waited = True
        return 0


# -----------------------------------------------------------------------------
# build_parser
# -----------------------------------------------------------------------------


def test_parser_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        ssh_tunnel.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    assert "start" in out and "stop" in out and "status" in out and "list" in out


@pytest.mark.parametrize("sub", ["start", "stop", "status", "list"])
def test_parser_subcommand_help_exits_zero(
    sub: str, capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        ssh_tunnel.main([sub, "--help"])
    assert exc.value.code == 0


def test_parser_start_requires_remote_port(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        ssh_tunnel.build_parser().parse_args(["start", "pai"])  # missing --remote-port


def test_parser_start_full_argv_parses() -> None:
    args = ssh_tunnel.build_parser().parse_args(
        ["start", "pai",
         "--remote-port", "22",
         "--local-port", "13306",
         "--remote-host", "127.0.0.1",
         "--bind-address", "0.0.0.0",
         "--idle-timeout", "30",
         "--connect-timeout", "12",
         "--no-detach"],
    )
    assert args.cmd == "start"
    assert args.alias == "pai"
    assert args.remote_port == REMOTE_PORT
    assert args.local_port == LOCAL_PORT_A
    assert args.remote_host == "127.0.0.1"
    assert args.bind_address == "0.0.0.0"
    assert args.idle_timeout == IDLE_TIMEOUT_USER_SET
    assert args.connect_timeout == CONNECT_TIMEOUT_USER_SET
    assert args.no_detach is True


# -----------------------------------------------------------------------------
# State file primitives
# -----------------------------------------------------------------------------


def test_state_path_is_deterministic_per_alias_port(tmp_path: Path) -> None:
    a1 = ssh_tunnel._state_path("pai", LOCAL_PORT_A)
    a2 = ssh_tunnel._state_path("pai", LOCAL_PORT_A)
    b = ssh_tunnel._state_path("pai", LOCAL_PORT_B)
    assert a1 == a2
    assert a1 != b


def test_write_and_read_state_roundtrip(tmp_path: Path) -> None:
    entry = ssh_tunnel._StateEntry(
        pid=DUMMY_PID, local_port=LOCAL_PORT_A, remote_host="127.0.0.1",
        remote_port=REMOTE_PORT, bind_address="127.0.0.1", alias="pai",
        started_at="2026-05-26T00:00:00+00:00",
    )
    ssh_tunnel._write_state(entry)
    re_read = ssh_tunnel._read_state(entry.path)
    assert re_read is not None
    assert re_read.pid == DUMMY_PID
    assert re_read.local_port == LOCAL_PORT_A
    assert re_read.alias == "pai"
    # mode is 0o600
    mode = entry.path.stat().st_mode & 0o777
    assert mode == STATE_FILE_MODE


def test_read_state_corrupt_returns_none(tmp_path: Path) -> None:
    p = ssh_tunnel._state_dir() / "bogus.json"
    p.write_text("not-json")
    assert ssh_tunnel._read_state(p) is None


def test_read_state_missing_field_returns_none(tmp_path: Path) -> None:
    p = ssh_tunnel._state_dir() / "incomplete.json"
    p.write_text(json.dumps({"pid": 1}))  # missing required fields
    assert ssh_tunnel._read_state(p) is None


def test_scan_state_orders_and_filters(tmp_path: Path) -> None:
    e1 = ssh_tunnel._StateEntry(
        pid=DUMMY_PID, local_port=LOCAL_PORT_A, remote_host="r",
        remote_port=REMOTE_PORT, bind_address="b", alias="pai",
        started_at="t1",
    )
    e2 = ssh_tunnel._StateEntry(
        pid=ANOTHER_PID, local_port=LOCAL_PORT_B, remote_host="r",
        remote_port=REMOTE_PORT, bind_address="b", alias="pai",
        started_at="t2",
    )
    ssh_tunnel._write_state(e1)
    ssh_tunnel._write_state(e2)
    # Drop a corrupt file
    (ssh_tunnel._state_dir() / "bad.json").write_text("not-json")
    results = ssh_tunnel._scan_state()
    aliases_ports = sorted((r.alias, r.local_port) for r in results)
    assert aliases_ports == [("pai", LOCAL_PORT_A), ("pai", LOCAL_PORT_B)]


def test_pid_alive_true_for_self(_no_sleep: None) -> None:
    assert ssh_tunnel._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_pid: int, _sig: int) -> None:
        raise ProcessLookupError()
    monkeypatch.setattr(ssh_tunnel.os, "kill", _boom)
    assert ssh_tunnel._pid_alive(99999) is False


def test_pid_alive_true_for_other_user_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_pid: int, _sig: int) -> None:
        raise PermissionError()
    monkeypatch.setattr(ssh_tunnel.os, "kill", _boom)
    assert ssh_tunnel._pid_alive(1) is True


# -----------------------------------------------------------------------------
# Port allocator
# -----------------------------------------------------------------------------


def test_alloc_explicit_free_port_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)
    assert ssh_tunnel._alloc_local_port(LOCAL_PORT_A) == LOCAL_PORT_A


def test_alloc_explicit_busy_raises_port_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: False)
    with pytest.raises(ssh_tunnel._TunnelError) as exc:
        ssh_tunnel._alloc_local_port(LOCAL_PORT_A)
    assert exc.value.failure_class == ssh_tunnel.FAIL_PORT_BUSY
    assert exc.value.exit_code == EXIT_PORT_BUSY


def test_alloc_pool_first_attempt_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)
    monkeypatch.setattr(ssh_tunnel.random, "randint", lambda _a, _b: RANDOM_POOL_PORT)
    assert ssh_tunnel._alloc_local_port(None) == RANDOM_POOL_PORT


def test_alloc_pool_exhausted_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: False)
    monkeypatch.setattr(ssh_tunnel.random, "randint", lambda _a, _b: RANDOM_POOL_PORT)
    with pytest.raises(ssh_tunnel._TunnelError) as exc:
        ssh_tunnel._alloc_local_port(None)
    assert exc.value.failure_class == ssh_tunnel.FAIL_PORT_POOL_EXHAUSTED
    assert exc.value.exit_code == EXIT_PORT_POOL_EXHAUSTED


def test_port_free_uses_socket_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_port_free` returns False when bind raises OSError."""
    class _S:
        def __enter__(self) -> _S:
            return self
        def __exit__(self, *exc: Any) -> None:
            return None
        def bind(self, _addr: tuple[str, int]) -> None:
            raise OSError("in use")
    monkeypatch.setattr(ssh_tunnel.socket, "socket", lambda *_a, **_kw: _S())
    assert ssh_tunnel._port_free(LOCAL_PORT_A) is False


def test_port_free_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _S:
        def __enter__(self) -> _S:
            return self
        def __exit__(self, *exc: Any) -> None:
            return None
        def bind(self, _addr: tuple[str, int]) -> None:
            return None
    monkeypatch.setattr(ssh_tunnel.socket, "socket", lambda *_a, **_kw: _S())
    assert ssh_tunnel._port_free(LOCAL_PORT_A) is True


# -----------------------------------------------------------------------------
# ssh argv + detach helpers
# -----------------------------------------------------------------------------


def test_build_ssh_argv_contains_expected_options() -> None:
    argv = ssh_tunnel._build_ssh_argv(
        "pai", LOCAL_PORT_A, "127.0.0.1", REMOTE_PORT, "127.0.0.1", 8,
    )
    assert argv[0] == "ssh"
    assert "-N" in argv
    assert "-L" in argv
    li = argv.index("-L")
    assert argv[li + 1] == f"127.0.0.1:{LOCAL_PORT_A}:127.0.0.1:{REMOTE_PORT}"
    joined = " ".join(argv)
    assert "ExitOnForwardFailure=yes" in joined
    assert "BatchMode=yes" in joined
    assert "ConnectTimeout=8" in joined
    assert argv[-1] == "pai"


def test_classify_start_failure_network() -> None:
    fc, ec = ssh_tunnel._classify_start_failure(
        "ssh: Could not resolve hostname nope: nodename"
    )
    assert fc == ssh_tunnel.FAIL_SSH_CONNECT_FAILED
    assert ec == EXIT_SSH_CONNECT_FAILED


def test_classify_start_failure_auth() -> None:
    fc, ec = ssh_tunnel._classify_start_failure(
        "Permission denied (publickey).\r\n"
    )
    assert fc == ssh_tunnel.FAIL_AUTH
    assert ec == EXIT_AUTH


def test_classify_start_failure_forward_setup() -> None:
    fc, ec = ssh_tunnel._classify_start_failure(
        "bind [127.0.0.1]:1234: Address already in use\r\n"
    )
    assert fc == ssh_tunnel.FAIL_FORWARD_SETUP_FAILED
    assert ec == EXIT_FORWARD_SETUP_FAILED


def test_wait_for_forward_dies_within_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakePopen(poll_sequence=[None, 255], stderr_bytes=b"oops\n")
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    # Make time advance fast: each call adds 0.5s
    counter = {"v": 0.0}
    def _mono() -> float:
        counter["v"] += 0.1
        return counter["v"]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _mono)
    alive, stderr = ssh_tunnel._wait_for_forward(proc, 2)
    assert alive is False
    assert "oops" in stderr


def test_wait_for_forward_alive_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakePopen(poll_sequence=[None] * 100, stderr_bytes=b"")
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    # Pin time so deadline expires quickly.
    counter = {"v": 0.0}
    def _mono() -> float:
        counter["v"] += 0.5
        return counter["v"]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _mono)
    alive, stderr = ssh_tunnel._wait_for_forward(proc, 1)
    assert alive is True
    assert stderr == ""
    proc.stderr.close.assert_called_once()


# -----------------------------------------------------------------------------
# _kill_with_grace
# -----------------------------------------------------------------------------


def test_kill_with_grace_stale_when_already_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: False)
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "stale"


def test_kill_with_grace_killed_on_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"alive": True}
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: state["alive"])
    def _kill(_pid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            state["alive"] = False
    monkeypatch.setattr(ssh_tunnel.os, "kill", _kill)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    fake_now = [0.0]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: fake_now[0])
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "killed"


def test_kill_with_grace_killed_on_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM doesn't kill; SIGKILL does."""
    state = {"alive": True}
    sig_seen: list[int] = []

    def _alive(_p: int) -> bool:
        return state["alive"]
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", _alive)

    def _kill(_pid: int, sig: int) -> None:
        sig_seen.append(sig)
        if sig == signal.SIGKILL:
            state["alive"] = False
    monkeypatch.setattr(ssh_tunnel.os, "kill", _kill)

    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    # Step monotonic forward in big jumps so the SIGTERM grace loop expires
    # quickly (forces fall-through to SIGKILL), but the final loop after
    # SIGKILL still sees alive=False on its first poll.
    timeline = iter([0.0, 100.0, 200.0, 200.0, 200.0])
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: next(timeline))
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "killed"
    assert signal.SIGKILL in sig_seen


def test_kill_with_grace_fail_on_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError()
    monkeypatch.setattr(ssh_tunnel.os, "kill", _kill)
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "kill_failed"


def test_kill_with_grace_dies_between_check_and_term(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"alive": True}
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: state["alive"])
    def _kill(_pid: int, _sig: int) -> None:
        # Process died between _pid_alive() and the syscall
        raise ProcessLookupError()
    monkeypatch.setattr(ssh_tunnel.os, "kill", _kill)
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "stale"


# -----------------------------------------------------------------------------
# _cmd_start
# -----------------------------------------------------------------------------


def test_start_refuses_on_non_posix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_posix", lambda: False)
    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT), "--json"],
        capsys,
    )
    assert rc == EXIT_POSIX_ONLY
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_POSIX_ONLY


def test_start_port_busy_explicit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: False)
    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT),
         "--local-port", str(LOCAL_PORT_A), "--json"],
        capsys,
    )
    assert rc == EXIT_PORT_BUSY
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_PORT_BUSY


def test_start_pool_exhausted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: False)
    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT), "--json"],
        capsys,
    )
    assert rc == EXIT_PORT_POOL_EXHAUSTED
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_PORT_POOL_EXHAUSTED


def test_start_ssh_connect_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(
            poll_sequence=[255],
            stderr_bytes=b"ssh: Could not resolve hostname pai: nodename\n",
        )
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)

    counter = {"v": 0.0}
    def _mono() -> float:
        counter["v"] += 0.1
        return counter["v"]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _mono)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)

    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT), "--json"],
        capsys,
    )
    assert rc == EXIT_SSH_CONNECT_FAILED
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_SSH_CONNECT_FAILED


def test_start_auth_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(
            poll_sequence=[255],
            stderr_bytes=b"Permission denied (publickey).\n",
        )
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    def _mono() -> float:
        counter["v"] += 0.1
        return counter["v"]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _mono)

    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT), "--json"],
        capsys,
    )
    assert rc == EXIT_AUTH
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_AUTH


def test_start_forward_setup_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(
            poll_sequence=[255],
            stderr_bytes=b"bind [127.0.0.1]:1234: Address already in use\n",
        )
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    def _mono() -> float:
        counter["v"] += 0.1
        return counter["v"]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _mono)

    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT), "--json"],
        capsys,
    )
    assert rc == EXIT_FORWARD_SETUP_FAILED
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_FORWARD_SETUP_FAILED


def test_start_success_writes_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None, _isolated_state_dir: Path,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(poll_sequence=[None] * 100)
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    def _mono() -> float:
        counter["v"] += 0.5
        return counter["v"]
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _mono)

    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT),
         "--local-port", str(LOCAL_PORT_A), "--json"],
        capsys,
    )
    assert rc == EXIT_OK
    assert env["success"] is True
    assert env["data"]["alias"] == "pai"
    assert env["data"]["local_port"] == LOCAL_PORT_A
    assert env["data"]["remote_port"] == REMOTE_PORT
    assert env["data"]["pid"] == DUMMY_PID
    # State file actually got written under our tmp dir
    state_path = Path(env["data"]["state_file"])
    assert state_path.exists()


def test_start_idle_timeout_marker_when_user_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(poll_sequence=[None] * 100)
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: counter.__setitem__("v", counter["v"] + 0.5) or counter["v"])
    # _idle_timeout_user_set inspects sys.argv directly; patch it.
    monkeypatch.setattr(sys, "argv", ["prog", "start", "pai", "--remote-port", "22", "--idle-timeout", "30"])
    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT),
         "--local-port", str(LOCAL_PORT_A), "--idle-timeout", "30", "--json"],
        capsys,
    )
    assert rc == EXIT_OK
    assert env["data"].get("flag_deferred") == ["--idle-timeout"]


def test_start_no_idle_marker_when_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        return _FakePopen(poll_sequence=[None] * 100)
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: counter.__setitem__("v", counter["v"] + 0.5) or counter["v"])
    monkeypatch.setattr(sys, "argv", ["prog", "start", "pai", "--remote-port", "22"])
    rc, env = _run_main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT),
         "--local-port", str(LOCAL_PORT_A), "--json"],
        capsys,
    )
    assert rc == EXIT_OK
    assert "flag_deferred" not in env["data"]


# -----------------------------------------------------------------------------
# _cmd_start --no-detach
# -----------------------------------------------------------------------------


def test_start_no_detach_runs_foreground(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)
    spawned: list[dict[str, Any]] = []

    class _FgPopen:
        def __init__(self, *args: Any, **kw: Any) -> None:
            spawned.append({"argv": args[0], "start_new_session": kw.get("start_new_session", False)})
            self.pid = DUMMY_PID

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _FgPopen)
    rc = ssh_tunnel.main(
        ["start", "pai", "--remote-port", str(REMOTE_PORT),
         "--local-port", str(LOCAL_PORT_A), "--no-detach", "--json"],
    )
    assert rc == EXIT_OK
    # In --no-detach we do NOT pass start_new_session=True
    assert spawned and spawned[0]["start_new_session"] is False


# -----------------------------------------------------------------------------
# _cmd_stop
# -----------------------------------------------------------------------------


def _seed_state(
    *,
    alias: str = "pai",
    local_port: int = LOCAL_PORT_A,
    pid: int = DUMMY_PID,
) -> ssh_tunnel._StateEntry:
    entry = ssh_tunnel._StateEntry(
        pid=pid, local_port=local_port, remote_host="127.0.0.1",
        remote_port=REMOTE_PORT, bind_address="127.0.0.1", alias=alias,
        started_at="2026-05-26T00:00:00+00:00",
    )
    ssh_tunnel._write_state(entry)
    return entry


def test_stop_not_found_returns_36(capsys: pytest.CaptureFixture[str]) -> None:
    rc, env = _run_main(["stop", "pai", "--json"], capsys)
    assert rc == EXIT_TUNNEL_NOT_FOUND
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_TUNNEL_NOT_FOUND


def test_stop_single_kills_and_removes_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    e = _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "killed")
    rc, env = _run_main(["stop", "pai", "--port", str(LOCAL_PORT_A), "--json"], capsys)
    assert rc == EXIT_OK
    assert env["data"]["ports_stopped"] == [LOCAL_PORT_A]
    assert str(e.path) in env["data"]["state_files_removed"]
    assert not e.path.exists()


def test_stop_ambiguous_when_no_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state(local_port=LOCAL_PORT_A)
    _seed_state(local_port=LOCAL_PORT_B, pid=ANOTHER_PID)
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "killed")
    rc, env = _run_main(["stop", "pai", "--json"], capsys)
    assert rc == EXIT_AMBIGUOUS_STOP
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_AMBIGUOUS_STOP
    assert set(env["data"]["ports_present"]) == {LOCAL_PORT_A, LOCAL_PORT_B}


def test_stop_with_port_selects_one_of_many(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state(local_port=LOCAL_PORT_A)
    _seed_state(local_port=LOCAL_PORT_B, pid=ANOTHER_PID)
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "killed")
    rc, env = _run_main(
        ["stop", "pai", "--port", str(LOCAL_PORT_A), "--json"], capsys,
    )
    assert rc == EXIT_OK
    assert env["data"]["ports_stopped"] == [LOCAL_PORT_A]
    # The other tunnel's state survives.
    other = ssh_tunnel._state_path("pai", LOCAL_PORT_B)
    assert other.exists()


def test_stop_stale_state_only_returns_38(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "stale")
    rc, env = _run_main(["stop", "pai", "--port", str(LOCAL_PORT_A), "--json"], capsys)
    assert rc == EXIT_STALE_STATE_ONLY
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_STALE_STATE_ONLY


def test_stop_kill_failed_returns_39(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "kill_failed")
    rc, env = _run_main(["stop", "pai", "--port", str(LOCAL_PORT_A), "--json"], capsys)
    assert rc == EXIT_KILL_FAILED
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_KILL_FAILED


# -----------------------------------------------------------------------------
# _cmd_status / _cmd_list
# -----------------------------------------------------------------------------


def test_status_empty_returns_empty_forwards(capsys: pytest.CaptureFixture[str]) -> None:
    rc, env = _run_main(["status", "pai", "--json"], capsys)
    assert rc == EXIT_OK
    assert env["data"]["forwards"] == []


def test_status_lists_only_matching_alias(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state(alias="pai", local_port=LOCAL_PORT_A)
    _seed_state(alias="other", local_port=LOCAL_PORT_B, pid=ANOTHER_PID)
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    rc, env = _run_main(["status", "pai", "--json"], capsys)
    assert rc == EXIT_OK
    aliases = [f["alias"] for f in env["data"]["forwards"]]
    assert aliases == ["pai"]
    assert env["data"]["forwards"][0]["running"] is True


def test_list_returns_all_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state(alias="pai", local_port=LOCAL_PORT_A)
    _seed_state(alias="other", local_port=LOCAL_PORT_B, pid=ANOTHER_PID)
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: False)
    rc, env = _run_main(["list", "--json"], capsys)
    assert rc == EXIT_OK
    aliases = sorted(f["alias"] for f in env["data"]["forwards"])
    assert aliases == ["other", "pai"]
    for f in env["data"]["forwards"]:
        assert f["running"] is False


def test_describe_includes_state_file_and_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e = _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    d = ssh_tunnel._describe(e)
    assert d["state_file"] == str(e.path)
    assert d["running"] is True
    assert d["alias"] == "pai"


# -----------------------------------------------------------------------------
# main() error paths
# -----------------------------------------------------------------------------


def test_main_unexpected_exception_caught(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def _boom(_args: Any) -> int:
        raise RuntimeError("kaboom")
    monkeypatch.setattr(ssh_tunnel, "_cmd_list", _boom)
    rc, env = _run_main(["list", "--json"], capsys)
    assert rc == EXIT_GENERIC
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_REMOTE_ERROR


def test_main_unhandled_tunnel_error_caught(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A _TunnelError that escapes a subcommand body still produces the envelope."""
    def _boom(_args: Any) -> int:
        raise ssh_tunnel._TunnelError(ssh_tunnel.FAIL_REMOTE_ERROR, EXIT_GENERIC, "escape")
    monkeypatch.setattr(ssh_tunnel, "_cmd_list", _boom)
    rc, env = _run_main(["list", "--json"], capsys)
    assert rc == EXIT_GENERIC
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_REMOTE_ERROR


# -----------------------------------------------------------------------------
# Misc helpers
# -----------------------------------------------------------------------------


def test_emit_returns_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    env = ssh_tunnel.json_result(success=True, exit_code=0, data={"failure_class": None})
    rc = ssh_tunnel._emit(env, as_json=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out)["success"] is True


def test_hint_line_appends_newline() -> None:
    assert ssh_tunnel._hint_line("foo") == "foo\n"
    assert ssh_tunnel._hint_line("foo\n") == "foo\n"
    assert ssh_tunnel._hint_line("") == ""


def test_terminate_proc_handles_already_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakePopen(poll_sequence=[0])
    # terminate/wait raise (already gone) — _terminate_proc must swallow.
    def _bad_terminate() -> None:
        raise ProcessLookupError()
    proc.terminate = _bad_terminate  # type: ignore[method-assign]
    def _bad_wait(timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=2)
    proc.wait = _bad_wait  # type: ignore[method-assign]
    # Should not raise
    ssh_tunnel._terminate_proc(proc)  # type: ignore[arg-type]


def test_idle_timeout_user_set_inspects_sys_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "start", "x", "--idle-timeout", "1"])
    assert ssh_tunnel._idle_timeout_user_set() is True
    monkeypatch.setattr(sys, "argv", ["prog", "start", "x", "--idle-timeout=60"])
    assert ssh_tunnel._idle_timeout_user_set() is True
    monkeypatch.setattr(sys, "argv", ["prog", "start", "x"])
    assert ssh_tunnel._idle_timeout_user_set() is False


def test_posix_truthiness(monkeypatch: pytest.MonkeyPatch) -> None:
    # On the CI host this is True; flip platform to verify the gate.
    monkeypatch.setattr(ssh_tunnel.sys, "platform", "win32")
    assert ssh_tunnel._posix() is False
    monkeypatch.setattr(ssh_tunnel.sys, "platform", "linux")
    monkeypatch.setattr(ssh_tunnel.os, "setsid", lambda: None, raising=False)
    assert ssh_tunnel._posix() is True


def test_spawn_ssh_passes_start_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _FakePopenCls:
        def __init__(self, argv: list[str], **kw: Any) -> None:
            seen["argv"] = argv
            seen["kwargs"] = kw
            self.pid = DUMMY_PID

    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _FakePopenCls)
    proc = ssh_tunnel._spawn_ssh(["ssh", "-N"], detach=True)
    assert seen["argv"] == ["ssh", "-N"]
    assert seen["kwargs"].get("start_new_session") is True
    assert proc.pid == DUMMY_PID


def test_spawn_ssh_no_detach_omits_start_new_session(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _FakePopenCls:
        def __init__(self, _argv: list[str], **kw: Any) -> None:
            seen["kwargs"] = kw
            self.pid = DUMMY_PID

    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _FakePopenCls)
    ssh_tunnel._spawn_ssh(["ssh"], detach=False)
    assert "start_new_session" not in seen["kwargs"]


# -----------------------------------------------------------------------------
# Mode resolution / argv expectations
# -----------------------------------------------------------------------------


def test_start_argv_includes_bind_and_remote_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)
    captured: list[list[str]] = []

    def fake_spawn(argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        captured.append(argv)
        return _FakePopen(poll_sequence=[None] * 100)

    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: counter.__setitem__("v", counter["v"] + 0.5) or counter["v"])

    rc, env = _run_main(
        ["start", "pai", "--remote-port", "3306", "--local-port", "13306",
         "--remote-host", "10.0.0.5", "--bind-address", "0.0.0.0",
         "--connect-timeout", "12", "--json"],
        capsys,
    )
    assert rc == EXIT_OK
    joined = " ".join(captured[0])
    assert "0.0.0.0:13306:10.0.0.5:3306" in joined
    assert "ConnectTimeout=12" in joined


# -----------------------------------------------------------------------------
# Extra coverage targets
# -----------------------------------------------------------------------------


def test_start_spawn_oserror_returns_remote_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def _spawn_raises(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        raise OSError("execv: no such file")
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", _spawn_raises)
    rc, env = _run_main(
        ["start", "pai", "--remote-port", "22",
         "--local-port", str(LOCAL_PORT_A), "--json"],
        capsys,
    )
    assert rc == EXIT_GENERIC
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_REMOTE_ERROR


def test_start_state_write_failure_tears_down(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _fake_posix: None,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    proc_seen: list[Any] = []

    def fake_spawn(_argv: list[str], *, detach: bool) -> _FakePopen:  # noqa: ARG001
        p = _FakePopen(poll_sequence=[None] * 100)
        proc_seen.append(p)
        return p
    monkeypatch.setattr(ssh_tunnel, "_spawn_ssh", fake_spawn)

    def fake_write(_e: ssh_tunnel._StateEntry) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(ssh_tunnel, "_write_state", fake_write)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: counter.__setitem__("v", counter["v"] + 0.5) or counter["v"])

    rc, env = _run_main(
        ["start", "pai", "--remote-port", "22",
         "--local-port", str(LOCAL_PORT_A), "--json"],
        capsys,
    )
    assert rc == EXIT_GENERIC
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_REMOTE_ERROR
    # Verify the spawned process was torn down.
    assert proc_seen and (proc_seen[0]._terminated or proc_seen[0]._killed)


def test_start_no_detach_port_busy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: False)
    rc, env = _run_main(
        ["start", "pai", "--remote-port", "22",
         "--local-port", str(LOCAL_PORT_A), "--no-detach", "--json"],
        capsys,
    )
    assert rc == EXIT_PORT_BUSY
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_PORT_BUSY


def test_start_no_detach_spawn_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    def _bad(_argv: list[str], **_kw: Any) -> None:
        raise OSError("ssh not found")
    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _bad)

    rc = ssh_tunnel.main(
        ["start", "pai", "--remote-port", "22",
         "--local-port", str(LOCAL_PORT_A), "--no-detach"],
    )
    # In foreground mode + no JSON envelope, spawn failure exits 1.
    assert rc == EXIT_GENERIC


def test_start_no_detach_ctrl_c_returns_130(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda _p: True)

    class _InterruptingPopen:
        def __init__(self, *args: Any, **kw: Any) -> None:
            self.pid = DUMMY_PID

        def wait(self) -> int:
            raise KeyboardInterrupt()

        def terminate(self) -> None:
            return None

    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _InterruptingPopen)
    rc = ssh_tunnel.main(
        ["start", "pai", "--remote-port", "22",
         "--local-port", str(LOCAL_PORT_A), "--no-detach"],
    )
    assert rc == KEYBOARD_INTERRUPT_EXIT


def test_kill_with_grace_sigkill_already_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """After SIGTERM grace, SIGKILL raises ProcessLookupError -> killed."""
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    call_count = {"n": 0}

    def _kill(_pid: int, sig: int) -> None:
        call_count["n"] += 1
        if sig == signal.SIGKILL:
            raise ProcessLookupError()
        # SIGTERM succeeds but pid stays "alive" so we fall through.

    monkeypatch.setattr(ssh_tunnel.os, "kill", _kill)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    timeline = iter([0.0, 100.0])
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: next(timeline))
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "killed"


def test_kill_with_grace_sigkill_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)

    def _kill(_pid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            raise PermissionError()
        # SIGTERM allowed

    monkeypatch.setattr(ssh_tunnel.os, "kill", _kill)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    timeline = iter([0.0, 100.0])
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: next(timeline))
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "kill_failed"


def test_kill_with_grace_sigkill_no_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both SIGTERM and SIGKILL ran but pid keeps running -> kill_failed."""
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    monkeypatch.setattr(ssh_tunnel.os, "kill", lambda _p, _s: None)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    timeline = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: next(timeline))
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "kill_failed"


def test_wait_for_forward_stderr_read_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: stderr.read() raising OSError still classifies cleanly."""
    proc = _FakePopen(poll_sequence=[1])
    proc.stderr.read.side_effect = OSError("EBADF")
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    counter = {"v": 0.0}
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: counter.__setitem__("v", counter["v"] + 0.1) or counter["v"])
    alive, stderr = ssh_tunnel._wait_for_forward(proc, 2)
    assert alive is False
    assert stderr == ""
