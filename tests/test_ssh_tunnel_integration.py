"""Integration tests for ssh_tunnel v1.0 — runs real ssh against POC_HOST.

Markers: ``live_ssh`` + ``needs_posix`` — skipped by default. Run with::

    pytest tests/test_ssh_tunnel_integration.py -v -m live_ssh

The target host comes from ``POC_HOST`` (no committed default). See
``tests/README.md`` for prerequisites.

The whole module is POSIX-only, and the marker is module-level rather than
per-case because every case shares the same two facilities: detached tunnels
are started with ``setsid`` and stopped with ``os.killpg``/``SIGTERM``, neither
of which exists on Windows (``os.killpg`` is absent and ``os.kill(pid, 0)``
raises ``WinError 87``). Marking it here is what keeps a Windows run honest:
the module used to error in its cleanup fixture before any assertion ran
(GAP-002).

Cleanup is paramount: each test wraps its lifecycle in a try/finally
and an autouse fixture sweeps any leftover state files / detached
ssh processes for the POC_HOST alias after every test, so a crashing
case never leaks a tunnel.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import live_support  # noqa: E402
import pytest

# Read from the shipped table instead of copying a number: 1.4.0 renumbered the
# tunnel block from 36-39 to 126-129, which silently invalidated the literal.
# ``lib`` is pinned to ssh-core's own package by tests/conftest.py.
from lib import exit_codes as _codes  # noqa: E402
from live_support import require_live_ssh  # noqa: E402

pytestmark = [pytest.mark.live_ssh, pytest.mark.needs_posix]

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_TUNNEL = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_tunnel.py"
)
POC_HOST = live_support.poc_host()

EXIT_OK = _codes.EXIT_OK
EXIT_TUNNEL_NOT_FOUND = _codes.EXIT_TUNNEL_NOT_FOUND

START_WAIT_BUDGET_S = 25
IDLE_TIMEOUT_GRACE_S = 10


# -----------------------------------------------------------------------------
# Skip guards (mirror test_ssh_upload_integration.py)
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------


def _state_dir() -> Path:
    return Path(tempfile.gettempdir()) / "ssh_tunnel"


def _kill_alias_tunnels(alias: str) -> None:
    """Best-effort: SIGTERM every state-file PID matching `alias`, then drop the file.

    Also sweeps any leftover ``ssh -N -L ... <alias>`` processes the
    parent never persisted (e.g. ``--no-detach`` debug runs that were
    interrupted before exiting cleanly).
    """
    base = _state_dir()
    if base.exists():
        for p in list(base.glob("*.json")):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                with contextlib.suppress(OSError):
                    p.unlink()
                continue
            if raw.get("alias") != alias:
                continue
            pid = raw.get("pid")
            if isinstance(pid, int):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
                # Tiny grace; we don't want to actually wait long here.
                time.sleep(0.2)
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                p.unlink()

    # Belt-and-braces: any ``ssh -N -L ... <alias>`` still alive (e.g. from a
    # --no-detach test whose Python wrapper was killed before it could reap
    # its child). Using pgrep keeps us POSIX-only (already gated above).
    if shutil.which("pgrep") is None:
        return
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"ssh -N -L .* {alias}$"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return
    for line in out.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
        time.sleep(0.1)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture(autouse=True)
def _cleanup_tunnels() -> None:
    """Guarantee no leaked tunnel for POC_HOST after each test."""
    yield
    _kill_alias_tunnels(POC_HOST)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _run(*args: str, timeout: int = 30) -> tuple[int, dict, str]:
    """Invoke ssh_tunnel.py as a subprocess; return (rc, parsed_json, stderr)."""
    argv = [sys.executable, str(SSH_TUNNEL), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if not proc.stdout.strip():
        return proc.returncode, {}, proc.stderr
    try:
        return proc.returncode, json.loads(proc.stdout), proc.stderr
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_tunnel produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise  # unreachable


def _local_port_listening(port: int) -> bool:
    """Probe `(127.0.0.1, port)` with a TCP connect attempt."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _pid_alive(pid: int) -> bool:
    """Return True while ``pid`` is still a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


# -----------------------------------------------------------------------------
# Cases
# -----------------------------------------------------------------------------


def test_start_stop_roundtrip() -> None:
    """start -> local port listens -> stop -> port closes + state removed."""
    rc, env, _ = _run(
        "start", POC_HOST, "--remote-port", "22", timeout=START_WAIT_BUDGET_S,
    )
    assert rc == EXIT_OK, env
    assert env["success"] is True
    pid = env["data"]["pid"]
    lport = env["data"]["local_port"]
    state_file = Path(env["data"]["state_file"])
    try:
        assert _local_port_listening(lport), f"port {lport} should be listening"
        assert state_file.exists()
    finally:
        rc2, env2, _ = _run("stop", POC_HOST, "--port", str(lport))
        assert rc2 == EXIT_OK, env2
        # Port has closed, state file removed
        assert not state_file.exists()
        # Give the OS a moment to release the listener
        time.sleep(0.5)
        assert not _local_port_listening(lport)
        # Verify pid is no longer alive
        try:
            os.kill(pid, 0)
            still_alive = True
        except ProcessLookupError:
            still_alive = False
        assert still_alive is False


def test_no_detach_lifecycle() -> None:
    """--no-detach runs ssh in foreground; SIGINT returns non-zero and reaps ssh."""
    argv = [
        sys.executable, str(SSH_TUNNEL),
        "start", POC_HOST,
        "--remote-port", "22",
        "--no-detach",
    ]
    # ``start_new_session`` so we can signal the whole group and reap the ssh
    # grandchild — Python's default handlers for SIGTERM do not propagate.
    proc = subprocess.Popen(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        time.sleep(3)
        # Should still be running
        assert proc.poll() is None, "ssh exited too early in --no-detach"
        # SIGINT to the whole process group so both the Python wrapper AND
        # the ssh child receive it; the wrapper's KeyboardInterrupt handler
        # cleanly terminates ssh and returns 130.
        os.killpg(proc.pid, signal.SIGINT)
        rc = proc.wait(timeout=5)
        # KeyboardInterrupt path returns 130; ssh-killed-via-SIGINT path
        # often returns -2 (signal) — either is "non-zero with intent".
        assert rc != 0
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.kill()
            proc.wait()


def test_status_empty() -> None:
    """No tunnels for POC_HOST -> status returns empty forwards list."""
    # The autouse cleanup runs after this; we still verify it's clean now.
    _kill_alias_tunnels(POC_HOST)
    rc, env, _ = _run("status", POC_HOST)
    assert rc == EXIT_OK, env
    assert env["data"]["forwards"] == []


def test_stop_not_found() -> None:
    """Stop without any active forward -> 126 tunnel_not_found."""
    _kill_alias_tunnels(POC_HOST)
    rc, env, _ = _run("stop", POC_HOST)
    assert rc == EXIT_TUNNEL_NOT_FOUND, env
    assert env["data"]["failure_class"] == "tunnel_not_found"


def test_idle_timeout_is_honoured() -> None:
    """--idle-timeout is real: the watchdog stops an idle tunnel.

    1.4.0 replaced the old no-op flag with a detached watchdog that polls the
    OS socket table and stops the forward after N idle seconds. A 1 s timeout
    plus a grace window therefore ends the tunnel, which is the opposite of the
    v1.0 assertion this test used to make.
    """
    rc, env, stderr = _run(
        "start", POC_HOST, "--remote-port", "22",
        "--idle-timeout", "1",
        timeout=START_WAIT_BUDGET_S,
    )
    assert rc == EXIT_OK, env
    pid = env["data"]["pid"]
    lport = env["data"]["local_port"]
    state_file = Path(env["data"]["state_file"])
    assert env["data"].get("watchdog_pid"), "no idle watchdog was started"
    try:
        time.sleep(IDLE_TIMEOUT_GRACE_S)
        assert not _local_port_listening(lport), (
            "ssh_tunnel ignored --idle-timeout: the forward is still listening "
            f"{IDLE_TIMEOUT_GRACE_S}s after a 1s idle limit"
        )
        assert not _pid_alive(pid), f"tunnel pid {pid} survived the idle stop"
        # The state file is removed once the watchdog stops the tunnel.
        assert not state_file.exists(), "stale state file left after idle stop"
    finally:
        _run("stop", POC_HOST, "--port", str(lport))
        with contextlib.suppress(FileNotFoundError):
            state_file.unlink()
