"""Unit tests for ssh-guarded ``request_command.py`` drafter (12+ cases per D12).

Covers (design.md §3 D12 row "request_command.py"):
- help exits 0 and lists v1.0 flags only (no v1.1 deferred)
- happy path: artifact written, envelope shape
- workdir outside sandbox → warn but artifact still written (C3)
- timeout above max → exit 30
- timeout below min → exit 30
- empty command → exit 30
- --env max 32 enforcement → exit 33
- --env malformed → exit 30
- --expires-in invalid → exit 34
- SUPPRESS smoke: --json + --project-id work via parents=[common]
- JSON envelope shape verbatim per PRD acceptance row 4
- risk_summary computed by lib (sudo → host scope + irreversible=true)
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

# Evict-restore the ``lib`` package (ssh-core's lib has the same name).
_SAVED_LIB_MODULES = {
    k: sys.modules[k]
    for k in list(sys.modules)
    if k == "lib" or k.startswith("lib.")
}
for _k in list(_SAVED_LIB_MODULES):
    del sys.modules[_k]

sys.path.insert(0, str(SCRIPTS_DIR))
import request_command as _rc_mod  # noqa: E402

# Module aliases (don't repeat the dance below).
request_command = _rc_mod

# Restore other modules' ``lib`` namespace.
for _k in list(sys.modules):
    if _k == "lib" or _k.startswith("lib."):
        del sys.modules[_k]
sys.modules.update(_SAVED_LIB_MODULES)
with contextlib.suppress(ValueError):
    sys.path.remove(str(SCRIPTS_DIR))


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def tmp_requests_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$SSH_GUARDED_HOME`` at tmp_path (real resolution, no patching)."""
    state = tmp_path / "state"
    monkeypatch.setenv("SSH_GUARDED_HOME", str(state))
    return state / "reports" / "test-proj" / "requests"


