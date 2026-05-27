"""Live integration tests for diagnose_pod against a kind cluster.

Requires:
* ``kind`` binary on PATH (otherwise auto-skipped by conftest.py).
* ``kubectl`` binary on PATH (auto-skipped otherwise).

These tests spin up a session-scoped kind cluster and apply three
fixture Pods (crashloop / healthy / pending). They run only under
``pytest -m live_k8s``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_k8s

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAGNOSE_POD = (
    REPO_ROOT
    / "plugins"
    / "k8s-debug"
    / "skills"
    / "k8s-debug"
    / "scripts"
    / "diagnose_pod.py"
)


def _run_diagnose(namespace: str, pod: str, *extra: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(DIAGNOSE_POD), "local", namespace, pod, "--json", *extra],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode in (0, 1, 80, 81, 82, 83, 84, 85, 86, 87, 124), (
        f"diagnose_pod exited unexpectedly: rc={proc.returncode}\n"
        f"stdout={proc.stdout[:500]}\nstderr={proc.stderr[:500]}"
    )
    return json.loads(proc.stdout)


def test_crashloop_pod_real_kind(fixture_pods) -> None:
    env = _run_diagnose(fixture_pods["namespace"], "crashloop-pod")
    assert env["success"] is True
    assert env["exit_code"] == 0
    data = env["data"]
    # The Pod oscillates between CrashLoopBackOff (waiting) and a brief
    # Running interval mid-restart; accept any of the failing-pod phases.
    assert data["phase"] in (
        "CrashLoopBackOff",
        "Running-Restarting",
        "Running-NotReady",
    ), f"unexpected phase: {data['phase']}"
    # All three phases have a hint set.
    pod_finding = next(f for f in data["findings"] if f.get("kind") == "Pod")
    assert "hint" in pod_finding, f"missing hint on finding: {pod_finding}"
    # --previous is auto-triggered when classifier returns CrashLoopBackOff
    # (per design D3). For the transient Running-* phases the user would
    # need to pass --previous explicitly; that path is covered by unit tests.
    if data["phase"] == "CrashLoopBackOff":
        assert "previous_logs" in data["raw"], (
            "CrashLoopBackOff should auto-trigger --previous"
        )


def test_healthy_pod_real_kind(fixture_pods) -> None:
    env = _run_diagnose(fixture_pods["namespace"], "healthy-pod")
    assert env["success"] is True
    assert env["exit_code"] == 0
    data = env["data"]
    assert data["phase"] == "Running-Healthy", f"got {data['phase']}"
    assert data["summary"]["pods"] == "ok"
    # Healthy phase has no hint set.
    pod_finding = next(f for f in data["findings"] if f.get("kind") == "Pod")
    assert pod_finding.get("severity") == "ok"


def test_pending_pod_real_kind(fixture_pods) -> None:
    env = _run_diagnose(fixture_pods["namespace"], "pending-pod")
    assert env["success"] is True
    assert env["exit_code"] == 0
    data = env["data"]
    assert data["phase"] == "Pending", f"got {data['phase']}"
    # Expect at least one Event finding with FailedScheduling.
    failed_sched = [
        f
        for f in data["findings"]
        if f.get("kind") == "Event" and f.get("reason") == "FailedScheduling"
    ]
    assert failed_sched, (
        f"expected FailedScheduling event finding, got findings={data['findings']}"
    )
