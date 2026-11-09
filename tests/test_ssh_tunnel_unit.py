"""Local unit tests for ``ssh_tunnel.py`` (no ssh, no network, no host).

Covers the parts of the 1.4.0 rewrite that had **no** local coverage after the
standalone-sync commit deleted the v1.0 unit suite:

* ``_kill_with_grace`` — every documented outcome
* ``_alloc_local_port`` / ``_port_free`` — the port allocator
* ``_start_watchdog`` — the detached idle watchdog introduced in 1.4.0
* state-file primitives, ssh argv construction, failure classification
* ``stop`` / ``status`` / ``list`` envelopes and their exit codes

The integration counterpart is ``tests/test_ssh_tunnel_integration.py``
(marker ``live_ssh``), which still needs a real host.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.ssh_core.helpers import load_script  # noqa: E402

ssh_tunnel = load_script("ssh_tunnel")

from lib import exit_codes  # noqa: E402

EXIT_OK = 0
EXIT_PORT_BUSY = exit_codes.EXIT_PORT_BUSY
EXIT_PORT_POOL_EXHAUSTED = exit_codes.EXIT_PORT_POOL_EXHAUSTED
EXIT_TUNNEL_NOT_FOUND = exit_codes.EXIT_TUNNEL_NOT_FOUND
EXIT_AMBIGUOUS_STOP = exit_codes.EXIT_AMBIGUOUS_STOP
EXIT_STALE_STATE_ONLY = exit_codes.EXIT_STALE_STATE_ONLY
EXIT_KILL_FAILED = exit_codes.EXIT_KILL_FAILED

DUMMY_PID = 4242
ANOTHER_PID = 4243
REMOTE_PORT = 22
LOCAL_PORT_A = 13306
LOCAL_PORT_B = 13307
RANDOM_POOL_PORT = 15555
CONNECT_TIMEOUT_USER_SET = 8
IDLE_TIMEOUT_USER_SET = 30


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep every state file inside tmp_path."""
    monkeypatch.setattr(ssh_tunnel.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / "ssh_tunnel"


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)


class _FakePopen:
    """Stand-in for ``subprocess.Popen[bytes]`` with a scripted lifecycle."""

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
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self) -> int | None:
        if self._poll_idx >= len(self._poll_sequence):
            return self._poll_sequence[-1]
        rc = self._poll_sequence[self._poll_idx]
        self._poll_idx += 1
        return rc

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def _run_main(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    rc = ssh_tunnel.main(argv)
    out = capsys.readouterr().out
    if not out.strip():
        return rc, {}
    return rc, json.loads(out)


def _entry(**overrides: Any) -> Any:
    base = {
        "pid": DUMMY_PID,
        "local_port": LOCAL_PORT_A,
        "remote_host": "127.0.0.1",
        "remote_port": REMOTE_PORT,
        "bind_address": "127.0.0.1",
        "alias": "pai",
        "started_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return ssh_tunnel._StateEntry(**base)


# -----------------------------------------------------------------------------
# state-file primitives
# -----------------------------------------------------------------------------


def test_state_path_is_deterministic_per_alias_type_port() -> None:
    a = ssh_tunnel._state_path("pai", LOCAL_PORT_A)
    b = ssh_tunnel._state_path("pai", LOCAL_PORT_A)
    c = ssh_tunnel._state_path("pai", LOCAL_PORT_B)
    d = ssh_tunnel._state_path("other", LOCAL_PORT_A)
    assert a == b
    assert a != c
    assert a != d


def test_write_and_read_state_roundtrip() -> None:
    entry = _entry()
    ssh_tunnel._write_state(entry)
    back = ssh_tunnel._read_state(entry.path)
    assert back is not None
    assert back.pid == entry.pid
    assert back.local_port == entry.local_port
    assert back.alias == entry.alias
    assert back.remote_port == entry.remote_port


def test_read_state_corrupt_returns_none(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ssh_tunnel._read_state(bad) is None


def test_read_state_missing_field_returns_none(tmp_path: Path) -> None:
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"pid": 1}), encoding="utf-8")
    assert ssh_tunnel._read_state(partial) is None


