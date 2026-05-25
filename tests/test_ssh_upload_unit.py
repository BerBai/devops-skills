"""Unit tests for ssh-core/scripts/ssh_upload.py + lib/sftp_transport.py v1.0.

Paramiko is **never** invoked in this module. We mock at two layers:

1. ``SftpSession`` itself — tests build a ``_FakeSession`` subclass that
   skips ``__init__`` (no real ssh handshake) and exposes a
   ``unittest.mock.MagicMock`` as ``self.sftp``. The tests then drive
   ``put_chunked`` / ``stat`` / ``chmod`` / ``mkdir_p`` via direct
   monkeypatching of ``SftpSession`` instance methods. This avoids
   re-implementing paramiko's API shape.

2. For ``upload_tree`` tests, the underlying ``upload_file`` is patched
   to a fake that records its arguments. This isolates the recursive
   walking + posixpath joining + failed_files aggregation logic from
   the per-file transfer state machine (which has its own dedicated
   ``upload_file`` tests above).

The integration counterpart in ``test_ssh_upload_integration.py``
exercises the same paths against a real host via the ``live_ssh``
marker.

Substitution-for-manual notes: Phase R's implement.md requested manual
smoke tests against pai (RD3, RD4). Because pai is offline, the
``upload_tree`` unit tests below are explicitly labelled
"SUBSTITUTE: ..." in their docstrings to mark which behaviour they
cover in lieu of that manual gate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_upload  # noqa: E402, I001
from lib import sftp_transport  # noqa: E402, I001
from lib.sftp_transport import (  # noqa: E402
    SftpSession,
    TransferResult,
    make_pct_callback,
    upload_file,
    upload_tree,
)

# Named exit codes used in assertions (avoids PLR2004 magic-value warnings).
EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_VERIFY_FAILED = 9

# Common byte-count and mode constants used in assertions.
SAMPLE_BLOCK = 1024
RESUME_REMOTE_SIZE = 300
RESUME_LOCAL_SIZE = 500
RESUME_DELTA = RESUME_LOCAL_SIZE - RESUME_REMOTE_SIZE  # 200
EQUAL_SIZE = 200
FLAT_FILE_COUNT = 3
FLAT_SUCCESS_BYTES = 40  # 2 files × 20 bytes (b.txt fails in continue-on-error test)
DIR_MODE = 0o755
FILE_MODE = 0o644
FILE_MODE_PRIVATE = 0o600


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class _FakeSession(SftpSession):
    """``SftpSession`` subclass that skips real paramiko connect."""

    def __init__(self) -> None:  # noqa: D401 — override
        # Deliberately NOT calling super().__init__ so no paramiko connect.
        self.alias = "fake"
        self._client = MagicMock()
        self.sftp = MagicMock()


def _make_fake_session() -> _FakeSession:
    return _FakeSession()


# -----------------------------------------------------------------------------
# CLI surface
# -----------------------------------------------------------------------------


def test_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse --help exits 0 and lists all 7 flags + 3 positionals."""
    with pytest.raises(SystemExit) as exc_info:
        ssh_upload.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    # 3 positionals
    assert "alias" in out
    assert "local_path" in out
    assert "remote_path" in out
    # 7 flags
    for flag in (
        "--resume",
        "--recursive",
        "--no-progress",
        "--chmod",
        "--bwlimit",
        "--block-size",
        "--json",
    ):
        assert flag in out, f"missing flag in --help: {flag}"


# -----------------------------------------------------------------------------
# CLI guard branches (run before SftpSession is opened)
# -----------------------------------------------------------------------------