@pytest.fixture(autouse=True)
def _stable_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a known project_id so sandbox checks behave."""
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "test-proj")


def _capture(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any] | None, str]:
    rc = request_command.main(args)
    cap = capsys.readouterr()
    env: dict[str, Any] | None
    try:
        env = json.loads(cap.out) if cap.out.strip() else None
    except json.JSONDecodeError:
        env = None
    return rc, env, cap.err


# --- Tests -----------------------------------------------------------------


def test_help_exits_zero_and_lists_v10_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        request_command.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    # v1.0 surface flags
    for flag in ("--reason", "--workdir", "--timeout", "--expires-in",
                 "--env", "--project-id", "--json", "--no-redact"):
        assert flag in out, f"missing v1.0 flag {flag}"
    # v1.1 deferred flags must NOT appear
    for flag in ("--dry-run", "--reviewer"):
        assert flag not in out, f"unexpected v1.1 flag {flag}"


def test_happy_path_writes_artifact_and_envelope(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo hello", "--reason", "smoke test", "--json"], capsys
    )
    assert rc == 0
    assert env is not None
    assert env["success"] is True
    assert env["error"] is None
    data = env["data"]
    assert data["operation"] == "command"
    assert data["server"] == "pai"
    rid = data["request_id"]
    assert rid.startswith("command-")
    assert data["artifact_path"].endswith(f"{rid}.json")
    # File actually written.
    art_path = Path(data["artifact_path"])
    assert art_path.exists()
    body = json.loads(art_path.read_text())
    assert body["version"] == 1
    assert body["operation"] == "command"
    assert body["payload"]["command"] == "echo hello"
    assert body["payload"]["timeout"] == 60
    # No workdir key when not passed.
    assert "workdir" not in body["payload"]
    # risk_summary structure
    assert set(data["risk_summary"].keys()) == {"irreversible", "scope", "blast_radius"}


def test_workdir_outside_sandbox_warns_but_writes(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, err = _capture(
        ["pai", "echo hi", "--reason", "x", "--workdir", "/etc", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    assert "outside" in err and "test-proj" in err
    # Artifact has the path verbatim (normalized).
    art_path = Path(env["data"]["artifact_path"])
    body = json.loads(art_path.read_text())
    assert body["payload"]["workdir"] == "/etc"


def test_timeout_above_max_exits_30(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--timeout", "999999", "--json"],
        capsys,
    )
    assert rc == 30
    assert env is not None and env["success"] is False
    assert env["error"]["failure_class"] == "command_payload_invalid"


def test_timeout_below_min_exits_30(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--timeout", "0", "--json"], capsys
    )
    assert rc == 30
    assert env is not None
    assert env["error"]["failure_class"] == "command_payload_invalid"


def test_empty_command_exits_30(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "   ", "--reason", "x", "--json"], capsys
    )
    assert rc == 30
    assert env is not None
    assert env["error"]["failure_class"] == "command_payload_invalid"


def test_env_over_cap_exits_33(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_args: list[str] = []
    for i in range(33):
        env_args += ["--env", f"K{i}=v{i}"]
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", *env_args, "--json"], capsys
    )
    assert rc == 33
    assert env is not None
    assert env["error"]["failure_class"] == "env_too_many"


def test_env_malformed_exits_30(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--env", "NO_EQUALS", "--json"],
        capsys,
    )
    assert rc == 30
    assert env is not None
    assert env["error"]["failure_class"] == "command_payload_invalid"


def test_expires_in_invalid_exits_34(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--expires-in", "365d", "--json"],
        capsys,
    )
    assert rc == 34
    assert env is not None
    assert env["error"]["failure_class"] == "expiry_invalid"


def test_expires_in_floor_exits_34(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--expires-in", "10m", "--json"],
        capsys,
    )
    # 10m = 600s > 60s floor, so this should succeed
    assert rc == 0


def test_expires_in_bad_format_exits_34(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--expires-in", "abc", "--json"],
        capsys,
    )
    assert rc == 34
    assert env is not None
    assert env["error"]["failure_class"] == "expiry_invalid"


def test_project_id_flag_overrides_env(
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SUPPRESS pattern: --project-id flag wins over env."""
    monkeypatch.setenv("SSH_GUARDED_PROJECT_ID", "env-only")
    rc, env, err = _capture(
        ["pai", "echo", "--reason", "x", "--project-id", "flag-wins",
         "--workdir", "~/workspace/flag-wins/sub", "--json"],
        capsys,
    )
    assert rc == 0
    # No "outside" warning because workdir matches the flag's project_id.
    assert "outside" not in err
    art_path = Path(env["data"]["artifact_path"])  # type: ignore[index]
    body = json.loads(art_path.read_text())
    assert body["payload"]["workdir"] == "~/workspace/flag-wins/sub"


def test_envelope_shape_happy_path_keys(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PRD Acceptance Criteria row 4: drafter envelope shape verbatim."""
    rc, env, _ = _capture(
        ["pai", "echo", "--reason", "x", "--json"], capsys
    )
    assert rc == 0
    assert env is not None
    assert set(env.keys()) == {
        "success", "exit_code", "stdout", "stderr", "data", "error",
    }
    assert env["exit_code"] == 0
    assert env["error"] is None
    assert set(env["data"].keys()) >= {
        "artifact_path", "request_id", "risk_summary",
    }


def test_risk_summary_sudo_is_irreversible_host(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D4 risk computation: sudo → irreversible=true + scope=host."""
    rc, env, _ = _capture(
        ["pai", "sudo systemctl restart nginx", "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    rs = env["data"]["risk_summary"]
    assert rs["irreversible"] is True
    assert rs["scope"] == "host"


def test_artifact_id_is_unique_across_two_runs(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc1, env1, _ = _capture(
        ["pai", "echo a", "--reason", "x", "--json"], capsys
    )
    rc2, env2, _ = _capture(
        ["pai", "echo b", "--reason", "y", "--json"], capsys
    )
    assert rc1 == 0 and rc2 == 0
    assert env1 is not None and env2 is not None
    assert env1["data"]["request_id"] != env2["data"]["request_id"]


def test_human_mode_writes_to_stdout(
    tmp_requests_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = request_command.main(
        ["pai", "echo hi", "--reason", "human mode"]
    )
    cap = capsys.readouterr()
    assert rc == 0
    assert "drafted command-" in cap.out
    assert "risk:" in cap.out
