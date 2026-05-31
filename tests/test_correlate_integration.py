# ruff: noqa: PLR2004 — magic values are inherent to test assertions
"""Integration tests for log-aggregator/scripts/correlate.py v1.0.

Marker: ``live_ssh`` -- skipped by default. Run with::

    pytest tests/test_correlate_integration.py -v -m live_ssh
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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
    / "correlate.py"
)
POC_HOST = os.environ.get("POC_HOST", "pai")


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
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "true"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


def _run_json(*args: str, timeout: int = 30) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args, "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"correlate produced non-JSON stdout: {exc}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return proc.returncode, env, proc.stderr


def test_correlate_local_fixture_pair_mode(tmp_path: Path) -> None:
    log = tmp_path / "fixture.log"
    base = datetime(2026, 5, 25, 14, 2, 0, tzinfo=timezone.utc)
    rows = []
    for idx in range(50):
        dt = base + timedelta(seconds=idx)
        body = f"{dt.strftime('%Y-%m-%dT%H:%M:%SZ')} line-{idx}"
        if idx == 10:
            body += " span_id=s7 start"
        if idx == 18:
            body += " ERROR span_id=s7 end"
        rows.append(body)
    log.write_text("\n".join(rows) + "\n")

    rc, env, _stderr = _run_json(
        "--sources",
        f"file://local{log}?ts_regex=^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z&ts_format=%Y-%m-%dT%H:%M:%SZ",
        "--anchor",
        "span_id=s7",
        "--window",
        "1s",
        "--pair-mode",
        timeout=15,
    )
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["summary"]["duration_ms"] == 8000
    assert len(env["data"]["window_lines"]) == 11


def test_correlate_remote_poc_host_journal() -> None:
    if not _ssh_agent_has_key():
        pytest.skip("ssh-agent has no loaded identity (run `ssh-add ~/.ssh/id_ed25519`)")
    if not _host_reachable(POC_HOST):
        pytest.skip(f"host '{POC_HOST}' not reachable (override with POC_HOST=<alias>)")

    rc, env, _stderr = _run_json(
        "--sources",
        f"journal://{POC_HOST}/sshd",
        "--anchor",
        "Accepted",
        "--window",
        "60s",
        "--since",
        "30min",
        timeout=45,
    )
    if rc == 90:
        pytest.skip("no recent sshd 'Accepted' line on the PoC host")
    assert rc == 0
    assert env["success"] is True
    assert env["data"]["anchor"]["source"]
    assert len(env["data"]["window_lines"]) >= 1
