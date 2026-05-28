"""Unit tests for ssh-guarded ``lib/sandbox.py``.

12 cases per design.md §3 D12:
- 4 for ``resolve_project_id`` (explicit, env, git, hard-fail)
- 5 for ``is_inside_sandbox`` happy + escape attempts
- 5 for ``is_in_denylist`` per pattern type

Total 14 ≥ 12 target (extra coverage for normalize_remote_path edge cases).
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = (
    REPO_ROOT
    / "plugins"
    / "ssh-guarded"
    / "skills"
    / "ssh-guarded"
    / "scripts"
)

# Each plugin ships its own ``lib/`` package; the first test file to import
# one wins ``sys.modules['lib']`` for the rest of the pytest collection.
# Evict any cached ``lib*`` before loading the ssh-guarded lib, then restore.
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import sandbox as _sandbox_mod  # noqa: E402, I001
from lib._errors import ArtifactError as _ArtifactError  # noqa: E402, I001

# Bind to module-level names so tests below don't need the import dance.
sandbox = _sandbox_mod
ArtifactError = _ArtifactError

# Restore previous lib state so subsequent test modules behave normally.
for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


# --- resolve_project_id ---------------------------------------------------


def test_resolve_project_id_explicit_flag_wins(tmp_path: Path) -> None:
    pid = sandbox.resolve_project_id(
        _ns(project_id="explicit-pid"),
        env={"SSH_GUARDED_PROJECT_ID": "env-pid"},
        cwd=tmp_path,
    )
    assert pid == "explicit-pid"


def test_resolve_project_id_env_when_no_flag(tmp_path: Path) -> None:
    # No project_id attribute on args.
    pid = sandbox.resolve_project_id(
        _ns(),
        env={"SSH_GUARDED_PROJECT_ID": "env-pid"},
        cwd=tmp_path,
    )
    assert pid == "env-pid"


def test_resolve_project_id_git_basename(tmp_path: Path) -> None:
    fake_root = tmp_path / "myproject"
    fake_root.mkdir()

    def fake_check_output(argv: list[str], **kw: Any) -> str:
        assert argv == ["git", "rev-parse", "--show-toplevel"]
        return f"{fake_root}\n"

    with patch.object(subprocess, "check_output", side_effect=fake_check_output):
        pid = sandbox.resolve_project_id(_ns(), env={}, cwd=tmp_path)
    assert pid == "myproject"


def test_resolve_project_id_hard_fail_when_all_missing(tmp_path: Path) -> None:
    def fake_check_output(argv: list[str], **kw: Any) -> str:
        raise subprocess.CalledProcessError(returncode=128, cmd=argv)

    with (
        patch.object(subprocess, "check_output", side_effect=fake_check_output),
        pytest.raises(ArtifactError) as exc_info,
    ):
        sandbox.resolve_project_id(_ns(), env={}, cwd=tmp_path)
    assert exc_info.value.failure_class == "project_id_unresolved"
    assert exc_info.value.exit_code == 1


# --- is_inside_sandbox -----------------------------------------------------


def test_is_inside_sandbox_happy_root() -> None:
    assert sandbox.is_inside_sandbox("~/workspace/proj", "proj")


def test_is_inside_sandbox_happy_nested() -> None:
    assert sandbox.is_inside_sandbox("~/workspace/proj/staging/2026.log", "proj")


def test_is_inside_sandbox_escape_dotdot() -> None:
    assert not sandbox.is_inside_sandbox("~/workspace/proj/../etc/passwd", "proj")


def test_is_inside_sandbox_other_project_rejected() -> None:
    assert not sandbox.is_inside_sandbox("~/workspace/otherproj/file", "proj")


def test_is_inside_sandbox_absolute_path_rejected() -> None:
    assert not sandbox.is_inside_sandbox("/etc/passwd", "proj")


def test_is_inside_sandbox_workspace_root_no_pid_rejected() -> None:
    assert not sandbox.is_inside_sandbox("~/workspace/", "proj")


# --- is_in_denylist --------------------------------------------------------


@pytest.mark.parametrize(
    "path,reason",
    [
        ("~/.ssh/authorized_keys", "ssh prefix"),
        ("~/workspace/proj/.env", "dotenv basename"),
        ("~/workspace/proj/.env.local", "dotenv variant"),
        ("~/workspace/proj/id_rsa", "private key basename"),
        ("~/workspace/proj/server.pem", "pem extension"),
        ("~/workspace/proj/backup.key", "key extension"),
        ("~/workspace/proj/identity.p12", "p12 extension"),
        ("~/workspace/proj/cert.pfx", "pfx extension"),
        ("~/workspace/proj/known_hosts", "known_hosts basename"),
        ("~/.aws/credentials", "aws prefix"),
        ("~/.kube/config", "kube prefix"),
        ("~/.gnupg/secring.gpg", "gnupg prefix"),
        ("~/.config/gh/hosts.yml", "github prefix"),
    ],
)
def test_is_in_denylist_hits(path: str, reason: str) -> None:
    assert sandbox.is_in_denylist(path), f"expected hit ({reason}): {path}"


def test_is_in_denylist_misses() -> None:
    assert not sandbox.is_in_denylist("~/workspace/proj/release.tar.gz")
    assert not sandbox.is_in_denylist("~/workspace/proj/logs/app.log")


# --- normalize_remote_path edge cases --------------------------------------


def test_normalize_remote_path_collapses_dotdot() -> None:
    assert sandbox.normalize_remote_path("~/foo/bar/../baz") == "~/foo/baz"


def test_normalize_remote_path_rejects_escape() -> None:
    with pytest.raises(ArtifactError) as exc_info:
        sandbox.normalize_remote_path("~/../etc")
    assert exc_info.value.exit_code == 50
    assert exc_info.value.failure_class == "workdir_outside_sandbox"


def test_normalize_remote_path_rejects_null_byte() -> None:
    with pytest.raises(ArtifactError):
        sandbox.normalize_remote_path("~/foo\x00bar")
