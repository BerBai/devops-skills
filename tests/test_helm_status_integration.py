"""Integration tests for k8s-debug/scripts/helm_status.py v1.0."""

from __future__ import annotations

import json
import shutil
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
    / "helm_status.py"
)


def _require_helm() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm binary not installed")


def _run_helm_status(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", *args, "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["helm", *args], capture_output=True, text=True, timeout=120, check=False)


def test_helm_status_deployed_release_json(kind_cluster) -> None:
    _require_helm()
    release = "helm-status-smoke"
    namespace = "default"
    _helm("-n", namespace, "uninstall", release, "--ignore-not-found")
    try:
        install = _helm(
            "-n",
            namespace,
            "install",
            release,
            "oci://registry-1.docker.io/bitnamicharts/nginx",
            "--wait",
            "--timeout",
            "30s",
        )
        if install.returncode != 0:
            pytest.skip(f"helm install fixture unavailable: {install.stderr[:300]}")
        env = _run_helm_status(namespace, release)
        assert env["success"] is True
        assert env["data"]["status"] == "deployed"
        assert "history" in env["data"]
    finally:
        _helm("-n", namespace, "uninstall", release, "--ignore-not-found")


def test_helm_status_list_pending_json(kind_cluster) -> None:
    _require_helm()
    env = _run_helm_status("--list-pending")
    assert env["success"] is True
    assert env["data"]["list_pending"] is True
    assert isinstance(env["data"]["pending_releases"], list)
