"""Integration tests for tail_log v1.0 -- real ssh to a real host.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_tail_log_integration.py -v -m live_ssh

These exercise the full chain:

    pytest -> tail_log.py (subprocess) -> ssh_execute.py (subprocess)
           -> native ssh -> remote tail / journalctl

i.e. the architectural claim from CONTRIBUTING.md line 29 that diagnostic
plugins talk to ssh-core through the CLI rather than via Python imports.

Default target host is ``pai`` (the PoC reference host). Override with
``POC_HOST=<alias>`` if you need to point at a different machine. See
``tests/README.md`` for prerequisites.
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
TAIL_LOG = (
    REPO_ROOT
    / "plugins"
    / "remote-debug"
    / "skills"
    / "remote-debug"
    / "scripts"
    / "tail_log.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")

EXIT_OK = 0
EXIT_FAIL = 1


# -----------------------------------------------------------------------------
# Skip guards
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
    """Invoke tail_log.py as a subprocess; return parsed JSON envelope."""
    argv = [sys.executable, str(TAIL_LOG), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"tail_log produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_tail_log_single_host_path() -> None:
    """End-to-end: tail_log pai /etc/hosts --lines 3 --json."""
    result = _run_envelope(POC_HOST, "/etc/hosts", "--lines", "3")

    assert result["success"] is True, result
    assert result["exit_code"] == EXIT_OK
    assert result["data"]["host"] == POC_HOST
    assert result["data"]["source"]["kind"] == "path"
    assert result["data"]["source"]["value"] == "/etc/hosts"
    lines = result["data"]["lines"]
    assert isinstance(lines, list)
    expected_max_lines = 3
    assert 0 < len(lines) <= expected_max_lines
    # Each returned line is a non-empty string. We intentionally do NOT
    # assert content (e.g. "localhost" / "127"): some hosts have heavy
    # IPv6 entries that push the loopback lines past the tail window.
    assert all(isinstance(ln, str) and ln for ln in lines)


def test_tail_log_grep_filters_locally() -> None:
    """Pull /etc/hosts, grep for 'localhost' -> matched <= total."""
    result = _run_envelope(
        POC_HOST, "/etc/hosts", "--lines", "50", "--grep", "localhost"
    )

    assert result["success"] is True, result
    # Every kept line contains 'localhost'.
    for ln in result["data"]["lines"]:
        assert "localhost" in ln, ln
    assert result["data"]["filter"]["grep"] == "localhost"


def test_tail_log_unit_mode_does_not_crash() -> None:
    """Pulling a likely-not-running unit must not crash: either succeeds
    (some build of `ssh.service` exists) or returns a clean remote_error
    envelope. Either way, JSON is valid and success/exit_code agree."""
    result = _run_envelope(
        POC_HOST, "--unit", "ssh", "--lines", "3"
    )

    # Either the unit exists (success=true, exit=0) or it doesn't
    # (success=false, exit=1). We only assert envelope integrity here.
    assert result["data"]["source"]["kind"] == "unit"
    assert result["data"]["source"]["value"] == "ssh"
    assert isinstance(result["success"], bool)
    if result["success"]:
        assert result["exit_code"] == EXIT_OK
    else:
        assert result["exit_code"] == EXIT_FAIL


def test_tail_log_multi_host_concurrent_with_local_partner() -> None:
    """Use 'local' as a second host (ssh_execute supports local route).
    Verifies multi-host concurrency + line-prefix without needing two
    real remote aliases."""
    result = _run_envelope(
        "--hosts", f"{POC_HOST},local", "/etc/hosts", "--lines", "2"
    )

    # The combined stdout should have both prefixes; structured data has both.
    assert "hosts" in result["data"]
    assert set(result["data"]["hosts"].keys()) == {POC_HOST, "local"}
    assert f"{POC_HOST}| " in result["stdout"]
    assert "local| " in result["stdout"]
