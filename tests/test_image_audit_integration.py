"""Integration tests for docker-quick/image_audit v1.0.

Marker: ``live_ssh`` (reused from ssh-core's marker convention).
Skipped by default. Run with::

    pytest tests/test_image_audit_integration.py -v -m live_ssh

These tests need a working ``docker`` CLI on PATH. They use the ``local``
route only -- no SSH involvement -- so they don't require ssh-agent or
a remote alias. Skipped at module load when ``docker`` is missing.

Smoke-verify the local route shell-out path end-to-end against real
``docker history`` + ``docker inspect`` output. Mock-driven unit tests
cover parser/scoring/route logic; these confirm the actual subprocess
wiring and that real docker's JSON Lines history shape parses cleanly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_AUDIT = (
    REPO_ROOT
    / "plugins"
    / "docker-quick"
    / "skills"
    / "docker-quick"
    / "scripts"
    / "image_audit.py"
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


@pytest.fixture(scope="module")
def _ensure_smoke_image() -> str:
    """Make sure SMOKE_IMAGE exists locally; pull if missing."""
    inspect = subprocess.run(
        [RUNTIME, "inspect", SMOKE_IMAGE],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if inspect.returncode != 0:
        pull = subprocess.run(
            [RUNTIME, "pull", SMOKE_IMAGE],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if pull.returncode != 0:
            pytest.skip(
                f"could not pull {SMOKE_IMAGE}: {pull.stderr!r}"
            )
    return SMOKE_IMAGE


def _run_envelope(*args: str, timeout: int = 30) -> dict:
    argv = [sys.executable, str(IMAGE_AUDIT), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"image_audit produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def test_image_audit_nonexistent_image_returns_failure_envelope() -> None:
    """A nonexistent image must yield success=false with
    failure_class=remote_error, not a crash."""
    result = _run_envelope(
        "local",
        f"nonexistent-image-{uuid.uuid4().hex[:8]}:tag-xyz",
        "--runtime", RUNTIME,
    )
    assert result["success"] is False, result
    assert result["data"]["failure_class"] == "remote_error", result
    assert "summary" in result["data"]
    assert "layers" in result["data"]


def test_image_audit_busybox_smoke(_ensure_smoke_image: str) -> None:
    """A real image -> history JSON Lines parses, layers list non-empty,
    every layer has integer size_bytes."""
    result = _run_envelope(
        "local", _ensure_smoke_image,
        "--runtime", RUNTIME, "--threshold-mb", "1",
    )
    assert result["data"]["host"] == "local"
    assert result["data"]["image"] == _ensure_smoke_image
    layers = result["data"]["layers"]
    assert len(layers) >= 1, result
    for layer in layers:
        assert isinstance(layer["size_bytes"], int), layer
        assert "created_by" in layer
    # busybox is tiny but threshold 1MB may still mark a layer warn -- both
    # ok and warn are valid here, but state must not be crit.
    assert result["data"]["summary"]["state"] in {"ok", "warn"}, result
    assert result["data"]["summary"]["layer_count"] == len(layers)