def test_scan_state_reads_only_parsable_files() -> None:
    ssh_tunnel._write_state(_entry(alias="pai", local_port=LOCAL_PORT_A))
    ssh_tunnel._write_state(_entry(alias="web-1", local_port=LOCAL_PORT_B))
    (ssh_tunnel._state_dir() / "broken.json").write_text("nope", encoding="utf-8")
    entries = ssh_tunnel._scan_state()
    assert {e.alias for e in entries} == {"pai", "web-1"}


# -----------------------------------------------------------------------------
# port allocator
# -----------------------------------------------------------------------------


def test_alloc_explicit_free_port_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda *_a, **_kw: True)
    assert ssh_tunnel._alloc_local_port(LOCAL_PORT_A) == LOCAL_PORT_A


def test_alloc_explicit_busy_raises_port_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda *_a, **_kw: False)
    with pytest.raises(ssh_tunnel._TunnelError) as exc:
        ssh_tunnel._alloc_local_port(LOCAL_PORT_A)
    assert exc.value.failure_class == ssh_tunnel.FAIL_PORT_BUSY
    assert exc.value.exit_code == EXIT_PORT_BUSY


def test_alloc_pool_first_attempt_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda *_a, **_kw: True)
    monkeypatch.setattr(ssh_tunnel.random, "randint", lambda _a, _b: RANDOM_POOL_PORT)
    assert ssh_tunnel._alloc_local_port(None) == RANDOM_POOL_PORT


def test_alloc_pool_exhausted_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_port_free", lambda *_a, **_kw: False)
    monkeypatch.setattr(ssh_tunnel.random, "randint", lambda _a, _b: RANDOM_POOL_PORT)
    with pytest.raises(ssh_tunnel._TunnelError) as exc:
        ssh_tunnel._alloc_local_port(None)
    assert exc.value.failure_class == ssh_tunnel.FAIL_PORT_POOL_EXHAUSTED
    assert exc.value.exit_code == EXIT_PORT_POOL_EXHAUSTED


def test_port_free_is_false_when_bind_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Sock:
        def __enter__(self) -> _Sock:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def bind(self, _addr: tuple[str, int]) -> None:
            raise OSError("in use")

    monkeypatch.setattr(ssh_tunnel.socket, "socket", lambda *_a, **_kw: _Sock())
    assert ssh_tunnel._port_free(LOCAL_PORT_A) is False


def test_port_free_is_true_when_bind_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Sock:
        def __enter__(self) -> _Sock:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def bind(self, _addr: tuple[str, int]) -> None:
            return None

    monkeypatch.setattr(ssh_tunnel.socket, "socket", lambda *_a, **_kw: _Sock())
    assert ssh_tunnel._port_free(LOCAL_PORT_A) is True


# -----------------------------------------------------------------------------
# _kill_with_grace — the 1.4.0 rewrite had no local coverage at all
#
# 1.4.0 goes through ``lib.platform.terminate`` (which hides SIGTERM vs
# TerminateProcess), not through ``os.kill``. The tests therefore patch
# ``ssh_tunnel.terminate`` — patching ``os.kill`` would silently do nothing and
# the grace loop would spin to its real deadline.
# -----------------------------------------------------------------------------


def test_kill_with_grace_stale_when_already_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: False)
    monkeypatch.setattr(
        ssh_tunnel, "terminate", lambda *_a, **_kw: pytest.fail("must not signal a dead pid")
    )
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "stale"


def test_kill_with_grace_killed_on_first_signal(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: None,
) -> None:
    state = {"alive": True}
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: state["alive"])
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: 0.0)

    def _terminate(_pid: int, *, force: bool = False) -> bool:
        assert force is False
        state["alive"] = False
        return True

    monkeypatch.setattr(ssh_tunnel, "terminate", _terminate)
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "killed"


