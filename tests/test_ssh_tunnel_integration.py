"""Integration tests for ssh_tunnel v1.0 — runs real ssh against POC_HOST.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_tunnel_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

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

import pytest

pytestmark = pytest.mark.live_ssh

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
POC_HOST = os.environ.get("POC_HOST", "pai")

EXIT_OK = 0
EXIT_TUNNEL_NOT_FOUND = 36

START_WAIT_BUDGET_S = 25
IDLE_TIMEOUT_GRACE_S = 10


# -----------------------------------------------------------------------------
# Skip guards (mirror test_ssh_upload_integration.py)
# -----------------------------------------------------------------------------


def _ssh_agent_has_key() -> bool:
    if shutil.which("ssh-add") is None:
        return False
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _host_reachable(host: str) -> bool:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


def _posix_ok() -> bool:
    return sys.platform != "win32" and hasattr(os, "setsid")


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    if not _posix_ok():
        pytest.skip("ssh_tunnel start is POSIX-only; skipping on this platform")
    if not _ssh_agent_has_key():
        pytest.skip("ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)")
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )


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
    """Stop without any active forward -> exit 36 tunnel_not_found."""
    _kill_alias_tunnels(POC_HOST)
    rc, env, _ = _run("stop", POC_HOST)
    assert rc == EXIT_TUNNEL_NOT_FOUND, env
    assert env["data"]["failure_class"] == "tunnel_not_found"


def test_idle_timeout_noop() -> None:
    """--idle-timeout is accepted, warns, but the tunnel survives past the value."""
    rc, env, stderr = _run(
        "start", POC_HOST, "--remote-port", "22",
        "--idle-timeout", "3",
        timeout=START_WAIT_BUDGET_S,
    )
    assert rc == EXIT_OK, env
    pid = env["data"]["pid"]
    lport = env["data"]["local_port"]
    state_file = Path(env["data"]["state_file"])
    try:
        # Wait longer than the user-supplied idle-timeout to prove it's a no-op
        time.sleep(IDLE_TIMEOUT_GRACE_S)
        # Tunnel must still be alive
        try:
            os.kill(pid, 0)
            still_alive = True
        except ProcessLookupError:
            still_alive = False
        assert still_alive, (
            "ssh_tunnel respected --idle-timeout (it should be a v1.0 no-op)"
        )
        assert _local_port_listening(lport)
        # The deferral marker is visible in data.flag_deferred
        assert env["data"].get("flag_deferred") == ["--idle-timeout"]
        # And in the stderr warning text
        assert "--idle-timeout is a no-op" in (env.get("stderr") or "")
    finally:
        # Belt-and-braces cleanup; the autouse fixture also runs.
        _run("stop", POC_HOST, "--port", str(lport))
        with contextlib.suppress(FileNotFoundError):
            state_file.unlink()
