"""Integration tests for port_check v1.0 -- real ssh to a real host.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_port_check_integration.py -v -m live_ssh

These exercise the full chain:

    pytest -> port_check.py (subprocess) -> ssh_execute.py (subprocess)
           -> native ssh -> remote nc / bash /dev/tcp

i.e. the architectural claim from CONTRIBUTING.md line 29 that diagnostic
plugins talk to ssh-core through the CLI rather than via Python imports.

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.

If ssh-agent is empty or the host is unreachable, the whole module is
skipped cleanly rather than failing -- these tests gate code quality,
not infrastructure.
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
PORT_CHECK = (
    REPO_ROOT
    / "plugins"
    / "remote-debug"
    / "skills"
    / "remote-debug"
    / "scripts"
    / "port_check.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

EXIT_OK = 0
EXIT_FAIL = 1


# -----------------------------------------------------------------------------
# Skip guards (mirror tests/test_diagnose_host_integration.py)
# -----------------------------------------------------------------------------


def _ssh_agent_has_key() -> bool:
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
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        host,
        "true",
    ]
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
        pytest.skip(
            "ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)"
        )
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )


def _run_envelope(*args: str, timeout: int = 60) -> dict:
    """Invoke port_check.py as a subprocess; return parsed JSON envelope."""
    argv = [sys.executable, str(PORT_CHECK), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"port_check produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise  # unreachable


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_port_check_pai_to_localhost_ssh_is_open() -> None:
    """End-to-end: from ``pai`` probe localhost:22. Expect open."""
    result = _run_envelope(POC_HOST, "--target", "localhost", "--ports", "22")

    assert result["exit_code"] == EXIT_OK, result
    assert result["success"] is True, result
    matrix = result["data"]["matrix"]
    assert len(matrix) == 1
    cell = matrix[0]
    assert cell["source"] == POC_HOST
    assert cell["target"] == "localhost"
    expected_port = 22
    assert cell["port"] == expected_port
    assert cell["status"] == "open", cell
    assert cell["elapsed_ms"] >= 0


def test_port_check_pai_to_localhost_high_port_not_open() -> None:
    """Probe a port that should be unbound: 9999 -> refused or filtered.

    We can't be strict about which (depends on whether the OS sends RST or
    drops). Both refused and filtered are 'not open', which is what
    matters for the matrix semantics.
    """
    result = _run_envelope(POC_HOST, "--target", "localhost", "--ports", "9999")

    assert result["exit_code"] == EXIT_FAIL, result
    assert result["success"] is False, result
    cell = result["data"]["matrix"][0]
    assert cell["status"] in {"refused", "filtered"}, cell


def test_port_check_pai_to_invalid_host_classifies_host_error() -> None:
    """Probe an unresolvable host from pai -> host-error (or filtered).

    On NAT-redirected networks, DNS may resolve to a sinkhole IP; in that
    case nc gets a connect failure rather than a name error. We accept
    'host-error' or 'filtered' (anything except 'open'/'refused').
    """
    result = _run_envelope(
        POC_HOST, "--target", "this-does-not-exist-xyzzy.invalid", "--ports", "22"
    )

    assert result["exit_code"] == EXIT_FAIL, result
    cell = result["data"]["matrix"][0]
    assert cell["status"] in {"host-error", "filtered"}, cell


def test_port_check_matrix_mode_with_multiple_ports() -> None:
    """Single source, single target, 2 ports: matrix has 2 cells."""
    result = _run_envelope(
        POC_HOST,
        "--target", "localhost",
        "--ports", "22,9999",
    )

    matrix = result["data"]["matrix"]
    expected_cells = 2
    assert len(matrix) == expected_cells
    # Port 22 should be open; 9999 should not.
    statuses_by_port = {c["port"]: c["status"] for c in matrix}
    assert statuses_by_port[22] == "open"
    assert statuses_by_port[9999] != "open"  # noqa: PLR2004
