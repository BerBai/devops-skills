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
import subprocess
import sys
from pathlib import Path

import live_support  # noqa: E402
import pytest
from live_support import require_live_ssh  # noqa: E402

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
POC_HOST = live_support.poc_host()

EXIT_OK = 0
EXIT_FAIL = 1


# -----------------------------------------------------------------------------
# Skip guards
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


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
    """One remote alias + 'local' in the same run.

    On Windows a POSIX path on the local route is an explicit *skip*
    (``skipped_reason: "posix_path_on_windows"``, OPT-004): not a failure, and
    not a line source either. So "both halves answered" means the remote half
    produced prefixed lines and the local half either produced them too (POSIX
    host) or reported the skip -- and the run is only red when a genuinely
    servable half failed.
    """
    result = _run_envelope(
        "--hosts", f"{POC_HOST},local", "/etc/hosts", "--lines", "2"
    )

    assert set(result["data"]["hosts"].keys()) == {POC_HOST, "local"}
    assert f"{POC_HOST}| " in result["stdout"]
    assert POC_HOST in result["data"]["ok_hosts"]
    assert result["data"]["failed_hosts"] == []

    local = result["data"]["hosts"]["local"]
    if local.get("skipped"):
        assert local["skipped_reason"] == "posix_path_on_windows"
        assert local["failure_class"] is None
        assert result["data"]["skipped_hosts"] == ["local"]
        assert result["success"] is True, result
        assert result["exit_code"] == EXIT_OK
        assert "local| " not in result["stdout"]
        assert "tail_log: local: skipped (posix_path_on_windows)" in result["stderr"]
    else:
        assert local["success"] is True
        assert result["data"]["skipped_hosts"] == []
        assert "local| " in result["stdout"]