def test_local_path_missing_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pre-flight: local path does not exist → exit 1, failure_class=remote_error."""

    # Belt-and-braces: even if main got past the guard, SftpSession would
    # fail. We don't want to actually attempt a connection.
    def _refuse_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("SftpSession must not be opened when local missing")

    monkeypatch.setattr(ssh_upload, "SftpSession", _refuse_connect)

    rc = ssh_upload.main(
        ["pai", "/no/such/path/xyz", "/tmp/x", "--json"]
    )
    assert rc == EXIT_GENERIC_FAILURE
    out = capsys.readouterr().out
    env = json.loads(out)
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert env["data"]["failure_class"] == "remote_error"
    assert "local path not found" in env["stderr"]


def test_local_dir_without_recursive_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pre-flight: passing a dir without --recursive → exit 1 + helpful stderr."""

    def _refuse_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("SftpSession must not be opened on dir guard")

    monkeypatch.setattr(ssh_upload, "SftpSession", _refuse_connect)

    rc = ssh_upload.main([
        "pai", str(tmp_path), "/tmp/x", "--json",
    ])
    assert rc == EXIT_GENERIC_FAILURE
    out = capsys.readouterr().out
    env = json.loads(out)
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert "--recursive" in env["stderr"]


def test_chmod_parse_error_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--chmod XYZ (not octal) → exit 1, stderr names the flag."""
    local = tmp_path / "f.txt"
    local.write_text("hi")

    def _refuse_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("SftpSession must not be opened when chmod invalid")

    monkeypatch.setattr(ssh_upload, "SftpSession", _refuse_connect)

    rc = ssh_upload.main([
        "pai", str(local), "/tmp/x", "--chmod", "XYZ", "--json",
    ])
    assert rc == EXIT_GENERIC_FAILURE
    out = capsys.readouterr().out
    env = json.loads(out)
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert "invalid --chmod" in env["stderr"]


# -----------------------------------------------------------------------------
# upload_file — fast path (no resume)
# -----------------------------------------------------------------------------


def test_fast_path_uploads_no_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --resume: put_chunked once at offset 0, no sha256 calls."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"x" * SAMPLE_BLOCK)

    session = _make_fake_session()
    put_calls: list[dict[str, Any]] = []

    def fake_put_chunked(local_path: str, remote_path: str, **kwargs: Any) -> int:
        put_calls.append({"local": local_path, "remote": remote_path, **kwargs})
        return SAMPLE_BLOCK

    sha_called: list[str] = []

    def fail_sha_local(_p: str) -> str:
        sha_called.append("local")
        raise AssertionError("sha256_streaming_local must not run on fast path")

    def fail_sha_remote(_s: SftpSession, _p: str) -> str:
        sha_called.append("remote")
        raise AssertionError("sha256_streaming_remote must not run on fast path")

    monkeypatch.setattr(session, "put_chunked", fake_put_chunked)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_local", fail_sha_local)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_remote", fail_sha_remote)

    tr = upload_file(session, str(local), "/tmp/dest", resume=False)
    assert tr.success is True
    assert tr.exit_code == EXIT_OK
    assert tr.sha256_verified is False
    assert tr.bytes_transferred == SAMPLE_BLOCK
    assert len(put_calls) == 1
    assert put_calls[0]["offset"] == 0
    assert sha_called == []


def test_fast_path_applies_chmod_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chmod=0o600 → session.chmod(remote, 0o600) called exactly once."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"y" * 16)

    session = _make_fake_session()
    monkeypatch.setattr(session, "put_chunked", lambda *_a, **_kw: 16)
    chmod_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        session, "chmod", lambda r, m: chmod_calls.append((r, m))
    )

    tr = upload_file(session, str(local), "/tmp/dest", chmod=0o600)
    assert tr.success is True
    assert chmod_calls == [("/tmp/dest", 0o600)]


def test_fast_path_skips_chmod_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chmod=None → session.chmod must not be called."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"z" * 8)

    session = _make_fake_session()
    monkeypatch.setattr(session, "put_chunked", lambda *_a, **_kw: 8)

    def fail_chmod(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("session.chmod must not be called when chmod=None")

    monkeypatch.setattr(session, "chmod", fail_chmod)

    tr = upload_file(session, str(local), "/tmp/dest", chmod=None)
    assert tr.success is True


# -----------------------------------------------------------------------------
# upload_file — resume path
# -----------------------------------------------------------------------------


def _mock_sha(
    monkeypatch: pytest.MonkeyPatch, local_hex: str, remote_hex: str
) -> None:
    monkeypatch.setattr(
        sftp_transport, "sha256_streaming_local", lambda _p: local_hex
    )
    monkeypatch.setattr(
        sftp_transport,
        "sha256_streaming_remote",
        lambda _s, _p: remote_hex,
    )


def test_resume_missing_remote_fast_path_then_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remote stat → None: put from offset 0, then verify."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"a" * 100)

    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: None)
    put_calls: list[int] = []
    monkeypatch.setattr(
        session,
        "put_chunked",
        lambda *_a, **kw: put_calls.append(kw.get("offset", 0)) or 100,
    )
    _mock_sha(monkeypatch, "aa" * 16, "aa" * 16)

    tr = upload_file(session, str(local), "/tmp/dest", resume=True)
    assert tr.success is True
    assert tr.sha256_verified is True
    assert tr.bytes_resumed_from == 0
    assert put_calls == [0]


def test_resume_equal_size_skips_transfer_runs_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remote_size == local_size → no put_chunked, verify only."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"b" * EQUAL_SIZE)

    session = _make_fake_session()
    monkeypatch.setattr(
        session, "stat", lambda _r: type("A", (), {"st_size": EQUAL_SIZE})()
    )

    def fail_put(*_a: Any, **_kw: Any) -> int:
        raise AssertionError("put_chunked must not run when sizes are equal")

    monkeypatch.setattr(session, "put_chunked", fail_put)
    _mock_sha(monkeypatch, "bb" * 16, "bb" * 16)

    tr = upload_file(session, str(local), "/tmp/dest", resume=True)
    assert tr.success is True
    assert tr.sha256_verified is True
    assert tr.bytes_resumed_from == EQUAL_SIZE
    assert tr.bytes_transferred == 0


def test_resume_partial_continues_from_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remote_size < local_size → put with offset=remote_size, verify."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"c" * RESUME_LOCAL_SIZE)

    session = _make_fake_session()
    monkeypatch.setattr(
        session, "stat", lambda _r: type("A", (), {"st_size": RESUME_REMOTE_SIZE})()
    )
    captured: dict[str, Any] = {}

    def fake_put(*_a: Any, **kw: Any) -> int:
        captured.update(kw)
        return RESUME_DELTA

    monkeypatch.setattr(session, "put_chunked", fake_put)
    _mock_sha(monkeypatch, "cc" * 16, "cc" * 16)

    tr = upload_file(session, str(local), "/tmp/dest", resume=True)
    assert tr.success is True
    assert tr.sha256_verified is True
    assert tr.bytes_resumed_from == RESUME_REMOTE_SIZE
    assert tr.bytes_transferred == RESUME_DELTA
    assert captured["offset"] == RESUME_REMOTE_SIZE


def test_resume_remote_larger_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """remote_size > local_size → exit 1 'refuse to truncate'."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"d" * 100)

    session = _make_fake_session()
    monkeypatch.setattr(
        session, "stat", lambda _r: type("A", (), {"st_size": 500})()
    )

    def fail_put(*_a: Any, **_kw: Any) -> int:
        raise AssertionError("put_chunked must not run when remote is larger")

    def fail_sha(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("sha256 must not run when refusing truncate")

    monkeypatch.setattr(session, "put_chunked", fail_put)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_local", fail_sha)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_remote", fail_sha)

    tr = upload_file(session, str(local), "/tmp/dest", resume=True)
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert "refuse to truncate" in tr.stderr


