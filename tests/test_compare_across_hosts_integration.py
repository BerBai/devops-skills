"""Integration tests for compare_across_hosts v1.0 -- real ssh to real hosts.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_compare_across_hosts_integration.py -v -m live_ssh

These exercise the full chain:

    pytest -> compare_across_hosts.py (subprocess) -> ssh_execute.py
           -> native ssh -> remote cat / command / dpkg|rpm

Two hosts are required. We use ``POC_HOST`` (default ``pai``) as the
baseline and ``local`` (handled by ssh_execute's local route) as the
"other" host. Override the baseline with ``POC_HOST=<alias>``.
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
COMPARE = (
    REPO_ROOT
    / "plugins"
    / "remote-debug"
    / "skills"
    / "remote-debug"
    / "scripts"
    / "compare_across_hosts.py"
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
    argv = [sys.executable, str(COMPARE), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"compare_across_hosts produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def test_compare_files_hostname_differs() -> None:
    """pai vs local /etc/hostname -- almost certainly differs."""
    result = _run_envelope(POC_HOST, "local", "--files", "/etc/hostname")

    # Envelope sanity (always).
    assert result["data"]["baseline"] == POC_HOST
    assert result["data"]["others"] == ["local"]
    assert result["data"]["summary"]["total"] == 1
    cell = result["data"]["comparisons"][0]
    assert cell["kind"] == "file"
    assert cell["target"] == "/etc/hostname"
    # If both reads succeeded, hostnames should differ.
    if "error" not in cell:
        assert cell["differs"] is True
        assert cell["unified_diff"] != ""


def test_compare_commands_uname_envelope_shape() -> None:
    """pai vs local with `uname -s` -- envelope is well-formed regardless
    of whether the OSes match."""
    result = _run_envelope(
        POC_HOST, "local", "--commands", "uname -s"
    )

    assert result["data"]["summary"]["total"] == 1
    cell = result["data"]["comparisons"][0]
    assert cell["kind"] == "command"
    assert cell["target"] == "uname -s"
    # Either differs (likely: Linux vs Darwin) or same -- both valid.
    assert isinstance(cell["differs"], bool)


def test_compare_files_identical_paths_same_content() -> None:
    """Diff /etc/hosts between the same physical host twice (via local
    invocations of ssh_execute). Both reads run on the same machine so
    the result should be identical -> differs=false.

    We use the same alias twice for both ends. ssh_execute routes 'local'
    via shlex.split + subprocess; running it twice yields identical
    content on a stable system.
    """
    result = _run_envelope("local", "local", "--files", "/etc/hosts")

    cell = result["data"]["comparisons"][0]
    if "error" not in cell:
        assert cell["differs"] is False, cell["unified_diff"]