def test_kill_with_grace_escalates_to_force(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: None,
) -> None:
    """A tunnel that ignores the polite signal is force-killed."""
    state = {"alive": True}
    forces: list[bool] = []
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: state["alive"])

    def _terminate(_pid: int, *, force: bool = False) -> bool:
        forces.append(force)
        if force:
            state["alive"] = False
        return True

    monkeypatch.setattr(ssh_tunnel, "terminate", _terminate)

    # One monotonic read per loop iteration, +0.25 s each: the 3 s SIGTERM
    # window expires, the force branch runs, and the first poll of the final
    # 1 s window sees the pid gone.
    clock = {"now": 0.0}

    def _monotonic() -> float:
        clock["now"] += 0.25
        return clock["now"]

    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _monotonic)
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "killed"
    assert True in forces


def test_kill_with_grace_reports_kill_failed(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: None,
) -> None:
    """Signals are refused (permissions) and the pid stays alive."""
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    monkeypatch.setattr(ssh_tunnel, "terminate", lambda *_a, **_kw: False)
    timeline = iter([0.0, 100.0, 200.0, 300.0, 400.0, 500.0])
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: next(timeline, 500.0))
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "kill_failed"


def test_kill_with_grace_process_vanished_before_the_signal(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: None,
) -> None:
    """The pid was gone by the time the signal landed: that is still success.

    ``lib.platform.terminate`` swallows ``ProcessLookupError`` and reports
    False, so the fast path is skipped; the grace loop then observes the dead
    pid and returns ``killed``. Either way the caller may remove the state file.
    """
    calls = {"n": 0}

    def _alive(_pid: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # alive for the pre-check, gone afterwards

    def _terminate(_pid: int, *, force: bool = False) -> bool:
        raise ProcessLookupError()

    monkeypatch.setattr(ssh_tunnel, "_pid_alive", _alive)
    monkeypatch.setattr(ssh_tunnel, "terminate", _terminate)
    monkeypatch.setattr(ssh_tunnel.time, "monotonic", lambda: 0.0)
    assert ssh_tunnel._kill_with_grace(DUMMY_PID) == "killed"


# -----------------------------------------------------------------------------
# _start_watchdog — new in 1.4.0
# -----------------------------------------------------------------------------


def test_start_watchdog_spawns_with_idle_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakePopen(pid=ANOTHER_PID)

    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _popen)
    entry = _entry(idle_timeout=IDLE_TIMEOUT_USER_SET)
    assert ssh_tunnel._start_watchdog(entry) is True
    assert entry.watchdog_pid == ANOTHER_PID
    argv = captured["argv"]
    assert argv[1].endswith("ssh_tunnel.py")
    assert argv[2] == "_watch"
    assert argv[3] == str(entry.path)
    assert str(IDLE_TIMEOUT_USER_SET) in argv


def test_start_watchdog_returns_false_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _popen(*_a, **_kw):
        raise OSError("nope")

    monkeypatch.setattr(ssh_tunnel.subprocess, "Popen", _popen)
    entry = _entry(idle_timeout=IDLE_TIMEOUT_USER_SET)
    assert ssh_tunnel._start_watchdog(entry) is False
    assert entry.watchdog_pid is None


# -----------------------------------------------------------------------------
# ssh argv + failure classification
# -----------------------------------------------------------------------------


def test_build_ssh_argv_local_forward() -> None:
    argv = ssh_tunnel._build_ssh_argv(
        "pai", LOCAL_PORT_A, "127.0.0.1", REMOTE_PORT, "127.0.0.1", CONNECT_TIMEOUT_USER_SET,
    )
    assert argv[0] == "ssh"
    assert "-N" in argv
    flag = argv.index("-L")
    assert argv[flag + 1] == f"127.0.0.1:{LOCAL_PORT_A}:127.0.0.1:{REMOTE_PORT}"
    joined = " ".join(argv)
    assert "ExitOnForwardFailure=yes" in joined
    assert "BatchMode=yes" in joined
    assert f"ConnectTimeout={CONNECT_TIMEOUT_USER_SET}" in joined
    assert argv[-1] == "pai"