def test_resume_sha256_mismatch_returns_9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sha256 mismatch → exit 9, failure_class=transfer_verify_failed."""
    local = tmp_path / "src.bin"
    local.write_bytes(b"e" * 50)

    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: None)
    monkeypatch.setattr(session, "put_chunked", lambda *_a, **_kw: 50)
    _mock_sha(monkeypatch, "aaaa", "bbbb")

    tr = upload_file(session, str(local), "/tmp/dest", resume=True)
    assert tr.success is False
    assert tr.exit_code == EXIT_VERIFY_FAILED
    assert tr.failure_class == "transfer_verify_failed"
    assert tr.sha256_local == "aaaa"
    assert tr.sha256_remote == "bbbb"


# -----------------------------------------------------------------------------
# upload_tree — recursive logic (SUBSTITUTE for implement.md RD3/RD4 manual smoke)
# -----------------------------------------------------------------------------


def test_recursive_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: empty dir → success, 0 file calls."""
    session = _make_fake_session()
    mkdir_calls: list[str] = []
    monkeypatch.setattr(
        session, "mkdir_p", lambda d, mode=0o755: mkdir_calls.append(d)
    )

    upload_calls: list[Any] = []

    def fake_upload_file(*args: Any, **kwargs: Any) -> TransferResult:
        upload_calls.append((args, kwargs))
        return TransferResult(success=True, exit_code=0, bytes_transferred=0)

    monkeypatch.setattr(sftp_transport, "upload_file", fake_upload_file)

    tr = upload_tree(session, str(tmp_path), "/srv/dest")
    assert tr.success is True
    assert tr.exit_code == EXIT_OK
    assert tr.bytes_transferred == 0
    assert upload_calls == []
    # root dir is still ensured
    assert "/srv/dest" in mkdir_calls


