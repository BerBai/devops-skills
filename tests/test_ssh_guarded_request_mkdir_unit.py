"""Unit tests for ssh-guarded ``request_mkdir.py`` drafter (8+ cases per D12).

Covers (design.md §3 D12 row "request_mkdir.py"):
- help exits 0 + lists v1.0 flags
- happy path file artifact written
- --mode invalid (non-octal / out of range) → exit 40
- remote outside sandbox warn-but-write
- --parents required for nested path → exit 42
- --parents present accepts nested path
- JSON envelope shape
- SUPPRESS smoke
- mkdir scope is single-file + irreversible=false (D4)
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

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
import request_mkdir as _rm_mod  # noqa: E402

request_mkdir = _rm_mod

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


def _capture(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any] | None, str]:
    rc = request_mkdir.main(args)
    cap = capsys.readouterr()
    try:
        env = json.loads(cap.out) if cap.out.strip() else None
    except json.JSONDecodeError:
        env = None
    return rc, env, cap.err


def test_help_exits_zero_and_lists_v10_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        request_mkdir.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--remote", "--reason", "--mode", "--parents", "--expires-in",
                 "--project-id", "--json", "--no-redact"):
        assert flag in out, f"missing v1.0 flag {flag}"


def test_happy_path_artifact_written(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/staging",
         "--reason", "smoke", "--json"], capsys
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    data = env["data"]
    assert data["operation"] == "mkdir"
    art_path = Path(data["artifact_path"])
    body = json.loads(art_path.read_text())
    assert body["payload"]["remote_path"] == "~/workspace/test-proj/staging"
    assert body["payload"]["mode"] == "0755"
    assert body["payload"]["parents"] is False
    # D4: mkdir is irreversible=false, scope=single-file
    assert body["risk_summary"]["irreversible"] is False
    assert body["risk_summary"]["scope"] == "single-file"


def test_mode_invalid_non_octal_exits_40(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--mode", "9XYZ", "--json"],
        capsys,
    )
    assert rc == 40
    assert env is not None
    assert env["error"]["failure_class"] == "mode_invalid"


def test_mode_out_of_range_exits_40(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Above 0o0777 → reject."""
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--mode", "4755", "--json"],
        capsys,
    )
    assert rc == 40
    assert env is not None
    assert env["error"]["failure_class"] == "mode_invalid"


def test_remote_outside_sandbox_warns_but_writes(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, err = _capture(
        ["pai", "--remote", "/etc/oops",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    assert "outside" in err


def test_parents_required_for_nested_exits_42(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """2+ segments below sandbox root + no --parents → exit 42."""
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/a/b",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 42
    assert env is not None
    assert env["error"]["failure_class"] == "parents_required_for_nested"


def test_parents_flag_allows_nested(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/a/b/c",
         "--reason", "x", "--parents", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    art_path = Path(env["data"]["artifact_path"])
    body = json.loads(art_path.read_text())
    assert body["payload"]["parents"] is True


def test_envelope_shape(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/single",
         "--reason", "x", "--json"], capsys
    )
    assert rc == 0
    assert env is not None
    assert set(env.keys()) == {
        "success", "exit_code", "stdout", "stderr", "data", "error",
    }
    assert "artifact_path" in env["data"]
    assert "request_id" in env["data"]
    assert "risk_summary" in env["data"]


def test_suppress_no_redact_no_clobber(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-redact is a SUPPRESS flag (mkdir drafter doesn't actually use it
    but it must be accepted without breaking)."""
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/sentinel",
         "--reason", "x", "--no-redact", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True


def test_mode_normalized_to_4_digit_octal(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--mode 700 → payload.mode = '0700'."""
    rc, env, _ = _capture(
        ["pai", "--remote", "~/workspace/test-proj/private",
         "--reason", "x", "--mode", "700", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    art_path = Path(env["data"]["artifact_path"])
    body = json.loads(art_path.read_text())
    assert body["payload"]["mode"] == "0700"
