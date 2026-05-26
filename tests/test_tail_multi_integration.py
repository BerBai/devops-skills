# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Integration tests for log-aggregator/scripts/tail_multi.py v1.0.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_tail_multi_integration.py -v -m live_ssh

These cases exercise the full chain end-to-end:

    pytest -> tail_multi.py (subprocess) -> native ssh / local subprocess
           -> remote journalctl / local tail

The default target host is ``pai`` (the PoC reference host). Override
with ``POC_HOST=<alias>`` if you need to point at a different machine.
See ``tests/README.md`` for prerequisites.

Network-skip rule (per CLAUDE.md): when the local machine is not on the
192.168.2.0/24 subnet, ``pai`` is unreachable and the *remote* fixtures
skip automatically through the existing _host_reachable guard. The
local file-fetcher test runs regardless.
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
SCRIPT_PATH = (
    REPO_ROOT
    / "plugins"
    / "log-aggregator"
    / "skills"
    / "log-aggregator"
    / "scripts"
    / "tail_multi.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")


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
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             host, "true"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


def _run_envelope(*args: str, timeout: int = 60) -> tuple[list[dict], dict]:
    """Invoke tail_multi.py as a subprocess and split events vs summary.

    Returns ``(events, summary)`` from the NDJSON stdout. Fails the test
    if stdout is not parseable.
    """
    argv = [sys.executable, str(SCRIPT_PATH), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    out_lines = [ln for ln in proc.stdout.strip().split("\n") if ln]
    if not out_lines:
        pytest.fail(
            f"tail_multi produced empty stdout\nargv={argv}\nstderr={proc.stderr!r}"
        )
    try:
        parsed = [json.loads(ln) for ln in out_lines]
    except json.JSONDecodeError as e:
        pytest.fail(
            f"tail_multi produced non-JSON stdout: {e}\n"
            f"argv={argv}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    *events, summary = parsed  # type: ignore[misc]
    return events, summary


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_file_local_fixture_round_trip(tmp_path: Path) -> None:
    """Local file:// fetcher: 50 lines in → 50 events out, exit 0, no skew."""
    log = tmp_path / "test.log"
    log.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")

    events, summary = _run_envelope(
        "--sources", f"file://local{log}",
        "--no-default-filters",
        timeout=15,
    )
    assert summary["success"] is True, summary
    assert summary["exit_code"] == 0
    assert summary["data"]["events_emitted"] == 50
    assert summary["data"]["clock_skew_ms"] == {}  # no remote hosts
    assert summary["data"]["failure_class"] is None
    assert len(events) == 50
    # Spot check: first and last event payloads survived the pipeline.
    bodies = sorted({ev["raw"] for ev in events})
    assert "line 0" in bodies and "line 49" in bodies


def test_journal_remote_poc_host() -> None:
    """journal:// against the PoC host: events_emitted >= 0, exit 0,
    clock_skew_ms includes POC_HOST."""
    if not _ssh_agent_has_key():
        pytest.skip(
            "ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)"
        )
    if not _host_reachable(POC_HOST):
        pytest.skip(
            f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)"
        )

    events, summary = _run_envelope(
        "--sources", f"journal://{POC_HOST}/ssh",
        "--since", "10min",
        timeout=45,
    )
    assert summary["success"] is True, summary
    assert summary["exit_code"] == 0
    assert summary["data"]["events_emitted"] >= 0
    # clock_skew_ms must contain the remote host key (value may be int or None).
    assert POC_HOST in summary["data"]["clock_skew_ms"]
    # Per-event invariant: every event line carries source + raw fields.
    for ev in events:
        assert "source" in ev and "raw" in ev
