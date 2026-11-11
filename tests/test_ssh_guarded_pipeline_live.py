"""Integration tests for ssh-guarded request/execute pipeline v1.0.

Marker: ``live_ssh`` — skipped by default. Run with::

    pytest tests/test_ssh_guarded_pipeline_live.py -v -m live_ssh

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

These tests gate quality, not infrastructure: if ssh-agent has no key or
the host is unreachable, the entire module is skipped instead of failing.

Each test owns a unique ``~/workspace/devops-skills/<prefix>-<uuid>`` path
on the remote host and cleans up after itself in a ``finally`` block.
Local artifact + result.json files are also cleaned up.

The 6 cases follow PRD Acceptance Criteria row 3 verbatim:
1. command happy path
2. upload happy path
3. mkdir happy path
4. delete happy path (mkdir then delete --recursive --confirm-delete)
5. denylist refusal (drafter warns, executor exits 51)
6. sandbox refusal (drafter warns, executor exits 50)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import live_support  # noqa: E402
import pytest
from live_support import require_live_ssh  # noqa: E402

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_GUARDED_SCRIPTS = (
    REPO_ROOT / "plugins" / "ssh-guarded" / "skills" / "ssh-guarded" / "scripts"
)
REQUEST_COMMAND = SSH_GUARDED_SCRIPTS / "request_command.py"
REQUEST_UPLOAD = SSH_GUARDED_SCRIPTS / "request_upload.py"
REQUEST_MKDIR = SSH_GUARDED_SCRIPTS / "request_mkdir.py"
REQUEST_DELETE = SSH_GUARDED_SCRIPTS / "request_delete.py"
RUN_REQUEST = SSH_GUARDED_SCRIPTS / "run_request.py"

POC_HOST = live_support.poc_host()
PROJECT_ID = "devops-skills"

#: Redirected state home for the whole module so a live run never pollutes the
#: developer's real ``~/.devops/ssh-guarded`` (GAP-006).
_STATE_HOME = Path(tempfile.mkdtemp(prefix="ssh-guarded-live-state-"))

EXIT_OK = 0
EXIT_SANDBOX_REFUSAL = 50
EXIT_DENYLIST_REFUSAL = 51


def _state_env() -> dict[str, str]:
    """Process env for a live run, with state redirected to a temp dir."""
    env = dict(os.environ)
    env["SSH_GUARDED_HOME"] = str(_STATE_HOME)
    env["SSH_GUARDED_PROJECT_ID"] = PROJECT_ID
    return env


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


def _ssh(host: str, command: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _remote_cleanup(host: str, remote_path: str) -> None:
    """Best-effort cleanup; failures don't fail the test."""
    _ssh(host, f"rm -rf {remote_path}", timeout=10)


def _run_drafter(script: Path, *args: str, timeout: int = 60) -> tuple[dict, str]:
    """Invoke a drafter script; return (parsed_envelope, stderr_text).

    All drafters always run with --json so envelope is stable; --project-id is
    pinned so tests don't depend on cwd-detection; $SSH_GUARDED_HOME is
    redirected into the per-run temp dir so a live run never writes into the
    developer's real state home (GAP-006).
    """
    argv = [
        sys.executable,
        str(script),
        *args,
        "--json",
        "--project-id",
        PROJECT_ID,
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_state_env(),
    )
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"drafter produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise
    return env, proc.stderr


def _run_executor(*args: str, timeout: int = 120) -> tuple[dict, str, int]:
    """Invoke run_request.py; return (parsed_envelope, stderr_text, returncode)."""
    argv = [
        sys.executable,
        str(RUN_REQUEST),
        *args,
        "--json",
        "--project-id",
        PROJECT_ID,
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_state_env(),
    )
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"run_request produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise
    return env, proc.stderr, proc.returncode


