"""Integration smoke tests for cicd-debug/scripts/secret_scope_audit.py v1.0."""
# ruff: noqa: PLR2004

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "cicd-debug"
    / "skills"
    / "cicd-debug"
    / "scripts"
    / "secret_scope_audit.py"
)


def _gh_ready() -> bool:
    return bool(os.getenv("CICD_GH_REPO")) and shutil.which("gh") is not None and subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, timeout=15, check=False
    ).returncode == 0


@pytest.mark.cicd_gh
def test_secret_scope_audit_gh_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


@pytest.mark.cicd_gh
def test_secret_scope_audit_gh_no_workflow_scan_live_smoke() -> None:
    if not _gh_ready():
        pytest.skip("requires CICD_GH_REPO and gh auth status")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "gh",
            "--repo",
            os.environ["CICD_GH_REPO"],
            "--no-workflow-scan",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == result.returncode
    assert payload["data"]["provider"] == "gh"
    assert payload["data"]["scope"]["kind"] == "repo"


@pytest.mark.cicd_gh
def test_secret_scope_audit_gh_public_repo_workflow_scan_live_smoke() -> None:
    if not _gh_ready():
        pytest.skip("requires CICD_GH_REPO and gh auth status")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "gh",
            "--repo",
            os.environ["CICD_GH_REPO"],
            "--workflow-fetch-cap",
            "2",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == result.returncode
    assert payload["data"]["provider"] == "gh"
    assert payload["data"]["summary"] in {"ok", "warn", "crit"}


@pytest.mark.cicd_glab
def test_secret_scope_audit_glab_help_smoke() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
