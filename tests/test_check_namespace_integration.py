"""Integration tests for k8s-debug/scripts/check_namespace.py v1.0."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_k8s

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = (
    REPO_ROOT
    / "plugins"
    / "k8s-debug"
    / "skills"
    / "k8s-debug"
    / "scripts"
    / "check_namespace.py"
)


def _run_check_namespace(namespace: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", namespace, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode in (0, 88), proc.stderr
    return json.loads(proc.stdout)


def test_check_namespace_default_namespace_json(kind_cluster) -> None:
    env = _run_check_namespace("default")
    assert "summary" in env["data"]
    assert set(env["data"].get("summary", {})) >= {"pods", "events", "deployments", "services", "pvcs"}


def test_check_namespace_degraded_fixture_namespace_json(fixture_pods) -> None:
    env = _run_check_namespace(fixture_pods["namespace"])
    assert env["success"] is True
    data = env["data"]
    assert data["summary"]["pods"] == "crit"
    assert data["all_findings_count"] >= 1
    assert any(f["domain"] == "pods" and f["severity"] == "crit" for f in data["findings"])
