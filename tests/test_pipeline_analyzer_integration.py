"""Integration tests for cicd-debug/scripts/pipeline_analyzer.py v1.0.

These tests require real gh/glab CLI access and are guarded by markers:
- cicd_gh: requires CICD_GH_REPO env var and gh auth status
- cicd_glab: requires glab binary (optional, skips if not installed)

PRD: .trellis/tasks/05-26-cicd-debug-pipeline-analyzer/prd.md
Design: .trellis/tasks/05-26-cicd-debug-pipeline-analyzer/design.md
"""
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
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "cicd-debug" / "skills" / "cicd-debug" / "scripts"
)

# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def _gh_auth_status() -> bool:
    """Check if gh is authenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _glab_installed() -> bool:
    """Check if glab is installed."""
    return shutil.which("glab") is not None


# -----------------------------------------------------------------------------
# cicd_gh marker tests
# -----------------------------------------------------------------------------


@pytest.mark.cicd_gh
def test_help_smoke() -> None:
    """pipeline_analyzer.py --help exits 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "pipeline_analyzer.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "provider" in result.stdout


@pytest.mark.cicd_gh
def test_gh_success_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Analyze a successful GitHub Actions run."""
    repo = os.getenv("CICD_GH_REPO")
    if not repo:
        pytest.skip("CICD_GH_REPO not set")

    # Find a recent successful run
    result = subprocess.run(
        ["gh", "run", "list", "--repo", repo, "--status", "success", "--limit", "1", "--json", "databaseId"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("Could not find successful run")

    runs = json.loads(result.stdout)
    if not runs:
        pytest.skip("No successful runs found")

    run_id = str(runs[0]["databaseId"])

    # Run pipeline_analyzer
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "pipeline_analyzer.py"),
            "gh",
            run_id,
            "--project",
            repo,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert data["data"]["summary"] in ("ok", "warn")
    assert data["data"]["failure_class"] is None


@pytest.mark.cicd_gh
def test_gh_failure_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Analyze a failed GitHub Actions run."""
    repo = os.getenv("CICD_GH_REPO")
    if not repo:
        pytest.skip("CICD_GH_REPO not set")

    # Find a recent failed run
    result = subprocess.run(
        ["gh", "run", "list", "--repo", repo, "--status", "failure", "--limit", "1", "--json", "databaseId"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("Could not find failed run")

    runs = json.loads(result.stdout)
    if not runs:
        pytest.skip("No failed runs found")

    run_id = str(runs[0]["databaseId"])

    # Run pipeline_analyzer
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "pipeline_analyzer.py"),
            "gh",
            run_id,
            "--project",
            repo,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert data["data"]["summary"] == "crit"
    assert data["data"]["failure_class"] is None


@pytest.mark.cicd_gh
def test_gh_diff_last_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Analyze with --diff-against last-success."""
    repo = os.getenv("CICD_GH_REPO")
    if not repo:
        pytest.skip("CICD_GH_REPO not set")

    # Find a recent run (any status)
    result = subprocess.run(
        ["gh", "run", "list", "--repo", repo, "--limit", "1", "--json", "databaseId"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("Could not find any run")

    runs = json.loads(result.stdout)
    if not runs:
        pytest.skip("No runs found")

    run_id = str(runs[0]["databaseId"])

    # Run pipeline_analyzer with diff
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "pipeline_analyzer.py"),
            "gh",
            run_id,
            "--project",
            repo,
            "--diff-against",
            "last-success",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert "diff_summary" in data["data"]


# -----------------------------------------------------------------------------
# cicd_glab marker tests
# -----------------------------------------------------------------------------


@pytest.mark.cicd_glab
def test_glab_help_smoke() -> None:
    """pipeline_analyzer.py --help exits 0 (glab path)."""
    if not _glab_installed():
        pytest.skip("glab not installed")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "pipeline_analyzer.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


@pytest.mark.cicd_glab
def test_glab_binary_check() -> None:
    """Verify glab binary exists when marker is active."""
    if not _glab_installed():
        pytest.skip("glab not installed")
    assert shutil.which("glab") is not None


# -----------------------------------------------------------------------------
# marker auto-skip decorators
# -----------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Configure auto-skip for cicd markers based on environment."""
    config.addinivalue_line(
        "markers", "cicd_gh: tests requiring GitHub CLI access to CICD_GH_REPO"
    )
    config.addinivalue_line(
        "markers", "cicd_glab: tests requiring GitLab CLI access"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Auto-skip cicd_gh tests if env not set or gh not authenticated."""
    repo = os.getenv("CICD_GH_REPO")
    gh_auth = _gh_auth_status()

    for item in items:
        if "cicd_gh" in item.keywords and (not repo or not gh_auth):
            item.add_marker(
                pytest.mark.skip(
                    reason=f"CICD_GH_REPO not set or gh not authenticated (repo={repo}, auth={gh_auth})"
                )
            )

        if "cicd_glab" in item.keywords and not _glab_installed():
            item.add_marker(pytest.mark.skip(reason="glab not installed"))
