"""Live SSH integration tests for ssh-guarded ``exec_detached.py``.

Run with:
    uv run pytest tests/test_exec_detached_live.py -m live_ssh -q
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import live_support  # noqa: E402
import pytest
from live_support import require_live_ssh  # noqa: E402

pytestmark = pytest.mark.live_ssh

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)
EXEC_DETACHED = SCRIPTS_DIR / "exec_detached.py"
POC_HOST = live_support.poc_host()
PROJECT_ID = "exec-detached-it"


def _state_reports_root() -> Path:
    """Where ssh-guarded writes job state (GAP-006), mirroring lib/state.py."""
    raw = (os.environ.get("SSH_GUARDED_HOME") or "").strip()
    home = Path(raw).expanduser() if raw else Path.home() / ".devops" / "ssh-guarded"
    return home / "reports"


#: ``$SSH_GUARDED_HOME/reports/<project_id>/jobs`` — never the plugin tree.
JOBS_DIR = _state_reports_root() / PROJECT_ID / "jobs"

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import RequestArtifact, RiskSummary  # noqa: E402, I001

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))

POC_HOST = live_support.poc_host()
PROJECT_ID = "exec-detached-it"
EXIT_REQUEST_NOT_EXECUTABLE = 60


@pytest.fixture(scope="module", autouse=True)
def _require_live_ssh() -> None:
    """Module-level precondition; the shared guard names what is missing."""
    require_live_ssh()


def _iso(seconds: int = 3600) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _artifact_path(tmp_path: Path, command: str, remote_workdir: str) -> Path:
    request_id = f"command-{uuid.uuid4().hex[:8]}"
    art = RequestArtifact(
        version=1,
        request_id=request_id,
        operation="command",
        server=POC_HOST,
        reason="exec_detached live test",
        created_at=_iso(0),
        expires_at=_iso(3600),
        payload={"command": command, "timeout": 60, "workdir": remote_workdir},
        risk_summary=RiskSummary(
            irreversible=False,
            scope="single-file",
            blast_radius="exec_detached live test",
        ),
    )
    path = tmp_path / f"{request_id}.json"
    art.save(path)
    return path


def _run_exec(*args: str, timeout: int = 90) -> tuple[dict[str, Any], str, int]:
    env = dict(os.environ)
    env["SSH_GUARDED_PROJECT_ID"] = PROJECT_ID
    proc = subprocess.run(
        [
            sys.executable,
            str(EXEC_DETACHED),
            *args,
            "--json",
            "--project-id",
            PROJECT_ID,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"exec_detached produced non-JSON stdout: {exc}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
        raise
    return payload, proc.stderr, proc.returncode


def _ssh(command: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", POC_HOST, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _cleanup(remote_workdir: str, job_ids: list[str]) -> None:
    _ssh(f"rm -rf {remote_workdir}", timeout=20)
    for job_id in job_ids:
        (JOBS_DIR / f"{job_id}.json").unlink(missing_ok=True)


def test_run_sleep_then_status_then_exited(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex[:8]
    remote_workdir = f"~/workspace/{PROJECT_ID}/{suffix}"
    job_ids: list[str] = []
    try:
        art = _artifact_path(tmp_path, "sleep 5; echo done", remote_workdir)
        env, _, rc = _run_exec("run", "--request", str(art))
        assert rc == 0, env
        job_id = env["data"]["job_id"]
        job_ids.append(job_id)

        status, _, status_rc = _run_exec("status", job_id)
        assert status_rc == 0, status
        assert status["data"]["state"] == "running"

        waited, _, wait_rc = _run_exec("wait", job_id, "--timeout", "20", timeout=30)
        assert wait_rc == 0, waited
        assert waited["data"]["state"] == "exited"
        assert waited["data"]["exit_code"] == 0
    finally:
        _cleanup(remote_workdir, job_ids)


def test_tail_log_fetches_command_output(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex[:8]
    remote_workdir = f"~/workspace/{PROJECT_ID}/{suffix}"
    job_ids: list[str] = []
    try:
        art = _artifact_path(
            tmp_path,
            "for i in 1 2 3; do echo line-$i; sleep 1; done",
            remote_workdir,
        )
        env, _, rc = _run_exec("run", "--request", str(art))
        assert rc == 0, env
        job_id = env["data"]["job_id"]
        job_ids.append(job_id)
        waited, _, wait_rc = _run_exec("wait", job_id, "--timeout", "20", timeout=30)
        assert wait_rc == 0, waited

        tail, _, tail_rc = _run_exec("tail-log", job_id, "--lines", "10")
        assert tail_rc == 0, tail
        joined = "\n".join(tail["data"]["lines"])
        assert "line-1" in joined
        assert "line-2" in joined
        assert "line-3" in joined
    finally:
        _cleanup(remote_workdir, job_ids)


def test_duplicate_run_returns_60(tmp_path: Path) -> None:
    suffix = uuid.uuid4().hex[:8]
    remote_workdir = f"~/workspace/{PROJECT_ID}/{suffix}"
    job_ids: list[str] = []
    try:
        art = _artifact_path(tmp_path, "sleep 3; echo duplicate", remote_workdir)
        env, _, rc = _run_exec("run", "--request", str(art))
        assert rc == 0, env
        job_id = env["data"]["job_id"]
        job_ids.append(job_id)

        env2, _, rc2 = _run_exec("run", "--request", str(art))
        assert rc2 == EXIT_REQUEST_NOT_EXECUTABLE, env2
        assert env2["data"]["failure_class"] == "request_not_executable"
        assert env2["data"]["subclass"] == "request_already_executed"
        assert env2["data"]["existing_job_id"] == job_id
        _run_exec("wait", job_id, "--timeout", "15", timeout=25)
    finally:
        _cleanup(remote_workdir, job_ids)