def test_recursive_flat_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: 3 files in root → 3 upload_file calls, all success."""
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name)

    session = _make_fake_session()
    monkeypatch.setattr(session, "mkdir_p", lambda *_a, **_kw: None)

    captured: list[tuple[str, str]] = []

    def fake_upload_file(
        _sess: SftpSession, local: str, remote: str, **_kw: Any
    ) -> TransferResult:
        captured.append((local, remote))
        return TransferResult(
            success=True, exit_code=0, bytes_transferred=len(remote)
        )

    monkeypatch.setattr(sftp_transport, "upload_file", fake_upload_file)

    tr = upload_tree(session, str(tmp_path), "/srv/dest")
    assert tr.success is True
    assert len(captured) == FLAT_FILE_COUNT
    remotes = sorted(r for _, r in captured)
    assert remotes == ["/srv/dest/a.txt", "/srv/dest/b.txt", "/srv/dest/c.txt"]


def test_recursive_nested_tree_uses_posixpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: nested tree → POSIX separators on remote paths only."""
    (tmp_path / "a.txt").write_text("A")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("B")
    deep = sub / "deep"
    deep.mkdir()
    (deep / "c.txt").write_text("C")

    session = _make_fake_session()
    mkdir_calls: list[str] = []
    monkeypatch.setattr(
        session, "mkdir_p", lambda d, mode=0o755: mkdir_calls.append(d)
    )

    captured_remotes: list[str] = []

    def fake_upload_file(
        _sess: SftpSession, _local: str, remote: str, **_kw: Any
    ) -> TransferResult:
        captured_remotes.append(remote)
        return TransferResult(success=True, exit_code=0, bytes_transferred=1)

    monkeypatch.setattr(sftp_transport, "upload_file", fake_upload_file)

    tr = upload_tree(session, str(tmp_path), "/srv/x")
    assert tr.success is True
    assert set(captured_remotes) == {
        "/srv/x/a.txt",
        "/srv/x/sub/b.txt",
        "/srv/x/sub/deep/c.txt",
    }
    # No backslashes leaked into remote paths.
    for r in captured_remotes:
        assert "\\" not in r
    # mkdir_p was called for every unique remote dir.
    for d in ("/srv/x", "/srv/x/sub", "/srv/x/sub/deep"):
        assert d in mkdir_calls, f"missing mkdir_p call for {d}"