def _local_cleanup(artifact_path: str | Path) -> None:
    """Remove artifact + result.json. Suppresses missing files."""
    art = Path(artifact_path)
    art.unlink(missing_ok=True)
    art.with_suffix(".result.json").unlink(missing_ok=True)
    # Lock file removed by exclusive_lock context manager on POSIX; best-effort:
    Path(str(art) + ".lock").unlink(missing_ok=True)


def _local_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while chunk := fp.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _remote_sha256(host: str, remote_path: str) -> str | None:
    proc = _ssh(host, f"sha256sum {remote_path} 2>/dev/null | awk '{{print $1}}'")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# -----------------------------------------------------------------------------
# 1. command happy path
# -----------------------------------------------------------------------------


def test_command_happy_path_live() -> None:
    """request_command + run_request --execute → exit 0, stdout has 'hello'."""
    artifact_path: str | None = None
    try:
        env, _ = _run_drafter(
            REQUEST_COMMAND,
            POC_HOST,
            "echo hello",
            "--reason",
            "live_test_command_happy",
        )
        assert env["success"] is True, env
        artifact_path = env["data"]["artifact_path"]

        env2, _, rc = _run_executor("--request", artifact_path, "--execute")
        assert rc == EXIT_OK, env2
        assert env2["success"] is True, env2
        assert env2["data"]["exit_code"] == 0, env2

        result_path = Path(artifact_path).with_suffix(".result.json")
        assert result_path.exists(), f"result.json missing: {result_path}"
        result = json.loads(result_path.read_text())
        assert "hello" in result["stdout_redacted"], result
    finally:
        if artifact_path:
            _local_cleanup(artifact_path)


# -----------------------------------------------------------------------------
# 2. upload happy path
# -----------------------------------------------------------------------------


def test_upload_happy_path_live(tmp_path: Path) -> None:
    """request_upload + run_request --execute → remote sha256 matches local."""
    local_file = tmp_path / "upload-test.bin"
    local_file.write_bytes(os.urandom(1024))
    local_hash = _local_sha256(local_file)

    remote = f"~/workspace/{PROJECT_ID}/upload-test-{uuid.uuid4().hex[:8]}.bin"
    artifact_path: str | None = None
    try:
        env, _ = _run_drafter(
            REQUEST_UPLOAD,
            POC_HOST,
            "--local",
            str(local_file),
            "--remote",
            remote,
            "--reason",
            "live_test_upload_happy",
            "--confirm-sensitive-local-upload",
        )
        assert env["success"] is True, env
        artifact_path = env["data"]["artifact_path"]

        env2, _, rc = _run_executor("--request", artifact_path, "--execute")
        assert rc == EXIT_OK, env2
        assert env2["success"] is True, env2

        remote_hash = _remote_sha256(POC_HOST, remote)
        assert remote_hash == local_hash, (
            f"sha mismatch: remote={remote_hash} local={local_hash}"
        )
    finally:
        _remote_cleanup(POC_HOST, remote)
        if artifact_path:
            _local_cleanup(artifact_path)


# -----------------------------------------------------------------------------
# 3. mkdir happy path
# -----------------------------------------------------------------------------


def test_mkdir_happy_path_live() -> None:
    """request_mkdir + run_request --execute → remote dir exists."""
    remote = f"~/workspace/{PROJECT_ID}/staging-{uuid.uuid4().hex[:8]}"
    artifact_path: str | None = None
    try:
        env, _ = _run_drafter(
            REQUEST_MKDIR,
            POC_HOST,
            "--remote",
            remote,
            "--reason",
            "live_test_mkdir_happy",
            "--parents",
        )
        assert env["success"] is True, env
        artifact_path = env["data"]["artifact_path"]

        env2, _, rc = _run_executor("--request", artifact_path, "--execute")
        assert rc == EXIT_OK, env2
        assert env2["success"] is True, env2

        proc = _ssh(POC_HOST, f"test -d {remote} && echo present")
        assert proc.stdout.strip() == "present", proc
    finally:
        _remote_cleanup(POC_HOST, remote)
        if artifact_path:
            _local_cleanup(artifact_path)


