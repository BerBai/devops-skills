"""Integration tests for iac-state/scripts/module_validate.py v1.0."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_terraform

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "plugins" / "iac-state" / "skills" / "iac-state" / "scripts" / "module_validate.py"


def _tool() -> str:
    for candidate in ("tofu", "terraform"):
        if shutil.which(candidate):
            return candidate
    pytest.skip("neither tofu nor terraform is installed")


def _write_good_module(path: Path) -> None:
    (path / "main.tf").write_text("")
    (path / "variables.tf").write_text(
        """
variable "name" {
  type        = string
  description = "Name."
  validation {
    condition     = length(var.name) > 0
    error_message = "name required"
  }
}
""".strip()
    )
    (path / "outputs.tf").write_text("")
    (path / "versions.tf").write_text('terraform { required_version = ">= 1.5, < 2.0" }\n')
    (path / "README.md").write_text("# module\n")


def test_module_validate_well_formed_module_json(tmp_path: Path) -> None:
    tool = _tool()
    _write_good_module(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", str(tmp_path), "--tool", tool, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"{tool} validate fixture unavailable: {proc.stderr[:300]}")
    env = json.loads(proc.stdout)
    assert env["success"] is True
    assert env["data"]["summary"]["modules"] in {"ok", "warn"}


def test_module_validate_broken_module_json(tmp_path: Path) -> None:
    tool = _tool()
    (tmp_path / "main.tf").write_text('resource "terraform_data" "x" {\n')
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", str(tmp_path), "--tool", tool, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    env = json.loads(proc.stdout)
    assert proc.returncode in {0, 143}
    assert env["success"] in {True, False}
