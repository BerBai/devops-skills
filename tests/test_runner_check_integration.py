"""Integration smoke tests for cicd-debug/scripts/runner_check.py v1.0."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    / "runner_check.py"
)


def test_runner_check_help_smoke() -> None:
    result = subprocess.run(
        ["python3", str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--pool-cap" in result.stdout
    assert "--enterprise" in result.stdout


@pytest.mark.cicd_gh
def test_runner_check_gh_repo_smoke() -> None:
    repo = os.getenv("CICD_GH_REPO")
    if not repo:
        pytest.skip("CICD_GH_REPO is not set")
    gh = shutil.which("gh")
    if not gh:
        pytest.skip("gh is not installed")
    auth = subprocess.run([gh, "auth", "status"], text=True, capture_output=True, check=False)
    if auth.returncode != 0:
        pytest.skip("gh auth is not configured")

    result = subprocess.run(
        ["python3", str(SCRIPT), "gh", "--repo", repo, "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    assert result.returncode in {0, 168}
    assert payload["data"]["provider"] == "gh"
    assert payload["data"]["scope"] == {"kind": "repo", "value": repo}
    assert payload["data"]["aggregate"]["total_count"] >= 0


@pytest.mark.cicd_gh
def test_runner_check_gh_org_optional_smoke() -> None:
    org = os.getenv("CICD_GH_ORG")
    if not org:
        pytest.skip("CICD_GH_ORG is not set")
    gh = shutil.which("gh")
    if not gh:
        pytest.skip("gh is not installed")

    result = subprocess.run(
        ["python3", str(SCRIPT), "gh", "--org", org, "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode in {160, 162, 164}:
        pytest.skip("gh auth/token does not allow org runner inspection")
    payload = json.loads(result.stdout)
    assert payload["data"]["scope"] == {"kind": "org", "value": org}


@pytest.mark.cicd_glab
def test_runner_check_glab_help_smoke() -> None:
    if not shutil.which("glab"):
        pytest.skip("glab is not installed")
    result = subprocess.run(
        ["python3", str(SCRIPT), "glab", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0