def test_recursive_continues_on_error_aggregates_failed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: one file fails, two succeed → exit 1, failed_files has 1 entry."""
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name * 10)

    session = _make_fake_session()
    monkeypatch.setattr(session, "mkdir_p", lambda *_a, **_kw: None)

    def fake_upload_file(
        _sess: SftpSession, local: str, _remote: str, **_kw: Any
    ) -> TransferResult:
        if local.endswith("b.txt"):
            return TransferResult(
                success=False,
                exit_code=1,
                bytes_transferred=0,
                failure_class="remote_error",
                stderr="boom",
            )
        return TransferResult(success=True, exit_code=0, bytes_transferred=20)

    monkeypatch.setattr(sftp_transport, "upload_file", fake_upload_file)

    tr = upload_tree(session, str(tmp_path), "/srv/dest")
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert tr.failure_class == "remote_error"
    assert tr.failed_files is not None
    assert len(tr.failed_files) == 1
    assert tr.failed_files[0]["path"].endswith("b.txt")
    assert "boom" in tr.failed_files[0]["error"]
    # Successful files' bytes summed
    assert tr.bytes_transferred == FLAT_SUCCESS_BYTES


def test_recursive_separator_line_emitted_when_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SUBSTITUTE: progress=True prints `--- file i/N: ... ---` for each file."""
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")

    session = _make_fake_session()
    monkeypatch.setattr(session, "mkdir_p", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        sftp_transport,
        "upload_file",
        lambda *_a, **_kw: TransferResult(
            success=True, exit_code=0, bytes_transferred=1
        ),
    )

    upload_tree(session, str(tmp_path), "/srv/dest", progress=True)
    err = capsys.readouterr().err
    assert "--- file 1/2:" in err
    assert "--- file 2/2:" in err

    # Now repeat with progress=False — no separator lines.
    upload_tree(session, str(tmp_path), "/srv/dest", progress=False)
    err2 = capsys.readouterr().err
    assert "--- file" not in err2


def test_recursive_chmod_propagated_to_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: chmod is passed to every upload_file call; mkdir_p uses 0o755."""
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")

    session = _make_fake_session()
    mkdir_modes: list[int] = []

    def fake_mkdir(d: str, mode: int = 0o755) -> None:
        mkdir_modes.append(mode)

    monkeypatch.setattr(session, "mkdir_p", fake_mkdir)

    chmod_args: list[int | None] = []

    def fake_upload_file(*_a: Any, chmod: int | None = None, **_kw: Any) -> TransferResult:
        chmod_args.append(chmod)
        return TransferResult(success=True, exit_code=0, bytes_transferred=1)

    monkeypatch.setattr(sftp_transport, "upload_file", fake_upload_file)

    upload_tree(session, str(tmp_path), "/srv/dest", chmod=FILE_MODE)
    assert chmod_args == [FILE_MODE, FILE_MODE]
    # mkdir_p was called with mode=0o755 (dirs ignore --chmod)
    assert all(m == DIR_MODE for m in mkdir_modes)


# -----------------------------------------------------------------------------
# Progress callback helper
# -----------------------------------------------------------------------------


def test_make_pct_callback_emits_at_5pct_buckets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """callback fires at 5%-bucket boundaries only."""
    cb = make_pct_callback("foo.bin", total=1000, no_progress=False)
    assert cb is not None
    # Drive from 0 → 1000 in steps of 10 (every 1%); we expect ~21 lines
    # (one at each 5% bucket from 0 → 100 inclusive).
    for done in range(0, 1001, 10):
        cb(done, 1000)
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "foo.bin" in line]
    # 0, 5, 10, ..., 100 → 21 distinct buckets
    expected_min = 20
    assert len(lines) >= expected_min, f"expected ~21 buckets, got {len(lines)}: {lines}"
    # First line is the 0% (start) bucket; final line should be 100%.
    assert "0%" in lines[0]
    assert "100%" in lines[-1]


def test_make_pct_callback_respects_no_progress(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """no_progress=True returns None (caller skips invocation)."""
    cb = make_pct_callback("foo.bin", total=1000, no_progress=True)
    assert cb is None
    err = capsys.readouterr().err
    assert err == ""


def test_make_pct_callback_verb_parametrization(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """verb='Downloading' shows up in the stderr line — used by ssh_download."""
    cb = make_pct_callback("foo.bin", total=100, no_progress=False, verb="Downloading")
    assert cb is not None
    cb(50, 100)
    err = capsys.readouterr().err
    assert "Downloading foo.bin" in err


def test_make_pct_callback_zero_total_returns_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Edge case: 0-byte file → callback is None (no division by zero)."""
    cb = make_pct_callback("empty.bin", total=0, no_progress=False)
    assert cb is None
    assert capsys.readouterr().err == ""