# -----------------------------------------------------------------------------
# 4. delete happy path (create via direct ssh, then run pipeline)
# -----------------------------------------------------------------------------


def test_delete_happy_path_live() -> None:
    """direct ssh mkdir → request_delete --recursive + run_request --execute
    --confirm-delete → remote path gone."""
    remote = f"~/workspace/{PROJECT_ID}/delete-test-{uuid.uuid4().hex[:8]}"
    proc = _ssh(POC_HOST, f"mkdir -p {remote}")
    assert proc.returncode == 0, proc

    artifact_path: str | None = None
    try:
        env, _ = _run_drafter(
            REQUEST_DELETE,
            POC_HOST,
            "--remote",
            remote,
            "--reason",
            "live_test_delete_happy",
            "--recursive",
        )
        assert env["success"] is True, env
        artifact_path = env["data"]["artifact_path"]

        env2, _, rc = _run_executor(
            "--request", artifact_path, "--execute", "--confirm-delete"
        )
        assert rc == EXIT_OK, env2
        assert env2["success"] is True, env2

        proc = _ssh(POC_HOST, f"test ! -e {remote} && echo gone")
        assert proc.stdout.strip() == "gone", proc
    finally:
        _remote_cleanup(POC_HOST, remote)
        if artifact_path:
            _local_cleanup(artifact_path)


# -----------------------------------------------------------------------------
# 5. denylist refusal (drafter warns, executor exits 51)
# -----------------------------------------------------------------------------


def test_denylist_refusal_live() -> None:
    """request_delete on ~/.ssh/authorized_keys → drafter writes artifact
    with warning (C3 pedagogical), executor refuses with exit 51, no ssh attempt."""
    artifact_path: str | None = None
    try:
        env, stderr = _run_drafter(
            REQUEST_DELETE,
            POC_HOST,
            "--remote",
            "~/.ssh/authorized_keys",
            "--reason",
            "live_test_denylist_refusal",
            "--recursive",
        )
        # Drafter still writes artifact (FR-2 pedagogical); stderr should warn.
        assert env["success"] is True, env
        artifact_path = env["data"]["artifact_path"]
        # Stderr-as-warning is the contract; we don't assert exact text but
        # confirm something was written.
        assert "denylist" in stderr.lower() or "denied" in stderr.lower() or "warn" in stderr.lower(), (
            f"expected denylist warning on stderr, got: {stderr!r}"
        )

        env2, _, rc = _run_executor(
            "--request", artifact_path, "--execute", "--confirm-delete"
        )
        assert rc == EXIT_DENYLIST_REFUSAL, env2
        assert env2["success"] is False, env2
        assert env2["error"]["failure_class"] == "path_in_denylist", env2
    finally:
        if artifact_path:
            _local_cleanup(artifact_path)


# -----------------------------------------------------------------------------
# 6. sandbox refusal (drafter warns, executor exits 50)
# -----------------------------------------------------------------------------


def test_sandbox_refusal_live() -> None:
    """request_command --workdir=/etc → drafter writes artifact with warning,
    executor refuses with exit 50."""
    artifact_path: str | None = None
    try:
        env, stderr = _run_drafter(
            REQUEST_COMMAND,
            POC_HOST,
            "echo x",
            "--reason",
            "live_test_sandbox_refusal",
            "--workdir",
            "/etc",
        )
        assert env["success"] is True, env
        artifact_path = env["data"]["artifact_path"]
        assert "sandbox" in stderr.lower() or "outside" in stderr.lower() or "warn" in stderr.lower(), (
            f"expected sandbox warning on stderr, got: {stderr!r}"
        )

        env2, _, rc = _run_executor("--request", artifact_path, "--execute")
        assert rc == EXIT_SANDBOX_REFUSAL, env2
        assert env2["success"] is False, env2
        assert env2["error"]["failure_class"] == "workdir_outside_sandbox", env2
    finally:
        if artifact_path:
            _local_cleanup(artifact_path)
