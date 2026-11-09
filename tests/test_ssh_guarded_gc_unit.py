"""Unit tests for ssh-guarded artefact cleanup (``lib/gc.py`` + ``gc_artifacts.py``).

Nothing here touches a network or a real reports/ directory: every case builds
its own tree under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "ssh-guarded" / "skills" / "ssh-guarded" / "scripts"
GC_CLI = SCRIPTS / "gc_artifacts.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.helpers_gc import load_gc, load_state  # noqa: E402

gc = load_gc()
state = load_state()

DAY = 86400


def _touch(path: Path, age_days: float, *, payload: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        path.write_text("{}", encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - age_days * DAY
    os.utime(path, (old, old))
    return path


# -----------------------------------------------------------------------------
# parse_age
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("7d", 7 * DAY),
        ("12h", 12 * 3600),
        ("30m", 30 * 60),
        ("90s", 90),
        ("3600", 3600),
        ("  2d  ", 2 * DAY),
        ("3D", 3 * DAY),
    ],
)
def test_parse_age_accepts_documented_forms(spec: str, expected: int) -> None:
    assert gc.parse_age(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "1w", "0d", "-3d", "7dd"])
def test_parse_age_rejects_everything_else(spec: str) -> None:
    with pytest.raises(ValueError):
        gc.parse_age(spec)


# -----------------------------------------------------------------------------
# gc_reports
# -----------------------------------------------------------------------------


def test_old_artefact_is_deleted_when_applying(tmp_path: Path) -> None:
    target = _touch(tmp_path / "requests" / "command-abcdef12.json", 10)
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.deleted == 1
    assert not target.exists()


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    target = _touch(tmp_path / "requests" / "command-abcdef12.json", 10)
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=True)
    assert result.would_delete == 1
    assert result.deleted == 0
    assert target.exists()


def test_young_artefact_is_kept(tmp_path: Path) -> None:
    target = _touch(tmp_path / "requests" / "command-abcdef12.json", 1)
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.deleted == 0
    assert target.exists()


def test_files_that_are_not_artefacts_are_ignored(tmp_path: Path) -> None:
    keep = _touch(tmp_path / "requests" / "notes.txt", 30)
    also_keep = _touch(tmp_path / "README.md", 30)
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.scanned == 0
    assert keep.exists() and also_keep.exists()


def test_still_valid_request_is_kept_even_when_old(tmp_path: Path) -> None:
    """A drafted request waiting for review must not vanish under the user."""
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + DAY))
    target = _touch(
        tmp_path / "requests" / "command-abcdef12.json",
        30,
        payload={"expires_at": future},
    )
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.deleted == 0
    assert target.exists()
    assert any("still valid" in e.reason for e in result.entries)


def test_expired_request_is_deleted(tmp_path: Path) -> None:
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - DAY))
    target = _touch(
        tmp_path / "requests" / "delete-abcdef12.json",
        30,
        payload={"expires_at": past},
    )
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.deleted == 1
    assert not target.exists()


def test_result_and_job_files_are_cleaned(tmp_path: Path) -> None:
    _touch(tmp_path / "requests" / "command-abcdef12.result.json", 10)
    _touch(tmp_path / "jobs" / "job-12345678.json", 10)
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.deleted == 2


def test_missing_reports_dir_is_not_an_error(tmp_path: Path) -> None:
    result = gc.gc_reports(tmp_path / "nope", 7 * DAY, dry_run=False)
    assert result.scanned == 0
    assert result.errors == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="flock semantics")
def test_held_lock_is_never_deleted(tmp_path: Path) -> None:
    import fcntl
    import os

    lock = _touch(tmp_path / "requests" / "command-abcdef12.lock", 30)
    fd = os.open(str(lock), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
        assert result.deleted == 0
        assert lock.exists()
    finally:
        os.close(fd)


def test_stale_lock_is_deleted_on_posix(tmp_path: Path) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("flock semantics")
    lock = _touch(tmp_path / "requests" / "command-abcdef12.lock", 30)
    result = gc.gc_reports(tmp_path, 7 * DAY, dry_run=False)
    assert result.deleted == 1
    assert not lock.exists()


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def _run(*args: str, env_home: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    if env_home is not None:
        env["SSH_GUARDED_HOME"] = str(env_home)
    return subprocess.run(
        [sys.executable, str(GC_CLI), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def _project_tree(root: Path, project: str = "proj-a") -> Path:
    """Create ``<root>/<project>/{requests,jobs}`` and return the project dir."""
    project_dir = root / project
    (project_dir / "requests").mkdir(parents=True, exist_ok=True)
    (project_dir / "jobs").mkdir(parents=True, exist_ok=True)
    return project_dir


def test_cli_help_exits_zero() -> None:
    proc = _run("--help")
    assert proc.returncode == 0
    assert "--older-than" in proc.stdout
    assert "--apply" in proc.stdout
    assert "--reports-dir" in proc.stdout


def test_cli_defaults_to_dry_run(tmp_path: Path) -> None:
    project = _project_tree(tmp_path)
    _touch(project / "requests" / "command-abcdef12.json", 10)
    proc = _run("--reports-dir", str(tmp_path), "--json")
    assert proc.returncode == 0
    env = json.loads(proc.stdout)
    assert env["data"]["dry_run"] is True
    assert env["data"]["would_delete"] == 1
    assert env["data"]["deleted"] == 0


def test_cli_apply_deletes(tmp_path: Path) -> None:
    project = _project_tree(tmp_path)
    target = _touch(project / "requests" / "command-abcdef12.json", 10)
    proc = _run("--reports-dir", str(tmp_path), "--apply", "--json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["data"]["deleted"] == 1
    assert not target.exists()


def test_cli_project_id_scopes_the_sweep(tmp_path: Path) -> None:
    keep_project = _project_tree(tmp_path, "proj-keep")
    sweep_project = _project_tree(tmp_path, "proj-sweep")
    kept = _touch(keep_project / "requests" / "command-aaaaaaaa.json", 10)
    swept = _touch(sweep_project / "requests" / "command-bbbbbbbb.json", 10)
    proc = _run(
        "--reports-dir", str(tmp_path), "--project-id", "proj-sweep",
        "--apply", "--json",
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["data"]["deleted"] == 1
    assert not swept.exists()
    assert kept.exists(), "another project's artifacts must not be touched"


def test_cli_sweeps_every_project_by_default(tmp_path: Path) -> None:
    a = _project_tree(tmp_path, "proj-a")
    b = _project_tree(tmp_path, "proj-b")
    _touch(a / "requests" / "command-aaaaaaaa.json", 10)
    _touch(b / "jobs" / "job-bbbbbbbb.json", 10)
    proc = _run("--reports-dir", str(tmp_path), "--apply", "--json")
    assert proc.returncode == 0
    env = json.loads(proc.stdout)
    assert env["data"]["deleted"] == 2
    assert sorted(env["data"]["projects"]) == ["proj-a", "proj-b"]


def test_cli_bad_duration_is_an_argv_error(tmp_path: Path) -> None:
    proc = _run("--reports-dir", str(tmp_path), "--older-than", "soon", "--json")
    assert proc.returncode == 2
    assert "soon" in proc.stderr
    env = json.loads(proc.stdout)
    assert env["exit_code"] == 2
    assert env["error"]["exit_code"] == 2


def test_cli_rejects_both_modes(tmp_path: Path) -> None:
    proc = _run("--reports-dir", str(tmp_path), "--dry-run", "--apply")
    assert proc.returncode == 2


# -----------------------------------------------------------------------------
# State-dir resolution (GAP-006)
# -----------------------------------------------------------------------------


def test_reports_root_defaults_to_ssh_guarded_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``$SSH_GUARDED_HOME`` is the only thing that decides the location."""
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    assert state.reports_root() == tmp_path / "state" / "reports"
    assert state.requests_dir("proj-a") == (
        tmp_path / "state" / "reports" / "proj-a" / "requests"
    )
    assert state.jobs_dir("proj-a") == (
        tmp_path / "state" / "reports" / "proj-a" / "jobs"
    )


