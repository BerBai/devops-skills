"""Integration tests for ssh_upload v1.0 — runs real ssh / sftp to a real host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_upload_integration.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

These tests gate quality, not infrastructure: if ssh-agent has no key
or the host is unreachable, the entire module is skipped instead of
failing.

Each test owns a unique ``/tmp/upload-test-<uuid>`` path under the
remote host and cleans up after itself in a ``finally`` block.
"""

from __future__ import annotations

import contextlib
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
SSH_UPLOAD = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_upload.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

# Well-known exit codes.
EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_VERIFY_FAILED = 9

# 1 MB and 2 MB sample sizes for resume / bwlimit tests.
ONE_MIB = 1024 * 1024
TWO_MIB = 2 * 1024 * 1024
HALF_MIB = ONE_MIB // 2
QUARTER_MIB = ONE_MIB // 4

# bwlimit timing bounds (KB/s × seconds). 100 KB/s × 1 MB ≈ 10s.
BWLIMIT_KBS = 100
BWLIMIT_MIN_S = 8
BWLIMIT_MAX_S = 14


# -----------------------------------------------------------------------------
# Skip guards (mirror the shape of test_ssh_execute_integration.py)
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
    """Invoke ssh_upload.py as a subprocess; return (parsed_json, stderr_text)."""
    argv = [sys.executable, str(SSH_UPLOAD), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout), proc.stderr
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_upload produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def _ssh(host: str, command: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
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


def _unique_remote(prefix: str = "upload-test") -> str:
    return f"/tmp/{prefix}-{uuid.uuid4().hex[:8]}"


# -----------------------------------------------------------------------------
# Single-file happy path + guards
# -----------------------------------------------------------------------------


def test_single_file_happy_path() -> None:
    """LICENSE → remote; sha256 matches both ends."""
    license_path = REPO_ROOT / "LICENSE"
    if not license_path.exists():
        pytest.skip("LICENSE missing from repo root")

    remote = _unique_remote()
    try:
        env, _ = _run_envelope(POC_HOST, str(license_path), remote)
        assert env["success"] is True, env
        assert env["exit_code"] == EXIT_OK
        assert env["data"]["route"] == "sftp"

        remote_hash = _remote_sha256(POC_HOST, remote)
        local_hash = _local_sha256(license_path)
        assert remote_hash == local_hash, (
            f"sha mismatch: remote={remote_hash} local={local_hash}"
        )
    finally:
        _remote_cleanup(POC_HOST, remote)


def test_local_missing_returns_1() -> None:
    """Non-existent local path → exit 1 without contacting the remote."""
    env, _ = _run_envelope(POC_HOST, "/no/such/file/xyz123", "/tmp/x")
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert env["data"]["failure_class"] == "remote_error"


def test_dir_without_recursive_returns_1(tmp_path: Path) -> None:
    """Local directory without --recursive → exit 1."""
    env, _ = _run_envelope(POC_HOST, str(tmp_path), "/tmp/x")
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert "--recursive" in env["stderr"]


def test_chmod_applied(tmp_path: Path) -> None:
    """--chmod 0600 → remote stat -c %a reports '600'."""
    local = tmp_path / "private.txt"
    local.write_text("secret\n")
    remote = _unique_remote()
    try:
        env, _ = _run_envelope(POC_HOST, str(local), remote, "--chmod", "0600")
        assert env["success"] is True, env

        stat_proc = _ssh(POC_HOST, f"stat -c %a {remote}")
        assert stat_proc.returncode == 0, stat_proc.stderr
        mode = stat_proc.stdout.strip()
        assert mode == "600", f"expected mode 600, got {mode!r}"
    finally:
        _remote_cleanup(POC_HOST, remote)


# -----------------------------------------------------------------------------
# Resume path
# -----------------------------------------------------------------------------


def test_resume_partial_then_complete(tmp_path: Path) -> None:
    """Seed remote with first 512KB; resume with full 2MB file; sha256 matches."""
    full = tmp_path / "full.bin"
    full.write_bytes(os.urandom(TWO_MIB))

    half_seed = tmp_path / "half.bin"
    half_seed.write_bytes(full.read_bytes()[:HALF_MIB])

    remote = _unique_remote()
    try:
        # Step 1: upload only the first 512KB (no --resume) to seed.
        env_seed, _ = _run_envelope(POC_HOST, str(half_seed), remote)
        assert env_seed["success"] is True, env_seed

        # Step 2: resume with the full file. Expect bytes_resumed_from=HALF_MIB.
        env_resume, _ = _run_envelope(POC_HOST, str(full), remote, "--resume")
        assert env_resume["success"] is True, env_resume
        assert env_resume["data"]["sha256_verified"] is True
        assert env_resume["data"]["bytes_resumed_from"] == HALF_MIB

        # Final remote sha256 matches the full local file.
        assert _remote_sha256(POC_HOST, remote) == _local_sha256(full)
    finally:
        _remote_cleanup(POC_HOST, remote)


def test_resume_sha256_mismatch_returns_9(tmp_path: Path) -> None:
    """Tampered remote partial → resume exits 9 with both sha256s present."""
    full = tmp_path / "full.bin"
    full.write_bytes(b"A" * ONE_MIB)

    # Tampered seed: same size as a partial transfer but different content.
    tampered = tmp_path / "tampered.bin"
    tampered.write_bytes(b"Z" * QUARTER_MIB)

    remote = _unique_remote()
    try:
        # Seed remote with the tampered prefix.
        env_seed, _ = _run_envelope(POC_HOST, str(tampered), remote)
        assert env_seed["success"] is True, env_seed

        # Resume the full file — remote is smaller, will continue from QUARTER_MIB
        # but the prefix bytes don't match, so sha256 verify must fail.
        env_resume, _ = _run_envelope(POC_HOST, str(full), remote, "--resume")
        assert env_resume["success"] is False, env_resume
        assert env_resume["exit_code"] == EXIT_VERIFY_FAILED
        assert "sha256_local" in env_resume["data"]
        assert "sha256_remote" in env_resume["data"]
        assert env_resume["data"]["sha256_local"] != env_resume["data"]["sha256_remote"]
    finally:
        _remote_cleanup(POC_HOST, remote)


# -----------------------------------------------------------------------------
# Recursive
# -----------------------------------------------------------------------------


def test_recursive_simple_tree(tmp_path: Path) -> None:
    """3 files (one in a subdir) → all uploaded; remote structure matches."""
    (tmp_path / "a.txt").write_text("AAA\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("BBB\n")
    (sub / "c.txt").write_text("CCC\n")

    remote = _unique_remote("tree-test")
    try:
        env, _ = _run_envelope(POC_HOST, str(tmp_path), remote, "--recursive")
        assert env["success"] is True, env

        # Verify structure + content.
        ls = _ssh(POC_HOST, f"ls -1 {remote} {remote}/sub")
        assert ls.returncode == 0, ls.stderr
        assert "a.txt" in ls.stdout
        assert "b.txt" in ls.stdout
        assert "c.txt" in ls.stdout

        assert _remote_sha256(POC_HOST, f"{remote}/a.txt") == _local_sha256(
            tmp_path / "a.txt"
        )
        assert _remote_sha256(POC_HOST, f"{remote}/sub/b.txt") == _local_sha256(
            sub / "b.txt"
        )
        assert _remote_sha256(POC_HOST, f"{remote}/sub/c.txt") == _local_sha256(
            sub / "c.txt"
        )
    finally:
        _remote_cleanup(POC_HOST, remote)


def test_recursive_partial_failure_returns_1_with_failed_files(
    tmp_path: Path,
) -> None:
    """One un-readable file → other two succeed, failed_files lists the bad one."""
    (tmp_path / "ok-1.txt").write_text("ok1\n")
    (tmp_path / "ok-2.txt").write_text("ok2\n")
    bad = tmp_path / "blocked.txt"
    bad.write_text("blocked\n")
    os.chmod(bad, 0o000)  # provoke local-read PermissionError

    remote = _unique_remote("partial-test")
    try:
        env, _ = _run_envelope(POC_HOST, str(tmp_path), remote, "--recursive")
        # restore mode before any further test work
        os.chmod(bad, 0o644)
        assert env["success"] is False, env
        assert env["exit_code"] == EXIT_GENERIC_FAILURE
        failed = env["data"].get("failed_files") or []
        assert any("blocked.txt" in entry["path"] for entry in failed), failed
        # The other two should still be on the remote.
        ls = _ssh(POC_HOST, f"ls -1 {remote}")
        assert "ok-1.txt" in ls.stdout
        assert "ok-2.txt" in ls.stdout
    finally:
        # Belt-and-braces restore in case the assertion above raised first.
        with contextlib.suppress(OSError):
            os.chmod(bad, 0o644)
        _remote_cleanup(POC_HOST, remote)


# -----------------------------------------------------------------------------
# Bandwidth + progress flags
# -----------------------------------------------------------------------------


def test_bwlimit_100kbs_roughly_10s(tmp_path: Path) -> None:
    """1MB at 100 KB/s ≈ 10s wall-clock (allow 8–14s for CI noise)."""
    local = tmp_path / "1mb.bin"
    local.write_bytes(os.urandom(ONE_MIB))

    remote = _unique_remote("bwlimit-test")
    try:
        t0 = time.monotonic()
        env, _ = _run_envelope(
            POC_HOST, str(local), remote, "--bwlimit", str(BWLIMIT_KBS),
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
    """--no-progress + --json → no 'Uploading' lines on stderr."""
    local = tmp_path / "small.bin"
    local.write_bytes(b"x" * 1024)

    remote = _unique_remote("noprog-test")
    try:
        env, stderr = _run_envelope(POC_HOST, str(local), remote, "--no-progress")
        assert env["success"] is True, env
        assert "Uploading" not in stderr, f"unexpected progress lines: {stderr!r}"
    finally:
        _remote_cleanup(POC_HOST, remote)
