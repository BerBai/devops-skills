# ruff: noqa: PLR2004 — integration assertions use concrete fixture counts
"""Integration tests for log-aggregator/scripts/grep_across_sources.py v1.0.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_grep_across_sources_integration.py -v -m live_ssh
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
SCRIPT_PATH = (
    REPO_ROOT
    / "plugins"
    / "log-aggregator"
    / "skills"
    / "log-aggregator"
    / "scripts"
    / "grep_across_sources.py"
)
POC_HOST = live_support.poc_host()


def _run_json(*args: str, timeout: int = 30) -> tuple[int, list[dict], str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args, "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"grep_across_sources produced non-JSON stdout: {exc}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return proc.returncode, lines, proc.stderr


@pytest.mark.skipif(os.name == "nt", reason="file:// source specs and tail are POSIX-only")
def test_grep_local_fixture_pattern_json(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text("INFO boot\nERROR one\nWARN noisy\nERROR two\n")

    rc, lines, stderr = _run_json(
        "--sources",
        f"file://local{log}",
        "--pattern",
        "ERROR",
        timeout=15,
    )

    assert rc == 0, stderr
    assert lines[-1]["success"] is True
    assert lines[-1]["data"]["total_hits"] == 2
    assert list(lines[-1]["data"]["per_source"].values()) == [2]
    assert [line["raw"] for line in lines[:-1]] == ["ERROR one", "ERROR two"]


@pytest.mark.skipif(os.name == "nt", reason="file:// source specs and tail are POSIX-only")
def test_grep_local_fixture_heatmap(tmp_path: Path) -> None:
    log1 = tmp_path / "a.log"
    log2 = tmp_path / "b.log"
    log1.write_text("\n".join(f"a-{i}" for i in range(10)) + "\n")
    log2.write_text("\n".join(f"b-{i}" for i in range(100)) + "\n")

    rc, lines, stderr = _run_json(
        "--sources",
        f"file://local{log1}",
        f"file://local{log2}",
        timeout=15,
    )

    assert rc == 0, stderr
    env = lines[-1]
    assert env["success"] is True
    assert env["data"]["is_heatmap"] is True
    assert env["data"]["total_lines"] == 110
    assert sorted(env["data"]["per_source"].values()) == [10, 100]


def test_grep_remote_poc_host_journal_pattern() -> None:
    live_support.require_live_ssh()

    rc, lines, stderr = _run_json(
        "--sources",
        f"journal://{POC_HOST}/sshd",
        "--pattern",
        "sshd|Accepted|session",
        "--since",
        "30min",
        timeout=45,
    )
    assert rc == 0, stderr
    assert lines[-1]["success"] is True
    assert f"{POC_HOST}/sshd" in lines[-1]["data"]["per_source"]