def test_reports_root_default_is_under_user_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSH_GUARDED_HOME", raising=False)
    resolved = state.reports_root()
    assert resolved == Path.home() / ".devops" / "ssh-guarded" / "reports"


def test_reports_dir_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    override = tmp_path / "ci-reports"
    assert state.reports_root(reports_dir=override) == override
    assert state.requests_dir("p", reports_dir=override) == (
        override / "p" / "requests"
    )


def test_state_home_is_never_the_plugin_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    plugin_reports = (
        REPO_ROOT
        / "plugins"
        / "ssh-guarded"
        / "skills"
        / "ssh-guarded"
        / "reports"
    )
    resolved = state.reports_root()
    assert resolved != plugin_reports
    assert "plugins" not in resolved.parts


def test_cli_uses_ssh_guarded_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: gc follows ``$SSH_GUARDED_HOME`` with no ``--reports-dir``."""
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    project = _project_tree(tmp_path / "state" / "reports")
    _touch(project / "requests" / "command-abcdef12.json", 10)
    proc = _run("--json")
    assert proc.returncode == 0
    env = json.loads(proc.stdout)
    assert env["data"]["root"] == str(tmp_path / "state" / "reports")
    assert env["data"]["would_delete"] == 1


def test_gc_artifacts_never_writes_into_plugin_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No new files appear under the plugin's own ``reports/`` (GAP-006)."""
    monkeypatch.setenv("SSH_GUARDED_HOME", str(tmp_path / "state"))
    plugin_reports = (
        REPO_ROOT
        / "plugins"
        / "ssh-guarded"
        / "skills"
        / "ssh-guarded"
        / "reports"
    )
    project = _project_tree(tmp_path / "state" / "reports")
    _touch(project / "requests" / "command-abcdef12.json", 10)
    before = sorted(str(p) for p in plugin_reports.rglob("*") if p.is_file())
    proc = _run("--apply", "--json")
    assert proc.returncode == 0
    after = sorted(str(p) for p in plugin_reports.rglob("*") if p.is_file())
    assert before == after
    # The documentation file is the only thing the tree is meant to keep.
    assert (plugin_reports / "README.md").is_file()
