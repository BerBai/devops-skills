# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Integration tests for log-aggregator/scripts/tail_multi.py v1.0.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_tail_multi_integration.py -v -m live_ssh

These cases exercise the full chain end-to-end:

    pytest -> tail_multi.py (subprocess) -> native ssh / local subprocess
           -> remote journalctl / local tail

The target host comes from ``POC_HOST``; there is no default. See
``tests/README.md`` for prerequisites.

A module that cannot reach the host skips through the shared guard in
``tests/live_support.py``, which probes reachability instead of assuming a
subnet. Cases that only need a local file run regardless.
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
SCRIPT_PATH = (
    REPO_ROOT
    / "plugins"
    / "log-aggregator"
    / "skills"
    / "log-aggregator"
    / "scripts"
    / "tail_multi.py"
)
POC_HOST = live_support.poc_host()


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


@pytest.mark.skipif(os.name == "nt", reason="file:// source specs and tail are POSIX-only")
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
    """journal:// against the live host: events_emitted >= 0, exit 0,
    clock_skew_ms includes POC_HOST."""
    live_support.require_live_ssh()

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
