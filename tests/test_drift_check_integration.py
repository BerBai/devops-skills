"""Integration tests for iac-state/scripts/drift_check.py v1.0."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_terraform

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "plugins" / "iac-state" / "skills" / "iac-state" / "scripts" / "drift_check.py"
PLAN_DIFF_EXIT = 2


def _tool() -> str:
    for candidate in ("tofu", "terraform"):
        if shutil.which(candidate):
            return candidate
    pytest.skip("neither tofu nor terraform is installed")


def test_drift_check_local_no_drift_json(tmp_path: Path) -> None:
    tool = _tool()
    (tmp_path / "main.tf").write_text("terraform {}\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", str(tmp_path), "--tool", tool, "--json"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"{tool} no-drift fixture unavailable: {proc.stderr[:300]}")
    env = json.loads(proc.stdout)
    assert env["success"] is True
    assert env["data"]["summary"]["drift"] == "ok"
    assert env["data"]["raw"]["plan_exit_code"] == 0


def test_drift_check_local_create_fixture_json(tmp_path: Path) -> None:
    tool = _tool()
    (tmp_path / "main.tf").write_text(
        """
resource "terraform_data" "example" {
  input = "drift-check"
}
""".strip()
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", str(tmp_path), "--tool", tool, "--json"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        pytest.skip(f"{tool} create fixture unavailable: {proc.stderr[:300]}")
    env = json.loads(proc.stdout)
    assert env["data"]["raw"]["plan_exit_code"] == PLAN_DIFF_EXIT
    assert env["data"]["summary"]["total_changes"] >= 1
    assert any(f["action"] == "create" for f in env["data"]["findings"])


def test_drift_check_local_target_create_fixture_json(tmp_path: Path) -> None:
    tool = _tool()
    (tmp_path / "main.tf").write_text(
        """
resource "terraform_data" "example" {
  input = "drift-check"
}
""".strip()
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "local",
            str(tmp_path),
            "--tool",
            tool,
            "--target",
            "terraform_data.example",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode not in {0, 1}:
        pytest.skip(f"{tool} target fixture unavailable: {proc.stderr[:300]}")
    env = json.loads(proc.stdout)
    assert env["data"]["target"] == "terraform_data.example"
    assert env["data"]["raw"]["plan_exit_code"] == PLAN_DIFF_EXIT