def test_build_ssh_argv_remote_forward_swaps_ports() -> None:
    """-R listens on --remote-port and dials --local-port (the reverse of -L)."""
    argv = ssh_tunnel._build_ssh_argv(
        "pai", LOCAL_PORT_A, "127.0.0.1", REMOTE_PORT, "127.0.0.1", 8,
        tunnel_type="remote",
    )
    flag = argv.index("-R")
    assert argv[flag + 1] == f"127.0.0.1:{REMOTE_PORT}:127.0.0.1:{LOCAL_PORT_A}"


def test_build_ssh_argv_dynamic_uses_socks_flag() -> None:
    argv = ssh_tunnel._build_ssh_argv(
        "pai", LOCAL_PORT_A, "127.0.0.1", REMOTE_PORT, "127.0.0.1", 8,
        tunnel_type="dynamic",
    )
    flag = argv.index("-D")
    assert argv[flag + 1] == f"127.0.0.1:{LOCAL_PORT_A}"


def test_build_ssh_argv_password_auth_drops_batchmode() -> None:
    argv = ssh_tunnel._build_ssh_argv(
        "pai", LOCAL_PORT_A, "127.0.0.1", REMOTE_PORT, "127.0.0.1", 8,
        password_auth=True,
    )
    joined = " ".join(argv)
    assert "BatchMode=yes" not in joined
    assert "PreferredAuthentications=password" in joined


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ssh: connect to host pai port 22: Connection timed out", ssh_tunnel.FAIL_SSH_CONNECT_FAILED),
        ("Permission denied (publickey).", ssh_tunnel.FAIL_AUTH),
        ("remote port forwarding failed for listen port 22", ssh_tunnel.FAIL_FORWARD_SETUP_FAILED),
    ],
)
def test_classify_start_failure(stderr: str, expected: str) -> None:
    failure_class, exit_code = ssh_tunnel._classify_start_failure(stderr)
    assert failure_class == expected
    assert exit_code == ssh_tunnel._EXIT_CODES[expected]


# -----------------------------------------------------------------------------
# stop / status / list envelopes
# -----------------------------------------------------------------------------


def _seed_state(**overrides: Any) -> Any:
    entry = _entry(**overrides)
    ssh_tunnel._write_state(entry)
    return entry


def test_stop_without_forward_returns_36(capsys: pytest.CaptureFixture[str]) -> None:
    rc, env = _run_main(["stop", "pai", "--json"], capsys)
    assert rc == EXIT_TUNNEL_NOT_FOUND
    assert env["data"]["failure_class"] == ssh_tunnel.FAIL_TUNNEL_NOT_FOUND


def test_stop_ambiguous_when_port_not_given(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_state(local_port=LOCAL_PORT_A)
    _seed_state(local_port=LOCAL_PORT_B)
    rc, env = _run_main(["stop", "pai", "--json"], capsys)
    assert rc == EXIT_AMBIGUOUS_STOP
    assert set(env["data"]["ports_present"]) == {LOCAL_PORT_A, LOCAL_PORT_B}


def test_stop_with_port_selects_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state(local_port=LOCAL_PORT_A)
    survivor = _seed_state(local_port=LOCAL_PORT_B)
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "killed")
    rc, env = _run_main(["stop", "pai", "--port", str(LOCAL_PORT_A), "--json"], capsys)
    assert rc == EXIT_OK
    assert env["data"]["ports_stopped"] == [LOCAL_PORT_A]
    assert survivor.path.exists()


def test_stop_stale_state_only_returns_38(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "stale")
    rc, env = _run_main(["stop", "pai", "--json"], capsys)
    assert rc == EXIT_STALE_STATE_ONLY
    assert env["data"]["errors"][0]["failure_class"] == ssh_tunnel.FAIL_STALE_STATE_ONLY


def test_stop_kill_failed_returns_39(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "kill_failed")
    rc, env = _run_main(["stop", "pai", "--json"], capsys)
    assert rc == EXIT_KILL_FAILED
    assert env["data"]["errors"][0]["failure_class"] == ssh_tunnel.FAIL_KILL_FAILED


def test_status_empty_lists_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    rc, env = _run_main(["status", "pai", "--json"], capsys)
    assert rc == EXIT_OK
    assert env["data"]["forwards"] == []


