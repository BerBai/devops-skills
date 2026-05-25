"""Integration tests for ssh_download v1.0 — runs real ssh / sftp to a real host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_download_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

These tests gate quality, not infrastructure: if ssh-agent has no key
or the host is unreachable, the entire module is skipped instead of
failing.

Each test owns a unique ``/tmp/download-fixture-<uuid>`` path on the
remote host and cleans up after itself in a ``finally`` block.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_DOWNLOAD = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_download.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

# Well-known exit codes.
EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_VERIFY_FAILED = 9

# 1 MB and helpers for resume / bwlimit tests.
ONE_MIB = 1024 * 1024
TWO_MIB = 2 * 1024 * 1024
HALF_MIB = ONE_MIB // 2
QUARTER_MIB = ONE_MIB // 4

# bwlimit timing bounds (KB/s × seconds). 100 KB/s × 1 MB ≈ 10s.
BWLIMIT_KBS = 100
BWLIMIT_MIN_S = 8
BWLIMIT_MAX_S = 14


# -----------------------------------------------------------------------------
# Skip guards (mirror the upload integration shape)
# -----------------------------------------------------------------------------


def _ssh_agent_has_key() -> bool:
    """Return True iff ssh-add -l reports at least one loaded identity."""
    if shutil.which("ssh-add") is None:
        return False
    try:
        proc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _host_reachable(host: str) -> bool:
    """Quick `ssh <host> true` probe with a short timeout."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Skip the whole module unless agent + host are usable."""
    if not _ssh_agent_has_key():
        pytest.skip(
            "ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)"
        )
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _run_envelope(*args: str, timeout: int = 60) -> tuple[dict, str]:
    """Invoke ssh_download.py as a subprocess; return (parsed_json, stderr_text)."""
    argv = [sys.executable, str(SSH_DOWNLOAD), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout), proc.stderr
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_download produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def _ssh(host: str, command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _remote_sha256(host: str, remote_path: str) -> str | None:
    proc = _ssh(host, f"sha256sum {remote_path} 2>/dev/null | awk '{{print $1}}'")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _local_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while chunk := fp.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _remote_cleanup(host: str, remote_path: str) -> None:
    """Best-effort cleanup; failures don't fail the test."""
    _ssh(host, f"rm -rf {remote_path}", timeout=10)


def _unique_remote(prefix: str = "download-fixture") -> str:
    return f"/tmp/{prefix}-{uuid.uuid4().hex[:8]}"


# -----------------------------------------------------------------------------
# Single-file happy path
# -----------------------------------------------------------------------------


def test_single_file_happy_path(tmp_path: Path) -> None:
    """`/etc/hostname` → local; content matches `ssh pai cat`."""
    expected = _ssh(POC_HOST, "cat /etc/hostname").stdout
    if not expected.strip():
        pytest.skip("remote /etc/hostname is empty or unreadable")

    local = tmp_path / "hostname"
    env, _ = _run_envelope(POC_HOST, "/etc/hostname", str(local))
    assert env["success"] is True, env
    assert env["exit_code"] == EXIT_OK
    assert env["data"]["route"] == "sftp"
    assert env["data"]["direction"] == "download"
    assert local.read_text() == expected


# -----------------------------------------------------------------------------
# Resume path
# -----------------------------------------------------------------------------


def test_resume_partial_then_complete(tmp_path: Path) -> None:
    """Build 2MB remote fixture; pre-seed local with first 1MB; resume."""
    remote = _unique_remote()
    try:
        # Create the 2MB remote fixture deterministically.
        seed_proc = _ssh(
            POC_HOST,
            f"dd if=/dev/urandom of={remote} bs=1M count=2 status=none",
            timeout=30,
        )
        assert seed_proc.returncode == 0, seed_proc.stderr
        remote_hash = _remote_sha256(POC_HOST, remote)
        assert remote_hash, "could not read remote sha256 of fixture"

        # Pull the first 1MB of that fixture to a local file (so the local
        # has a valid prefix of the remote bytes).
        partial = tmp_path / "partial.bin"
        head = _ssh(
            POC_HOST,
            f"dd if={remote} of=/dev/stdout bs=1M count=1 status=none | base64",
            timeout=30,
        )
        assert head.returncode == 0, head.stderr

        partial.write_bytes(base64.b64decode(head.stdout))
        assert partial.stat().st_size == ONE_MIB, (
            f"partial setup wrong size: {partial.stat().st_size}"
        )

        # Resume: expect bytes_resumed_from=ONE_MIB and sha matches.
        local = tmp_path / "out.bin"
        local.write_bytes(partial.read_bytes())
        env_resume, _ = _run_envelope(POC_HOST, remote, str(local), "--resume")
        assert env_resume["success"] is True, env_resume
        assert env_resume["data"]["sha256_verified"] is True
        assert env_resume["data"]["bytes_resumed_from"] == ONE_MIB
        assert _local_sha256(local) == remote_hash
    finally:
        _remote_cleanup(POC_HOST, remote)


def test_resume_sha256_mismatch_returns_9(tmp_path: Path) -> None:
    """Tampered local partial → resume exits 9 with both sha256s present."""
    remote = _unique_remote()
    try:
        # 1MB of all-A on remote.
        _ssh(
            POC_HOST,
            f"yes A | head -c {ONE_MIB} > {remote}",
            timeout=30,
        )
        # Local partial: 256KB of all-Z (wrong prefix).
        partial = tmp_path / "out.bin"
        partial.write_bytes(b"Z" * QUARTER_MIB)

        env_resume, _ = _run_envelope(POC_HOST, remote, str(partial), "--resume")
        assert env_resume["success"] is False, env_resume
        assert env_resume["exit_code"] == EXIT_VERIFY_FAILED
        assert "sha256_local" in env_resume["data"]
        assert "sha256_remote" in env_resume["data"]
        assert (
            env_resume["data"]["sha256_local"]
            != env_resume["data"]["sha256_remote"]
        )
    finally:
        _remote_cleanup(POC_HOST, remote)


# -----------------------------------------------------------------------------
# Recursive
# -----------------------------------------------------------------------------


def test_recursive_simple_tree(tmp_path: Path) -> None:
    """3 files (one in a subdir) on remote → all downloaded, structure matches."""
    remote_dir = _unique_remote("tree-dl")
    try:
        setup = _ssh(
            POC_HOST,
            (
                f"mkdir -p {remote_dir}/sub && "
                f"echo AAA > {remote_dir}/a.txt && "
                f"echo BBB > {remote_dir}/sub/b.txt && "
                f"echo CCC > {remote_dir}/sub/c.txt"
            ),
            timeout=15,
        )
        assert setup.returncode == 0, setup.stderr

        local_dir = tmp_path / "out"
        env, _ = _run_envelope(POC_HOST, remote_dir, str(local_dir), "--recursive")
        assert env["success"] is True, env
        assert (local_dir / "a.txt").read_text() == "AAA\n"
        assert (local_dir / "sub" / "b.txt").read_text() == "BBB\n"
        assert (local_dir / "sub" / "c.txt").read_text() == "CCC\n"
    finally:
        _remote_cleanup(POC_HOST, remote_dir)


def test_recursive_partial_failure_returns_1_with_failed_files(
    tmp_path: Path,
) -> None:
    """One un-readable remote file → other two succeed, failed_files lists it.

    Skipped when the remote user is root, since ``chmod 000`` does not
    block root reads on Linux and the test cannot construct a reliably
    unreadable file without sudo / chown to another user. The mocked
    unit-test counterpart ``test_recursive_continues_on_error_aggregates_failed_files``
    covers the same code path deterministically.
    """
    remote_user = _ssh(POC_HOST, "whoami").stdout.strip()
    if remote_user == "root":
        pytest.skip(
            f"remote user on {POC_HOST!r} is root; chmod 000 does not "
            "block root reads, so this test cannot construct an "
            "unreadable file. Unit test covers the failed_files path."
        )

    remote_dir = _unique_remote("partial-dl")
    try:
        setup = _ssh(
            POC_HOST,
            (
                f"mkdir -p {remote_dir} && "
                f"echo ok1 > {remote_dir}/ok-1.txt && "
                f"echo ok2 > {remote_dir}/ok-2.txt && "
                f"echo blocked > {remote_dir}/blocked.txt && "
                f"chmod 000 {remote_dir}/blocked.txt"
            ),
            timeout=15,
        )
        assert setup.returncode == 0, setup.stderr

        local_dir = tmp_path / "out"
        env, _ = _run_envelope(POC_HOST, remote_dir, str(local_dir), "--recursive")
        assert env["success"] is False, env
        assert env["exit_code"] == EXIT_GENERIC_FAILURE
        failed = env["data"].get("failed_files") or []
        assert any("blocked.txt" in entry["path"] for entry in failed), failed
        # The two readable files made it down.
        assert (local_dir / "ok-1.txt").exists()
        assert (local_dir / "ok-2.txt").exists()
    finally:
        # Restore mode before removing so rm -rf works without trouble.
        _ssh(POC_HOST, f"chmod -R u+rwX {remote_dir} 2>/dev/null || true")
        _remote_cleanup(POC_HOST, remote_dir)


# -----------------------------------------------------------------------------
# Bandwidth + progress flags + guards
# -----------------------------------------------------------------------------


def test_bwlimit_100kbs_roughly_10s(tmp_path: Path) -> None:
    """1MB at 100 KB/s ≈ 10s wall-clock (allow 8-14s for CI noise)."""
    remote = _unique_remote("bwlimit-dl")
    try:
        seed = _ssh(
            POC_HOST,
            f"dd if=/dev/urandom of={remote} bs=1M count=1 status=none",
            timeout=30,
        )
        assert seed.returncode == 0, seed.stderr

        local = tmp_path / "out.bin"
        t0 = time.monotonic()
        env, _ = _run_envelope(
            POC_HOST, remote, str(local), "--bwlimit", str(BWLIMIT_KBS),
            timeout=60,
        )
        elapsed = time.monotonic() - t0
        assert env["success"] is True, env
        assert BWLIMIT_MIN_S <= elapsed <= BWLIMIT_MAX_S, (
            f"bwlimit produced elapsed={elapsed:.1f}s, "
            f"expected [{BWLIMIT_MIN_S},{BWLIMIT_MAX_S}]"
        )
    finally:
        _remote_cleanup(POC_HOST, remote)


def test_no_progress_suppresses_stderr_lines(tmp_path: Path) -> None:
    """--no-progress + --json → no 'Downloading' lines on stderr."""
    local = tmp_path / "hostname"
    env, stderr = _run_envelope(POC_HOST, "/etc/hostname", str(local), "--no-progress")
    assert env["success"] is True, env
    assert "Downloading" not in stderr, f"unexpected progress lines: {stderr!r}"


def test_remote_not_found_returns_1(tmp_path: Path) -> None:
    """Non-existent remote path → exit 1, failure_class=remote_error."""
    local = tmp_path / "x"
    env, _ = _run_envelope(POC_HOST, "/no/such/remote/xyz123", str(local))
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert env["data"]["failure_class"] == "remote_error"
    assert "remote path not found" in env["stderr"]


def test_remote_dir_without_recursive_returns_1(tmp_path: Path) -> None:
    """Remote directory without --recursive → exit 1."""
    local = tmp_path / "etc-out"
    env, _ = _run_envelope(POC_HOST, "/etc", str(local))
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert "--recursive" in env["stderr"]


def test_local_dir_without_recursive_returns_1(tmp_path: Path) -> None:
    """Local existing directory without --recursive → exit 1 (CLI guard)."""
    # tmp_path is already an existing directory.
    env, _ = _run_envelope(POC_HOST, "/etc/hostname", str(tmp_path))
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert "directory" in env["stderr"]
