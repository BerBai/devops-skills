"""Integration tests for ssh_server_transfer v1.0 — runs real SFTP to a host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_server_transfer_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

The PRD D7 (A) workaround applies: with only one POC_HOST, both
``src_alias`` and ``dst_alias`` resolve to the same host. Real
cross-host coverage waits for a second test target; the single-host
configuration still exercises the two-session lifecycle (paramiko
opens two distinct SFTP sessions onto separate TCP connections, well
under any default ``MaxSessions``).

Each test owns a unique ``/tmp/sst-*-<uuid>`` path namespace under the
remote host and cleans up after itself in a ``finally`` block.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_SERVER_TRANSFER = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_server_transfer.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

EXIT_OK = 0
EXIT_MODE_DEFERRED = 20
EXIT_FLAG_DEFERRED = 21

# Per-test fixture content (small files; we just care about correctness).
HELLO_BYTES = b"hello\n"
SUB_X_BYTES = b"x\n"
SUB_Y_BYTES = b"y\n"
TREE_FILE_COUNT = 2
TREE_TOTAL_BYTES = len(SUB_X_BYTES) + len(SUB_Y_BYTES)


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


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    if not _ssh_agent_has_key():
        pytest.skip("ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)")
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _run_envelope(*args: str, timeout: int = 60) -> tuple[dict, str]:
    """Invoke ssh_server_transfer.py as a subprocess; return (parsed_json, stderr)."""
    argv = [sys.executable, str(SSH_SERVER_TRANSFER), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False,
    )
    try:
        return json.loads(proc.stdout), proc.stderr
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_server_transfer produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def _ssh(
    host: str, command: str, timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, command],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _remote_cleanup(host: str, pattern: str) -> None:
    """Best-effort cleanup; failures don't fail the test."""
    _ssh(host, f"rm -rf {pattern}", timeout=10)


def _unique_pair(prefix: str = "sst") -> tuple[str, str]:
    """Return (src_path, dst_path) on POC_HOST with a unique random suffix."""
    tag = uuid.uuid4().hex[:8]
    return f"/tmp/{prefix}-src-{tag}", f"/tmp/{prefix}-dst-{tag}"


# -----------------------------------------------------------------------------
# Case 1 — --help smoke
# -----------------------------------------------------------------------------


def test_help_exits_zero() -> None:
    """`--help` exits 0 with usage line; no network touch needed."""
    proc = subprocess.run(
        [sys.executable, str(SSH_SERVER_TRANSFER), "--help"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert proc.returncode == 0
    assert "usage:" in proc.stdout.lower()
    for flag in ("--mode", "--use-rsync", "--recursive", "--bwlimit", "--json"):
        assert flag in proc.stdout


# -----------------------------------------------------------------------------
# Case 2 — --mode direct refuses without any network
# -----------------------------------------------------------------------------


def test_mode_direct_returns_mode_deferred() -> None:
    """--mode direct → exit 20 mode_deferred; no SSH handshake required."""
    env, _ = _run_envelope(
        POC_HOST, "/tmp/a", POC_HOST, "/tmp/b", "--mode", "direct",
    )
    assert env["success"] is False
    assert env["exit_code"] == EXIT_MODE_DEFERRED
    assert env["data"]["failure_class"] == "mode_deferred"


# -----------------------------------------------------------------------------
# Case 3 — --use-rsync refuses without any network
# -----------------------------------------------------------------------------


def test_use_rsync_returns_flag_deferred() -> None:
    """--use-rsync → exit 21 flag_deferred; no SSH handshake required."""
    env, _ = _run_envelope(
        POC_HOST, "/tmp/a", POC_HOST, "/tmp/b", "--use-rsync",
    )
    assert env["success"] is False
    assert env["exit_code"] == EXIT_FLAG_DEFERRED
    assert env["data"]["failure_class"] == "flag_deferred"


# -----------------------------------------------------------------------------
# Case 4 — single-file pai → pai round trip
# -----------------------------------------------------------------------------


def test_single_file_stream_pai_to_pai() -> None:
    """End-to-end: seed src, run stream, verify content arrived at dst."""
    src_path, dst_path = _unique_pair()
    try:
        # Seed src with known bytes; use printf for exact-byte control.
        seed = _ssh(POC_HOST, f"printf 'hello\\n' > {src_path}")
        assert seed.returncode == 0, seed.stderr

        env, _ = _run_envelope(POC_HOST, src_path, POC_HOST, dst_path)
        assert env["success"] is True, env
        assert env["exit_code"] == EXIT_OK
        assert env["data"]["mode_used"] == "stream"
        assert env["data"]["bytes"] == len(HELLO_BYTES)
        assert env["data"]["files_transferred"] == 1
        # default --mode is auto → marker present
        assert env["data"].get("auto_resolved_to") == "stream"

        # Verify dst contents byte-for-byte.
        verify = _ssh(POC_HOST, f"cat {dst_path}")
        assert verify.returncode == 0, verify.stderr
        assert verify.stdout == HELLO_BYTES.decode()
    finally:
        with contextlib.suppress(Exception):
            _remote_cleanup(POC_HOST, f"{src_path} {dst_path}")


# -----------------------------------------------------------------------------
# Case 5 — recursive tree pai → pai
# -----------------------------------------------------------------------------


def test_recursive_tree_pai_to_pai() -> None:
    """Recursive walk: 2 files across 2 dir levels arrive intact at dst."""
    src_path, dst_path = _unique_pair("sst-tree")
    try:
        setup = _ssh(
            POC_HOST,
            (
                f"mkdir -p {src_path}/a/b "
                f"&& printf 'x\\n' > {src_path}/a/x.txt "
                f"&& printf 'y\\n' > {src_path}/a/b/y.txt"
            ),
        )
        assert setup.returncode == 0, setup.stderr

        env, _ = _run_envelope(POC_HOST, src_path, POC_HOST, dst_path, "--recursive")
        assert env["success"] is True, env
        assert env["exit_code"] == EXIT_OK
        assert env["data"]["files_transferred"] == TREE_FILE_COUNT
        assert env["data"]["bytes"] == TREE_TOTAL_BYTES

        # Verify both files on dst with expected bytes.
        cat_x = _ssh(POC_HOST, f"cat {dst_path}/a/x.txt")
        assert cat_x.returncode == 0, cat_x.stderr
        assert cat_x.stdout == SUB_X_BYTES.decode()

        cat_y = _ssh(POC_HOST, f"cat {dst_path}/a/b/y.txt")
        assert cat_y.returncode == 0, cat_y.stderr
        assert cat_y.stdout == SUB_Y_BYTES.decode()
    finally:
        with contextlib.suppress(Exception):
            _remote_cleanup(POC_HOST, f"{src_path} {dst_path}")
