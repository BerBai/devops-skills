"""Unit tests for ssh-guarded ``request_delete.py`` drafter (10+ cases per D12).

Covers (design.md §3 D12 row "request_delete.py"):
- help exits 0 + lists v1.0 flags
- happy path file delete (stat → file)
- happy path dir + --recursive (stat → dir)
- dir without --recursive → exit 46
- denylist warn-but-write (stat path still resolved)
- SFTP/ssh stat failure → exit 49
- JSON envelope shape
- SUPPRESS smoke
- risk_summary.irreversible always true (D4)
- recursive flag drives scope=tree (D4)
- remote does not exist ("none") → drafter warns but writes (rm -f is no-op)
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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

_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import request_delete as _rd_mod  # noqa: E402

request_delete = _rd_mod

for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


@pytest.fixture
def tmp_requests_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    rd = tmp_path / "reports" / "requests"
    monkeypatch.setattr(request_delete, "_resolve_requests_dir", lambda: rd)
    return rd


@pytest.fixture(autouse=True)
def _stable_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "test-proj")


def _make_stat_run(envelope: dict[str, Any], rc: int = 0) -> Any:
    """Build a fake subprocess.run that returns an ssh_execute envelope."""
    completed = MagicMock()
    completed.returncode = rc
    completed.stdout = json.dumps(envelope)
    completed.stderr = ""
    def _run(*_a: Any, **_kw: Any) -> Any:
        return completed
    return _run


def _capture(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any] | None, str]:
    rc = request_delete.main(args)
    cap = capsys.readouterr()
    try:
        env = json.loads(cap.out) if cap.out.strip() else None
    except json.JSONDecodeError:
        env = None
    return rc, env, cap.err


def _env_ok(stdout_text: str) -> dict[str, Any]:
    return {
        "success": True, "exit_code": 0,
        "stdout": stdout_text, "stderr": "", "data": {},
    }


# --- Tests -----------------------------------------------------------------


def test_help_exits_zero_and_lists_v10_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        request_delete.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--remote", "--reason", "--recursive", "--expires-in",
                 "--project-id", "--json", "--no-redact"):
        assert flag in out
    # v1.1 deferred --force must NOT appear
    assert "--force" not in out


def test_happy_path_file(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("file\n")))
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/stale.log",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    art_path = Path(env["data"]["artifact_path"])
    body = json.loads(art_path.read_text())
    assert body["payload"]["recursive"] is False
    assert body["payload"]["is_directory_pre_check"] is False
    # D4: delete is always irreversible=true
    assert body["risk_summary"]["irreversible"] is True
    # File scope = single-file
    assert body["risk_summary"]["scope"] == "single-file"


def test_happy_path_dir_with_recursive(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("dir\n")))
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/old/",
         "--reason", "x", "--recursive", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())
    assert body["payload"]["recursive"] is True
    assert body["payload"]["is_directory_pre_check"] is True
    assert body["risk_summary"]["scope"] == "tree"
    assert body["risk_summary"]["irreversible"] is True


def test_dir_without_recursive_exits_46(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("dir")))
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/old",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 46
    assert env is not None
    assert env["error"]["failure_class"] == "recursive_required_for_directory"


def test_denylist_warns_but_writes(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("file")))
    rc, env, err = _capture(
        ["pai", "--remote", "~/.ssh/authorized_keys",
         "--reason", "x", "--json"],
        capsys,
    )
    # Drafter writes (executor will refuse).
    assert rc == 0
    assert env is not None and env["success"] is True
    # Two warnings: outside sandbox + denylist.
    assert "denylist" in err
    assert "outside" in err


def test_stat_subprocess_failure_exits_49(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed = MagicMock()
    failed.returncode = 1
    failed.stdout = ""
    failed.stderr = "ssh: connection refused"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: failed)
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 49
    assert env is not None
    assert env["error"]["failure_class"] == "pre_check_stat_failed"


def test_stat_envelope_not_success_exits_49(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ssh_execute returncode=0 but envelope.success=false (e.g. auth fail)."""
    bad = MagicMock()
    bad.returncode = 0
    bad.stdout = json.dumps({
        "success": False, "exit_code": 255,
        "stdout": "", "stderr": "Permission denied", "data": {},
    })
    bad.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: bad)
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 49
    assert env is not None
    assert env["error"]["failure_class"] == "pre_check_stat_failed"


def test_stat_non_json_stdout_exits_49(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    junk = MagicMock()
    junk.returncode = 0
    junk.stdout = "not json at all"
    junk.stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: junk)
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 49
    assert env is not None
    assert env["error"]["failure_class"] == "pre_check_stat_failed"


def test_stat_none_path_writes_with_warning(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote path does not exist → drafter still writes artifact + warns."""
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("none")))
    rc, env, err = _capture(
        ["pai", "--remote", "~/workspace/test-proj/ghost",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    assert "does not exist" in err
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())
    assert body["payload"]["is_directory_pre_check"] is False


def test_envelope_shape(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("file")))
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/stale",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    assert set(env.keys()) == {"success", "data", "error"}
    assert {"artifact_path", "request_id", "risk_summary"}.issubset(env["data"])


def test_suppress_pattern_no_redact(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(subprocess, "run", _make_stat_run(_env_ok("file")))
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--no-redact", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
