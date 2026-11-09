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
    """Point ``$SSH_GUARDED_HOME`` at tmp_path (real resolution, no patching)."""
    state = tmp_path / "state"
    monkeypatch.setenv("SSH_GUARDED_HOME", str(state))
    return state / "reports" / "test-proj" / "requests"


@pytest.fixture(autouse=True)
def _stable_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "test-proj")


@pytest.fixture(autouse=True)
def _stub_remote_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-stub only the remote ``$HOME`` lookup, not the command build.

    ``test_remote_stat_command_*`` cases restore the real implementation to
    assert the generated ``remote_cmd`` itself.
    """
    monkeypatch.setattr(
        request_delete, "_resolve_home_for_precheck", lambda _alias: "/root",
    )


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
    assert set(env.keys()) == {
        "success", "exit_code", "stdout", "stderr", "data", "error",
    }
    assert {"artifact_path", "request_id", "risk_summary"}.issubset(env["data"])


# --- BUG-001: tilde expansion in the remote pre-check -----------------------


def _capture_stat_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    """Install a fake subprocess.run, return the argv it was called with."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> Any:
        calls.append(list(argv))
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(_env_ok("dir"))
        completed.stderr = ""
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_remote_stat_command_quotes_resolved_absolute_path() -> None:
    """The generated remote_cmd must contain no ``'~`` and use the real home."""
    cmd = request_delete._build_remote_stat_command(
        "/root/workspace/test-proj/exists-probe"
    )
    assert "'~" not in cmd, cmd
    assert "/root/workspace/test-proj/exists-probe" in cmd
    assert "-d /root/workspace/test-proj/exists-probe" in cmd
    # A path that *needs* quoting still gets quoted, after expansion.
    spaced = request_delete._build_remote_stat_command(
        "/root/workspace/test-proj/my dir"
    )
    assert "'/root/workspace/test-proj/my dir'" in spaced
    assert "'~" not in spaced


def test_remote_stat_kind_expands_tilde_through_resolved_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``~/x`` and ``/root/x`` must produce the same probe (BUG-001)."""
    calls = _capture_stat_argv(monkeypatch)
    kind_tilde = request_delete._remote_stat_kind(
        "pai", "~/workspace/test-proj/exists-probe", remote_home="/root",
    )
    kind_abs = request_delete._remote_stat_kind(
        "pai", "/root/workspace/test-proj/exists-probe", remote_home="/root",
    )
    assert kind_tilde == kind_abs == "dir"
    assert len(calls) == 2
    # Identical remote command for both spellings; no quoted tilde anywhere.
    assert calls[0][3] == calls[1][3]
    assert "'~" not in calls[0][3]
    assert "'~/workspace" not in calls[0][3]


def test_remote_stat_kind_calls_home_resolver_when_tilde_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a pre-resolved home, the SSH ``$HOME`` probe runs first."""
    calls = _capture_stat_argv(monkeypatch)
    seen: list[str] = []

    def fake_resolve(alias: str) -> str:
        seen.append(alias)
        return "/home/jason"

    monkeypatch.setattr(request_delete, "_resolve_home_for_precheck", fake_resolve)
    kind = request_delete._remote_stat_kind("pai", "~/workspace/test-proj/x")
    assert kind == "dir"
    assert seen == ["pai"]
    assert "/home/jason/workspace/test-proj/x" in calls[0][3]
    assert "'~" not in calls[0][3]


def test_home_resolution_failure_is_49_not_none(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed pre-check must not masquerade as "the path does not exist"."""
    _ArtifactError = request_delete.ArtifactError

    def boom(_alias: str) -> str:
        raise _ArtifactError(
            failure_class="pre_check_stat_failed",
            exit_code=49,
            message="ssh unreachable",
        )

    monkeypatch.setattr(request_delete, "_resolve_home_for_precheck", boom)
    rc, env, err = _capture(
        ["pai", "--remote", "~/workspace/test-proj/mystery",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 49
    assert env is not None
    assert env["error"]["failure_class"] == "pre_check_stat_failed"
    assert "does not exist" not in err


def test_tilde_directory_without_recursive_exits_46(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BUG-001 acceptance: ``~/...`` dir + no --recursive → 46, not a draft."""
    calls = _capture_stat_argv(monkeypatch)
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/exists-probe",
         "--reason", "repro", "--json"],
        capsys,
    )
    assert rc == 46
    assert env is not None
    assert env["error"]["failure_class"] == "recursive_required_for_directory"
    assert "/root/workspace/test-proj/exists-probe" in calls[0][3]


def test_tilde_directory_with_recursive_reports_tree_scope(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BUG-001 acceptance: risk_summary.scope must be ``tree`` for a directory."""
    _capture_stat_argv(monkeypatch)
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/exists-probe",
         "--reason", "repro", "--recursive", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())
    assert body["payload"]["is_directory_pre_check"] is True
    assert body["risk_summary"]["scope"] == "tree"
    assert body["risk_summary"]["irreversible"] is True


def test_no_recursive_note_only_when_truly_absent(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The misleading ``does not exist`` note is gone for a real directory."""
    _capture_stat_argv(monkeypatch)
    rc, _env, err = _capture(
        ["pai", "--remote", "~/workspace/test-proj/exists-probe",
         "--reason", "x", "--recursive", "--json"],
        capsys,
    )
    assert rc == 0
    assert "does not exist" not in err


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
