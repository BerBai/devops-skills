"""Integration tests for docker-quick/compose_status v1.0.

Marker: ``live_ssh`` (reused from ssh-core's marker convention).
Skipped by default. Run with::

    pytest tests/test_compose_status_integration.py -v -m live_ssh

These tests need a working ``docker`` CLI with the Compose v2 plugin on
PATH. They use the ``local`` route only -- no SSH involvement -- so they
don't require ssh-agent or a remote alias. Skipped at module load when
``docker compose version`` doesn't work.

Smoke-verify the local route shell-out path end-to-end against real
docker compose. Mock-driven unit tests cover parser/scoring/route logic;
these confirm the actual subprocess wiring and that compose v2's
``ps --format json`` output is one of the two shapes the parser handles.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_STATUS = (
    REPO_ROOT
    / "plugins"
    / "docker-quick"
    / "skills"
    / "docker-quick"
    / "scripts"
    / "compose_status.py"
)
RUNTIME = os.environ.get("DOCKER_QUICK_RUNTIME", "docker")
SMOKE_IMAGE = os.environ.get("DOCKER_QUICK_SMOKE_IMAGE", "busybox:latest")

EXIT_OK = 0
EXIT_FAIL = 1


def _compose_available() -> bool:
    if shutil.which(RUNTIME) is None:
        return False
    try:
        proc = subprocess.run(
            [RUNTIME, "compose", "version"],
            capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


@pytest.fixture(scope="module", autouse=True)
def _require_docker_compose() -> None:
    if not _compose_available():
        pytest.skip(
            f"{RUNTIME!r} compose plugin not on PATH or unreachable; "
            "override with DOCKER_QUICK_RUNTIME=podman if needed."
        )


@pytest.fixture
def minimal_stack(tmp_path: Path) -> Iterator[Path]:
    """Spin up a one-service busybox stack that sleeps for 60s. Tear it down."""
    project = tmp_path / f"compose-test-{uuid.uuid4().hex[:8]}"
    project.mkdir()
    compose_file = project / "compose.yml"
    compose_file.write_text(textwrap.dedent(f"""\
        services:
          sleeper:
            image: {SMOKE_IMAGE}
            command: sleep 60
            restart: "no"
        """))
    start = subprocess.run(
        [RUNTIME, "compose", "up", "-d"],
        cwd=project,
        capture_output=True, text=True, timeout=60, check=False,
    )
    if start.returncode != 0:
        pytest.skip(
            f"could not bring up minimal compose stack: "
            f"{start.stderr!r}"
        )
    try:
        yield project
    finally:
        subprocess.run(
            [RUNTIME, "compose", "down", "-v"],
            cwd=project,
            capture_output=True, text=True, timeout=30, check=False,
        )


def _run_envelope(*args: str, timeout: int = 30) -> dict:
    argv = [sys.executable, str(COMPOSE_STATUS), *args, "--json"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"compose_status produced non-JSON stdout: {e}\n"
            f"argv: {argv}\nstdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )
        raise


def test_compose_status_local_nonexistent_dir_returns_failure_envelope() -> None:
    """A project_dir that doesn't exist should yield success=false with
    failure_class=remote_error, not a crash."""
    result = _run_envelope(
        "local",
        f"/tmp/definitely-does-not-exist-{uuid.uuid4().hex[:8]}",
    )
    assert result["success"] is False, result
    assert result["data"]["failure_class"] == "remote_error", result
    assert "summary" in result["data"]
    assert "services" in result["data"]


def test_compose_status_local_running_stack(minimal_stack: Path) -> None:
    """A real running compose stack -> summary.state should be ok or warn
    (not crit) with services_total >= 1."""
    result = _run_envelope("local", str(minimal_stack))
    assert result["data"]["host"] == "local"
    assert result["data"]["project_dir"] == str(minimal_stack)
    assert result["data"]["summary"]["services_total"] >= 1, result
    assert result["data"]["summary"]["state"] in {"ok", "warn"}, result
    # The sleeper service should be visible in the listing.
    names = [
        s.get("Service") or s.get("Name") or ""
        for s in result["data"]["services"]
    ]
    assert any("sleeper" in n for n in names), names
