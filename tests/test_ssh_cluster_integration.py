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
import subprocess
import sys
from pathlib import Path

import pytest

import live_support  # noqa: E402
from live_support import require_live_ssh  # noqa: E402

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
POC_HOST = live_support.poc_host()

EXIT_OK = 0
EXIT_FAIL = 1


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


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
    """With --health-check, a host that passes runs the business command.

    A host the health check cannot run on is **skipped**, not failed
    (DEVOPS-BUG-010). The canonical case is the ``local`` route on a Windows
    box with no POSIX shell: the probe exits 127, so the host is skipped and
    the batch still succeeds. Before that fix this module reported
    ``partial_failure`` with ``summary.fail == 0``.
    """
    result = _run_envelope(
        "ls /", "--hosts", f"{POC_HOST},local",
        "--health-check",
    )
    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK
    data = result["data"]
    assert data["health_check"] is True
    assert POC_HOST in data["ok_hosts"], result
    assert data["failed_hosts"] == [], result
    assert data["summary"]["fail"] == 0

    pai_env = data["results"][POC_HOST]
    assert pai_env["success"] is True
    # The business command's stdout should be non-empty for `ls /`
    assert pai_env["stdout"].strip(), pai_env

    local_env = data["results"]["local"]
    if local_env["skipped"]:
        # The skip is reported truthfully and does not fail the batch.
        assert local_env["skipped_reason"] == "health_check_failed"
        assert "local" in data["skipped_hosts"]
        assert "skipped_reason" in local_env
    else:
        assert local_env["success"] is True, local_env
        assert local_env["stdout"].strip(), local_env


def test_cluster_health_check_false_skips_every_host_without_failing() -> None:
    """`--health-check-command false` on every host is a skip, not a failure."""
    result = _run_envelope(
        "uptime", "--hosts", f"{POC_HOST},local",
        "--health-check", "--health-check-command", "false",
    )
    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK
    data = result["data"]
    assert data["failure_class"] is None
    assert data["ok_hosts"] == []
    assert data["failed_hosts"] == []
    assert set(data["skipped_hosts"]) == {POC_HOST, "local"}
    assert data["summary"] == {
        "total": 2, "ok": 0, "fail": 0, "skipped": 2,
        "elapsed_ms": data["summary"]["elapsed_ms"],
    }
    for alias in (POC_HOST, "local"):
        env = data["results"][alias]
        assert env["skipped"] is True
        assert env["skipped_reason"] == "health_check_failed"
