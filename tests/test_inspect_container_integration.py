"""Integration tests for docker-quick/inspect_container v1.0.

Marker: ``live_ssh`` (reused for any test that needs real external tools;
the name is historical from ssh-core). Skipped by default. Run with::

    pytest tests/test_inspect_container_integration.py -v -m live_ssh

These tests need a working ``docker`` CLI on PATH. They use the ``local``
route only -- no SSH involvement -- so they don't require ssh-agent or
remote alias. Skipped at module load when ``docker`` is missing.

Goal: smoke-verify the local-route shell-out path end-to-end against a
real docker (or podman) daemon. Mock-driven unit tests cover the
score/extract/route logic; these confirm the actual subprocess wiring.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
INSPECT_CTR = (
    REPO_ROOT
    / "plugins"
    / "docker-quick"
    / "skills"
    / "docker-quick"
    / "scripts"
    / "inspect_container.py"
)
RUNTIME = os.environ.get("DOCKER_QUICK_RUNTIME", "docker")
SMOKE_IMAGE = os.environ.get("DOCKER_QUICK_SMOKE_IMAGE", "busybox:latest")

EXIT_OK = 0
EXIT_FAIL = 1


def _runtime_available() -> bool:
    if shutil.which(RUNTIME) is None:
        return False
    try:
        proc = subprocess.run(
            [RUNTIME, "info"], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    if not _runtime_available():
        pytest.skip(
            f"{RUNTIME!r} not on PATH or daemon unreachable; "
            "override with DOCKER_QUICK_RUNTIME=podman if needed."
        )


@pytest.fixture
def busybox_sleeper() -> Iterator[str]:
    """Start a busybox container that sleeps for 60s. Tear it down after."""
    name = f"devops-skills-test-{uuid.uuid4().hex[:8]}"
    start = subprocess.run(
        [RUNTIME, "run", "-d", "--rm", "--name", name, SMOKE_IMAGE, "sleep", "60"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if start.returncode != 0:
        pytest.skip(
            f"could not start {SMOKE_IMAGE} container: {start.stderr!r}"
        )
    try:
        yield name
    finally:
        subprocess.run(
            [RUNTIME, "kill", name],
            capture_output=True, text=True, timeout=15, check=False,
        )


def _run_envelope(*args: str, timeout: int = 30) -> dict:
    argv = [sys.executable, str(INSPECT_CTR), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"inspect_container produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_inspect_local_nonexistent_container_returns_failure_envelope() -> None:
    """A container that doesn't exist should yield success=false with a
    clean failure_class, not a crash."""
    result = _run_envelope(
        "local",
        f"definitely-does-not-exist-{uuid.uuid4().hex[:8]}",
        "--runtime", RUNTIME,
    )

    assert result["success"] is False, result
    assert result["data"]["failure_class"] == "remote_error", result
    # Envelope shape intact even in failure.
    assert "summary" in result["data"]
    assert "findings" in result["data"]


def test_inspect_local_running_container_smoke(busybox_sleeper: str) -> None:
    """Running a real container -> inspect_container should mark it ok or
    warn (not crit), and extract the basic state fields."""
    result = _run_envelope(
        "local", busybox_sleeper,
        "--runtime", RUNTIME, "--tail", "5",
    )

    assert result["data"]["target"] == busybox_sleeper
    assert result["data"]["host"] == "local"
    assert result["data"]["summary"]["state"] in {"ok", "warn"}, result
    inspect = result["data"]["raw"]["inspect"]
    assert inspect["state"]["status"] == "running"
    assert inspect["state"]["running"] is True