def test_status_filters_by_alias(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_state(alias="pai", local_port=LOCAL_PORT_A)
    _seed_state(alias="web-1", local_port=LOCAL_PORT_B)
    rc, env = _run_main(["status", "pai", "--json"], capsys)
    assert rc == EXIT_OK
    assert [f["alias"] for f in env["data"]["forwards"]] == ["pai"]


def test_list_returns_every_entry(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_state(alias="pai", local_port=LOCAL_PORT_A)
    _seed_state(alias="web-1", local_port=LOCAL_PORT_B)
    rc, env = _run_main(["list", "--json"], capsys)
    assert rc == EXIT_OK
    assert {f["alias"] for f in env["data"]["forwards"]} == {"pai", "web-1"}


def test_describe_reports_state_file_and_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _seed_state()
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    described = ssh_tunnel._describe(entry)
    assert described["running"] is True
    assert described["state_file"] == str(entry.path)


# -----------------------------------------------------------------------------
# watchdog connection counting (feeds the idle decision, no ssh needed)
# -----------------------------------------------------------------------------


def test_connections_on_port_reads_the_socket_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_connections_on_port`` must never raise when the table is unreadable."""
    monkeypatch.setattr(ssh_tunnel, "_connections_windows", lambda _p: None)
    assert ssh_tunnel._connections_on_port(LOCAL_PORT_A) >= 0


def test_watchdog_stops_a_tunnel_after_the_idle_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``_cmd_watch`` removes the state file once the idle budget is spent."""
    entry = _seed_state(idle_timeout=1)
    monkeypatch.setattr(ssh_tunnel, "_connections_on_port", lambda _p: 0)
    monkeypatch.setattr(ssh_tunnel, "_remote_connections", lambda _e: 0)
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", lambda _p: True)
    monkeypatch.setattr(ssh_tunnel, "_kill_with_grace", lambda _p: "killed")
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ssh_tunnel, "WATCH_INTERVAL_S", 0.0)

    clock = {"now": 0.0}

    def _monotonic() -> float:
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _monotonic)
    rc = ssh_tunnel._cmd_watch(
        type("A", (), {"state_file": str(entry.path), "idle_timeout": 1, "json": True})()
    )
    assert rc == EXIT_OK
    assert not entry.path.exists()


def test_watchdog_leaves_an_active_tunnel_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Traffic on the forwarded port keeps the tunnel alive past the idle limit.

    The watchdog loop is ``while True``, so the case has to end the way a real
    one does: the tunnel process disappears (``stop`` ran, or ssh died) while
    the port was busy. The state file must still be there — the watchdog must
    not remove it on that path. Time is frozen inside the idle window so only
    the "busy port" branch can be reached; the loop then exits on ``pid_alive``.
    """
    entry = _seed_state(idle_timeout=1)
    timeout = 3600  # far beyond the frozen clock, so the idle branch is unreachable

    polls = {"n": 0}

    def _connections(_port: int) -> int:
        polls["n"] += 1
        return 1  # the forward is busy for the whole test

    def _alive(_pid: int) -> bool:
        # Four busy polls, then the tunnel is gone and the loop returns.
        return polls["n"] <= 4

    monkeypatch.setattr(ssh_tunnel, "_connections_on_port", _connections)
    monkeypatch.setattr(ssh_tunnel, "_remote_connections", lambda _e: 0)
    monkeypatch.setattr(ssh_tunnel, "_pid_alive", _alive)
    monkeypatch.setattr(ssh_tunnel, "WATCH_INTERVAL_S", 0.0)
    monkeypatch.setattr(ssh_tunnel.time, "sleep", lambda _s: None)

    clock = {"now": 0.0}

    def _monotonic() -> float:
        clock["now"] += 0.1
        return clock["now"]

    monkeypatch.setattr(ssh_tunnel.time, "monotonic", _monotonic)
    rc = ssh_tunnel._cmd_watch(
        type("A", (), {"state_file": str(entry.path), "idle_timeout": timeout, "json": True})()
    )
    assert rc == EXIT_OK
    assert polls["n"] >= 4
    assert entry.path.exists(), "watchdog removed the state file of a live tunnel"
