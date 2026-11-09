"""Integration tests for ssh_upload v1.0 — runs real ssh / sftp to a real host.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_upload_integration.py -v -m live_ssh

The target host comes from ``POC_HOST`` (no committed default). See
``tests/README.md`` for prerequisites.

These tests gate quality, not infrastructure: if ssh-agent has no key
or the host is unreachable, the entire module is skipped instead of
failing.

Every exit code asserted here is imported from the product's own table
(``plugins/ssh-core/.../scripts/lib/exit_codes.py``) instead of being copied
into the module: the transfer family was renumbered to 160-168 several releases
ago, and a local ``EXIT_GENERIC_FAILURE = 1`` was asserting a contract the
product no longer had (GAP-002).

Capability split (GAP-001/GAP-002): cases that need the paramiko/SFTP backend
are marked ``sftp_only``; cases the scp fallback *can* answer assert the
fallback instead of skipping, so a paramiko-free machine still exercises a real
transport.

Each test owns a unique ``/tmp/upload-test-<uuid>`` path under the
remote host and cleans up after itself in a ``finally`` block.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import live_support  # noqa: E402
from live_support import require_live_ssh  # noqa: E402

# ``lib`` is pinned to ssh-core's own package by tests/conftest.py, so this is
# the shipped table, not a copy of it.
from lib import exit_codes as _codes  # noqa: E402

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
POC_HOST = live_support.poc_host()

# Exit codes, read from lib/exit_codes.py.
EXIT_OK = _codes.EXIT_OK
EXIT_LOCAL_PATH_MISSING = _codes.EXIT_LOCAL_PATH_MISSING
EXIT_LOCAL_PATH_IS_DIRECTORY = _codes.EXIT_LOCAL_PATH_IS_DIRECTORY
EXIT_MODE_UNSUPPORTED = _codes.EXIT_MODE_UNSUPPORTED
EXIT_REMOTE_PATH_EXISTS = _codes.EXIT_REMOTE_PATH_EXISTS
EXIT_VERIFY_FAILED = _codes.EXIT_VERIFY_FAILED

#: Flags only the SFTP backend can honour. On the scp fallback each one must be
#: *refused* (166 ``mode_deferred``), never silently dropped (SSHCORE-026).
SFTP_ONLY_FLAGS: dict[str, list[str]] = {
    "--bwlimit": ["100"],
    "--chmod": ["0600"],
    "--resume": [],
}

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


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _run_envelope(*args: str, timeout: int = 60) -> tuple[dict, str]:
    """Invoke ssh_upload.py as a subprocess; return (parsed_json, stderr_text)."""
    argv = [sys.executable, str(SSH_UPLOAD), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
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
        encoding="utf-8",
        errors="replace",
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
    """LICENSE → remote; sha256 matches both ends on whichever transport ran."""
    license_path = REPO_ROOT / "LICENSE"
    if not license_path.exists():
        pytest.skip("LICENSE missing from repo root")

    remote = _unique_remote()
    try:
        env, _ = _run_envelope(POC_HOST, str(license_path), remote)
        assert env["success"] is True, env
        assert env["exit_code"] == EXIT_OK
        # GAP-001: assert the transport this machine can actually run. Marking
        # the case sftp_only would stop exercising the scp fallback, which is
        # the whole point of a paramiko-free box.
        transport = env["data"]["transport"]
        assert env["data"]["route"] == transport, env["data"]
        expected = "sftp" if live_support.has_paramiko() else "scp"
        assert transport == expected, env["data"]

        remote_hash = _remote_sha256(POC_HOST, remote)
        local_hash = _local_sha256(license_path)
        assert remote_hash == local_hash, (
            f"sha mismatch: remote={remote_hash} local={local_hash}"
        )
    finally:
        _remote_cleanup(POC_HOST, remote)


def test_local_missing_is_classified_local_path_missing() -> None:
    """Non-existent local path → 160 local_path_missing, without contacting ssh."""
    env, _ = _run_envelope(POC_HOST, "/no/such/file/xyz123", "/tmp/x")
    assert env["success"] is False
    assert env["exit_code"] == EXIT_LOCAL_PATH_MISSING, env
    assert env["data"]["failure_class"] == "local_path_missing"


def test_dir_without_recursive_is_classified_local_path_is_directory(
    tmp_path: Path,
) -> None:
    """Local directory without --recursive → 161 local_path_is_directory."""
    env, _ = _run_envelope(POC_HOST, str(tmp_path), "/tmp/x")
    assert env["success"] is False
    assert env["exit_code"] == EXIT_LOCAL_PATH_IS_DIRECTORY, env
    assert env["data"]["failure_class"] == "local_path_is_directory"
    assert "--recursive" in env["stderr"]


@pytest.mark.parametrize("flag", sorted(SFTP_ONLY_FLAGS))
def test_sftp_only_flag_is_refused_on_the_fallback(
    tmp_path: Path, flag: str
) -> None:
    """A paramiko-free machine refuses these loudly instead of ignoring them.

    This is the fallback-side counterpart of the ``sftp_only`` cases: the scp
    transport cannot honour ``--chmod``/``--resume``/``--bwlimit``, so the
    product answers 166 ``mode_deferred`` and names the refused flag. Asserting
    that is more useful than skipping the whole feature area.
    """
    if live_support.has_paramiko():
        pytest.skip(
            "paramiko is installed, so the sftp transport honours "
            f"{flag}; the refusal path only exists on the scp fallback"
        )

    local = tmp_path / "payload.bin"
    local.write_bytes(b"x" * 4096)
    env, _ = _run_envelope(
        POC_HOST, str(local), _unique_remote(), flag, *SFTP_ONLY_FLAGS[flag]
    )
    assert env["success"] is False, env
    assert env["exit_code"] == EXIT_MODE_UNSUPPORTED, env
    assert env["data"]["failure_class"] == "mode_deferred", env["data"]
    assert flag in env["data"]["deferred_flags"], env["data"]
    assert env["data"]["deferred_transport"] == "sftp"


# -----------------------------------------------------------------------------
# Resume path (SFTP only: the scp fallback refuses --resume with 166)
# -----------------------------------------------------------------------------


@pytest.mark.sftp_only
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


@pytest.mark.sftp_only
def test_resume_sha256_mismatch_returns_verify_failed(tmp_path: Path) -> None:
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
        assert env_resume["exit_code"] == EXIT_VERIFY_FAILED, env_resume
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
        # sftp on a paramiko box, scp here: both are real transports.
        assert env["data"]["transport"] in {"sftp", "scp"}, env["data"]

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


@pytest.mark.sftp_only
def test_recursive_partial_failure_aggregates_failed_files(
    tmp_path: Path,
) -> None:
    """One un-readable file → other two succeed, failed_files lists the bad one.

    ``failed_files`` is the SFTP tree walker's continue-on-error aggregation
    (``lib/sftp_transport.upload_tree``); scp reports one status for the whole
    tree and cannot name the file, so the case is paramiko-gated. The fallback
    is covered by ``test_recursive_simple_tree`` and by the 166 refusals above.
    """
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
        assert env["exit_code"] != EXIT_OK, env
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


@pytest.mark.sftp_only
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


# -----------------------------------------------------------------------------
# Overwrite contract (GAP-003)
# -----------------------------------------------------------------------------


def test_existing_destination_is_refused_without_overwrite(tmp_path: Path) -> None:
    """A second upload to the same path → 168 remote_path_exists, file untouched.

    The guard runs before any transport work and is transport-independent (one
    ``test -e`` probe on scp, one ``stat`` on sftp), so this case is the
    fallback's coverage of a contract that has nothing to do with paramiko.
    """
    first, second = b"first\n", b"second\n"
    local = tmp_path / "payload.txt"
    # write_bytes, not write_text: text mode on Windows rewrites "\n" to
    # "\r\n" and the remote sha256 would then describe different bytes.
    local.write_bytes(first)
    first_hash = _local_sha256(local)
    local.write_bytes(second)
    second_hash = _local_sha256(local)

    remote = _unique_remote("overwrite-test")
    try:
        local.write_bytes(first)
        seed, _ = _run_envelope(POC_HOST, str(local), remote)
        assert seed["success"] is True, seed
        assert _remote_sha256(POC_HOST, remote) == first_hash

        local.write_bytes(second)
        env, _ = _run_envelope(POC_HOST, str(local), remote)
        assert env["success"] is False, env
        assert env["exit_code"] == EXIT_REMOTE_PATH_EXISTS, env
        assert env["data"]["failure_class"] == "remote_path_exists"
        assert env["data"]["requires"] == "--overwrite"
        # The refused upload really left the destination alone.
        assert _remote_sha256(POC_HOST, remote) == first_hash

        # ...and --overwrite really replaces it.
        env2, _ = _run_envelope(POC_HOST, str(local), remote, "--overwrite")
        assert env2["success"] is True, env2
        assert _remote_sha256(POC_HOST, remote) == second_hash
    finally:
        _remote_cleanup(POC_HOST, remote)
