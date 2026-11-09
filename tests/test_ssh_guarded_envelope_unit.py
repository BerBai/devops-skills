"""Envelope contract for every ssh-guarded script (GAP-005).

The marketplace promises one shape::

    {"success", "exit_code", "stdout", "stderr", "data"}

with ``data.failure_class`` on failure. ssh-guarded used to emit
``{success, data, error}`` only, so ``jq -r .exit_code`` returned ``null`` for
every one of its scripts — including on the argparse-error path, which wrote
nothing to stdout at all.

These cases are subprocess-level on purpose: they assert what a caller actually
receives, not what a Python helper returns. Nothing here contacts a host: every
failure is raised before any transport call (bad argv, unreadable artifact,
missing catalog, empty stdin, job lookup on an empty state dir).
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "plugins" / "ssh-guarded" / "skills" / "ssh-guarded" / "scripts"

#: The public surface. Every entry is `<script>: <argv that fails locally>`.
_GUARDED_SCRIPTS = [
    "request_command.py",
    "request_upload.py",
    "request_mkdir.py",
    "request_delete.py",
    "run_request.py",
    "exec_detached.py",
    "gc_artifacts.py",
    "redact_check.py",
    "scan_software.py",
]

_UNIVERSAL_KEYS = {"success", "exit_code", "stdout", "stderr", "data", "error"}


def _run(
    script: str,
    *args: str,
    env_home: Path | None = None,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    env = dict(__import__("os").environ)
    env["PYTHONUTF8"] = "1"
    env.pop("SSH_GUARDED_PROJECT_ID", None)
    env["SSH_GUARDED_PROJECT_ID"] = "envelope-proj"
    if env_home is not None:
        env["SSH_GUARDED_HOME"] = str(env_home)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
        input=stdin,
        env=env,
    )


def _envelope(proc: subprocess.CompletedProcess[str]) -> dict:
    assert proc.stdout.strip(), (
        f"no JSON on stdout (stderr={proc.stderr!r}, rc={proc.returncode})"
    )
    return json.loads(proc.stdout)


# -----------------------------------------------------------------------------
# Universal shape
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("script", _GUARDED_SCRIPTS)
def test_argparse_error_still_emits_the_envelope(
    script: str, tmp_path: Path
) -> None:
    """``--json`` + an unknown flag must not produce an empty stdout."""
    proc = _run(script, "--json", "--definitely-not-a-flag")
    assert proc.returncode == 2
    env = _envelope(proc)
    assert set(env) == _UNIVERSAL_KEYS
    assert env["success"] is False
    assert env["exit_code"] == 2
    assert env["error"]["failure_class"] == "invalid_arguments"
    assert env["error"]["exit_code"] == env["exit_code"]
    assert env["stdout"] == ""
    assert env["stderr"]


@pytest.mark.parametrize("script", _GUARDED_SCRIPTS)
def test_exit_code_is_never_null(script: str) -> None:
    """``jq -r .exit_code`` must always print a number, never ``null``."""
    proc = _run(script, "--json", "--definitely-not-a-flag")
    env = _envelope(proc)
    assert isinstance(env["exit_code"], int)
    assert env["exit_code"] == proc.returncode


@pytest.mark.parametrize("script", _GUARDED_SCRIPTS)
def test_error_exit_code_matches_top_level(script: str) -> None:
    proc = _run(script, "--json", "--definitely-not-a-flag")
    env = _envelope(proc)
    assert env["error"]["exit_code"] == env["exit_code"]
    assert env["error"]["failure_class"] == "invalid_arguments"


@pytest.mark.parametrize("script", _GUARDED_SCRIPTS)
def test_human_mode_argparse_error_writes_stderr_not_json(script: str) -> None:
    """Without ``--json`` the human path stays clean (usage on stderr only)."""
    proc = _run(script, "--definitely-not-a-flag")
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "error:" in proc.stderr


# -----------------------------------------------------------------------------
# Per-script failure envelopes (real, local failures)
# -----------------------------------------------------------------------------


def test_run_request_failure_envelope(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    proc = _run("run_request.py", "--request", str(missing), "--json")
    env = _envelope(proc)
    assert set(env) == _UNIVERSAL_KEYS
    assert proc.returncode == 53
    assert env["exit_code"] == 53
    assert env["error"]["failure_class"] == "artifact_invalid"
    assert env["error"]["exit_code"] == 53
    assert env["stderr"]


def test_request_command_failure_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "state"
    proc = _run(
        "request_command.py", "host", "  ", "--reason", "x", "--json",
        env_home=env_dir,
    )
    env = _envelope(proc)
    assert proc.returncode == 30
    assert env["exit_code"] == 30
    assert env["error"]["failure_class"] == "command_payload_invalid"
    assert env["data"] is None


def test_request_mkdir_failure_envelope(tmp_path: Path) -> None:
    proc = _run(
        "request_mkdir.py", "host", "--remote", "~/workspace/p/x",
        "--reason", "x", "--mode", "9XYZ", "--json",
        env_home=tmp_path / "state",
    )
    env = _envelope(proc)
    assert proc.returncode == 40
    assert env["exit_code"] == 40
    assert env["error"]["failure_class"] == "mode_invalid"


def test_request_upload_failure_envelope(tmp_path: Path) -> None:
    proc = _run(
        "request_upload.py", "host", "--local", str(tmp_path / "gone.bin"),
        "--remote", "~/workspace/p/x", "--reason", "x", "--json",
        env_home=tmp_path / "state",
    )
    env = _envelope(proc)
    assert proc.returncode == 35
    assert env["exit_code"] == 35
    assert env["error"]["failure_class"] == "local_path_missing"


def test_request_delete_failure_envelope(tmp_path: Path) -> None:
    """A resolution failure is exit 49, and still a full envelope."""
    proc = _run(
        "request_delete.py", "definitely-no-such-alias-xyz",
        "--remote", "~/workspace/p/x", "--reason", "x", "--json",
        env_home=tmp_path / "state",
    )
    env = _envelope(proc)
    assert set(env) == _UNIVERSAL_KEYS
    assert proc.returncode == env["exit_code"]
    assert env["error"]["failure_class"] in (
        "pre_check_stat_failed",
        "recursive_required_for_directory",
    )
    assert env["error"]["exit_code"] == env["exit_code"]


def test_exec_detached_failure_envelope(tmp_path: Path) -> None:
    proc = _run(
        "exec_detached.py", "status", "abcd1234", "--json",
        env_home=tmp_path / "state",
    )
    env = _envelope(proc)
    assert proc.returncode == 65
    assert env["exit_code"] == 65
    assert env["error"]["failure_class"] == "job_state_corrupt"


def test_exec_detached_success_envelope(tmp_path: Path) -> None:
    proc = _run(
        "exec_detached.py", "list", "--json", env_home=tmp_path / "state",
    )
    env = _envelope(proc)
    assert set(env) == _UNIVERSAL_KEYS
    assert proc.returncode == 0
    assert env["exit_code"] == 0
    assert env["error"] is None
    assert env["data"]["jobs"] == []


def test_gc_artifacts_success_envelope(tmp_path: Path) -> None:
    proc = _run("gc_artifacts.py", "--json", env_home=tmp_path / "state")
    env = _envelope(proc)
    assert set(env) == _UNIVERSAL_KEYS
    assert env["exit_code"] == 0
    assert env["error"] is None


def test_redact_check_success_envelope() -> None:
    proc = _run("redact_check.py", "--json", stdin="nothing secret here\n")
    env = _envelope(proc)
    assert set(env) == _UNIVERSAL_KEYS
    assert env["success"] is True
    assert env["exit_code"] == 0
    assert env["error"] is None


def test_redact_check_failure_envelope(tmp_path: Path) -> None:
    proc = _run(
        "redact_check.py", "--json", "--catalog", str(tmp_path / "missing.json"),
        stdin="x\n",
    )
    env = _envelope(proc)
    assert proc.returncode == 70
    assert env["exit_code"] == 70
    assert env["error"]["failure_class"] == "catalog_load_failed"


def test_scan_software_failure_envelope(tmp_path: Path) -> None:
    proc = _run(
        "scan_software.py", "host", "--json",
        "--catalog", str(tmp_path / "missing-catalog.json"),
    )
    env = _envelope(proc)
    assert proc.returncode == 75
    assert env["exit_code"] == 75
    assert env["error"]["failure_class"] == "catalog_load_failed"
    assert env["error"]["exit_code"] == 75


# -----------------------------------------------------------------------------
# Cross-plugin parity
# -----------------------------------------------------------------------------


def test_envelope_matches_the_ssh_core_shape(tmp_path: Path) -> None:
    """Key set is a superset of ssh-core's; only ``error`` is extra."""
    proc = _run("run_request.py", "--request", "nope.json", "--json")
    env = _envelope(proc)
    core_keys = {"success", "exit_code", "stdout", "stderr", "data"}
    assert core_keys.issubset(env)
    assert set(env) - core_keys == {"error"}


def test_envelope_types_are_stable(tmp_path: Path) -> None:
    proc = _run("gc_artifacts.py", "--json", env_home=tmp_path / "state")
    env = _envelope(proc)
    assert isinstance(env["success"], bool)
    assert isinstance(env["exit_code"], int)
    assert isinstance(env["stdout"], str)
    assert isinstance(env["stderr"], str)
