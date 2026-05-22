"""Integration tests for ssh-core/ssh_cluster v1.0 -- real ssh to pai.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_ssh_cluster_integration.py -v -m live_ssh

These exercise the full chain:

    pytest -> ssh_cluster.py (subprocess) -> ssh_execute.py (subprocess)
           -> native ssh -> remote shell

POC_HOST defaults to ``pai``; override via env. The ``local`` alias is
handled by ssh_execute's local route so no second remote machine is
required.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_CLUSTER = (
    REPO_ROOT
    / "plugins"
    / "ssh-core"
    / "skills"
    / "ssh-core"
    / "scripts"
    / "ssh_cluster.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

EXIT_OK = 0
EXIT_FAIL = 1


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
            argv, capture_output=True, text=True, timeout=10, check=False
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    if not _ssh_agent_has_key():
        pytest.skip("ssh-agent has no loaded identity")
    if not _host_reachable(POC_HOST):
        pytest.skip(f"host '{POC_HOST}' not reachable")


def _run_envelope(*args: str, timeout: int = 60) -> dict:
    argv = [sys.executable, str(SSH_CLUSTER), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"ssh_cluster produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def test_cluster_two_hosts_hostname() -> None:
    """`hostname` on pai vs local must succeed and return distinct values."""
    result = _run_envelope(
        "hostname", "--hosts", f"{POC_HOST},local",
    )
    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK
    assert set(result["data"]["results"].keys()) == {POC_HOST, "local"}

    pai_stdout = result["data"]["results"][POC_HOST]["stdout"].strip()
    local_stdout = result["data"]["results"]["local"]["stdout"].strip()
    assert pai_stdout, result
    assert local_stdout, result
    # Whatever the two hosts are called, they shouldn't be the same.
    assert pai_stdout != local_stdout, result

    # summary tallies
    assert result["data"]["summary"]["total"] == 2  # noqa: PLR2004
    assert result["data"]["summary"]["ok"] == 2  # noqa: PLR2004
    assert result["data"]["failure_class"] is None


def test_cluster_partial_failure_on_bogus_alias() -> None:
    """pai + nonexistent alias -> partial_failure with pai success."""
    bogus = "no-such-alias-xyz-zzz"
    result = _run_envelope(
        "true", "--hosts", f"{POC_HOST},{bogus}",
    )
    assert result["success"] is False, result
    assert result["data"]["failure_class"] == "partial_failure", result

    pai_env = result["data"]["results"][POC_HOST]
    bogus_env = result["data"]["results"][bogus]
    assert pai_env["success"] is True
    assert bogus_env["success"] is False
    # ssh_execute should classify a missing alias as a network or auth issue
    assert bogus_env["failure_class"] is not None


def test_cluster_health_check_passes_through_to_command() -> None:
    """With --health-check, both hosts should still run the business cmd."""
    result = _run_envelope(
        "ls /", "--hosts", f"{POC_HOST},local",
        "--health-check",
    )
    assert result["success"] is True, result
    pai_env = result["data"]["results"][POC_HOST]
    local_env = result["data"]["results"]["local"]
    assert pai_env["success"] is True
    assert local_env["success"] is True
    # The business command's stdout should be non-empty for `ls /`
    assert pai_env["stdout"].strip(), pai_env
    assert local_env["stdout"].strip(), local_env
    assert result["data"]["health_check"] is True
