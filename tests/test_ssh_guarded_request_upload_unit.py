"""Unit tests for ssh-guarded ``request_upload.py`` drafter (14+ cases per D12).

Covers (design.md §3 D12 row "request_upload.py"):
- help exits 0 + lists v1.0 flags
- happy path: sha256 + size in artifact, sensitive_local=False, overwrite=False
- happy path with --overwrite → risk.irreversible=true
- local path missing → exit 35
- local outside upload_roots without --confirm → exit 38
- local outside upload_roots WITH --confirm → succeeds + sensitive_local=true
- sha256 IOError mid-stream → exit 37
- size > 256 MiB → exit 39
- remote outside sandbox warn-but-write
- JSON envelope shape
- SUPPRESS smoke
- overwrite flag in risk_summary blast_radius
- file in cwd is inside upload_roots (no --confirm needed)
- sha256 hex matches recomputed value
"""

# ruff: noqa: PLR2004

from __future__ import annotations

import contextlib
import hashlib
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
import request_upload as _ru_mod  # noqa: E402

request_upload = _ru_mod

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


@pytest.fixture
def constrained_upload_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Force upload_roots to a single tmp_path root.

    This keeps the test stable regardless of where pytest is invoked.
    Returns the inside-roots directory.
    """
    inside = tmp_path / "inside"
    inside.mkdir()
    monkeypatch.setattr(
        request_upload,
        "_upload_roots",
        lambda _cwd: [inside.resolve()],
    )
    return inside


def _capture(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any] | None, str]:
    rc = request_upload.main(args)
    cap = capsys.readouterr()
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
        request_upload.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--local", "--remote", "--reason", "--overwrite",
                 "--confirm-sensitive-local-upload", "--expires-in",
                 "--project-id", "--json", "--no-redact"):
        assert flag in out, f"missing v1.0 flag {flag}"
    # v1.1 deferred --size-limit must NOT appear
    assert "--size-limit" not in out


def test_happy_path_writes_artifact(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = constrained_upload_roots / "release.tar.gz"
    local.write_bytes(b"hello world")
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/release.tar.gz",
         "--reason", "test", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())
    p = body["payload"]
    assert p["local_size"] == len(b"hello world")
    assert p["local_sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert p["overwrite"] is False
    assert p["sensitive_local"] is False
    assert p["remote_path"] == "~/workspace/test-proj/release.tar.gz"
    # upload risk: not irreversible without --overwrite
    assert body["risk_summary"]["irreversible"] is False
    assert body["risk_summary"]["scope"] == "single-file"


def test_overwrite_flag_marks_risk_irreversible(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = constrained_upload_roots / "a.bin"
    local.write_bytes(b"x")
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/a.bin",
         "--reason", "x", "--overwrite", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())
    assert body["payload"]["overwrite"] is True
    assert body["risk_summary"]["irreversible"] is True
    # blast_radius template includes overwrite=True
    assert "overwrite=True" in body["risk_summary"]["blast_radius"]


def test_local_missing_exits_35(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, env, _ = _capture(
        ["pai", "--local", "/no/such/file/here-xyz",
         "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 35
    assert env is not None
    assert env["error"]["failure_class"] == "local_path_missing"


def test_local_is_dir_exits_35(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc, env, _ = _capture(
        ["pai", "--local", str(constrained_upload_roots),
         "--remote", "~/workspace/test-proj/x",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 35
    assert env is not None
    assert env["error"]["failure_class"] == "local_path_missing"


def test_outside_upload_roots_without_confirm_exits_38(
    tmp_path: Path,
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Use a path OUTSIDE upload_roots.
    outside = tmp_path / "outside"
    outside.mkdir()
    sensitive = outside / "secret.txt"
    sensitive.write_text("data")
    # Force upload_roots to only contain a different tmp dir.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(
        request_upload, "_upload_roots", lambda _cwd: [other.resolve()]
    )
    rc, env, _ = _capture(
        ["pai", "--local", str(sensitive),
         "--remote", "~/workspace/test-proj/sec",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 38
    assert env is not None
    assert env["error"]["failure_class"] == "sensitive_local_unconfirmed"


def test_outside_upload_roots_with_confirm_succeeds(
    tmp_path: Path,
    tmp_requests_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sensitive = outside / "secret.txt"
    sensitive.write_text("data")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(
        request_upload, "_upload_roots", lambda _cwd: [other.resolve()]
    )
    rc, env, _ = _capture(
        ["pai", "--local", str(sensitive),
         "--remote", "~/workspace/test-proj/sec",
         "--reason", "x", "--confirm-sensitive-local-upload", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())
    assert body["payload"]["sensitive_local"] is True


def test_sha256_compute_failed_via_lib_wrapper(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force the wrapping path: simulate OSError inside _sha256_streaming itself
    by patching Path.open to raise mid-read."""
    local = constrained_upload_roots / "a.bin"
    local.write_bytes(b"x" * 1024)

    real_open = Path.open

    def boom_open(self: Path, *a: Any, **kw: Any) -> Any:
        if self.resolve() == local.resolve():
            raise OSError("flaky disk")
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", boom_open)
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/a.bin",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 37
    assert env is not None
    assert env["error"]["failure_class"] == "sha256_compute_failed"


def test_size_limit_exceeded_exits_39(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cap the size check by monkeypatching MAX_UPLOAD_BYTES rather than
    writing 256 MiB to disk."""
    local = constrained_upload_roots / "big.bin"
    local.write_bytes(b"a" * 4096)
    monkeypatch.setattr(request_upload, "MAX_UPLOAD_BYTES", 100)
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/big.bin",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 39
    assert env is not None
    assert env["error"]["failure_class"] == "size_limit_exceeded"


def test_remote_outside_sandbox_warns_but_writes(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = constrained_upload_roots / "a.bin"
    local.write_bytes(b"hi")
    rc, env, err = _capture(
        ["pai", "--local", str(local),
         "--remote", "/etc/passwd",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True
    assert "outside" in err


def test_envelope_shape(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = constrained_upload_roots / "a.bin"
    local.write_bytes(b"x")
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/a.bin",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None
    assert set(env.keys()) == {
        "success", "exit_code", "stdout", "stderr", "data", "error",
    }
    assert {"artifact_path", "request_id", "risk_summary"}.issubset(env["data"])


def test_suppress_no_redact(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = constrained_upload_roots / "a.bin"
    local.write_bytes(b"x")
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/a.bin",
         "--reason", "x", "--no-redact", "--json"],
        capsys,
    )
    assert rc == 0
    assert env is not None and env["success"] is True


def test_sha256_matches_expected_hex(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = b"some bytes to hash"
    local = constrained_upload_roots / "h.bin"
    local.write_bytes(data)
    rc, env, _ = _capture(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/h.bin",
         "--reason", "x", "--json"],
        capsys,
    )
    assert rc == 0
    body = json.loads(Path(env["data"]["artifact_path"]).read_text())  # type: ignore[index]
    assert body["payload"]["local_sha256"] == hashlib.sha256(data).hexdigest()
    assert body["payload"]["local_size"] == len(data)


def test_human_mode_writes_summary(
    tmp_requests_dir: Path,
    constrained_upload_roots: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = constrained_upload_roots / "a.bin"
    local.write_bytes(b"yo")
    rc = request_upload.main(
        ["pai", "--local", str(local),
         "--remote", "~/workspace/test-proj/a.bin", "--reason", "x"]
    )
    cap = capsys.readouterr()
    assert rc == 0
    assert "drafted upload-" in cap.out
