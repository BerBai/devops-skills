"""Integration tests for k8s-debug/scripts/cluster_health.py v1.0."""

from __future__ import annotations

import json
import subprocess
import sys
import time
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
    / "cluster_health.py"
)


def _run_cluster_health() -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "local", "--json"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert proc.returncode in (0, 100), proc.stderr
    return json.loads(proc.stdout)


def _kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _wait_for_failed_pod(namespace: str, pod: str, timeout_s: int = 90) -> None:
    deadline = time.monotonic() + timeout_s
    last_phase = ""
    while time.monotonic() < deadline:
        proc = _kubectl("-n", namespace, "get", "pod", pod, "-o", "json")
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            last_phase = data.get("status", {}).get("phase", "")
            if last_phase == "Failed":
                return
        time.sleep(2)
    raise AssertionError(f"{pod} did not reach Failed phase; last_phase={last_phase}")


def test_cluster_health_kind_cluster_json(kind_cluster) -> None:
    env = _run_cluster_health()
    assert env["success"] is True
    summary = env["data"]["summary"]
    assert set(summary) >= {"nodes", "control_plane", "kube_system", "events", "versions"}
    assert summary["nodes"] in {"ok", "warn", "crit"}


def test_cluster_health_degraded_kube_system_json(kind_cluster) -> None:
    pod = "cluster-health-failed"
    _kubectl("-n", "kube-system", "delete", "pod", pod, "--ignore-not-found=true")
    try:
        created = _kubectl(
            "-n",
            "kube-system",
            "run",
            pod,
            "--image=busybox:1.36",
            "--restart=Never",
            "--",
            "sh",
            "-c",
            "exit 1",
        )
        assert created.returncode == 0, created.stderr
        _wait_for_failed_pod("kube-system", pod)

        env = _run_cluster_health()
        assert env["success"] is True
        assert env["data"]["summary"]["kube_system"] == "crit"
        assert any(
            f["domain"] == "kube_system" and f["name"] == pod and f["severity"] == "crit"
            for f in env["data"]["findings"]
        )
    finally:
        _kubectl("-n", "kube-system", "delete", "pod", pod, "--ignore-not-found=true")
