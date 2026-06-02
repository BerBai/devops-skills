"""Integration tests for iac-state/scripts/state_inspect.py v1.0."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_terraform

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "plugins" / "iac-state" / "skills" / "iac-state" / "scripts" / "state_inspect.py"


def _tool() -> str:
    for candidate in ("tofu", "terraform"):
        if shutil.which(candidate):
            return candidate
    pytest.skip("neither tofu nor terraform is installed")


def test_state_inspect_local_initialized_project_json(tmp_path: Path) -> None:
    tool = _tool()
    (tmp_path / "main.tf").write_text(
        """
terraform {
  backend "local" {
    path = "terraform.tfstate"
  }
}
""".strip()
    )
    init = subprocess.run([tool, "init", "-input=false"], cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False)
    if init.returncode != 0:
        pytest.skip(f"{tool} init fixture unavailable: {init.stderr[:300]}")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", str(tmp_path), "--tool", tool, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout)
    assert env["success"] is True
    assert env["data"]["backend"]["type"] == "local"
    assert env["data"]["summary"]["state"] in {"ok", "warn", "crit"}
