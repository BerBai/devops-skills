"""Unit tests for ssh-core/scripts/ssh_download.py + lib/sftp_transport.py v1.0.

Mirror of ``test_ssh_upload_unit.py``: paramiko is never invoked here.
We mock at the same two layers (``SftpSession`` subclass that skips
``__init__`` and patches per-instance methods + ``sftp_transport``
module-level patches for ``download_file`` when exercising the
recursive walking logic).

The integration counterpart in ``test_ssh_download_integration.py``
exercises the same paths against a real host via the ``live_ssh``
marker.

Differences from the upload twin:
- 3 download-only guard tests (remote not found / remote is dir without
  --recursive / local is dir without --recursive). Mirrors PRD R11.
- No ``--chmod`` cases (download has no chmod surface).
- Resume direction is reversed: we stat **local** to choose offset, and
  the "refuse to truncate" branch fires when ``local_size > remote_size``.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_download  # noqa: E402, I001
from lib import sftp_transport  # noqa: E402, I001
from lib.sftp_transport import (  # noqa: E402
    SftpSession,
    TransferResult,
    download_file,
    download_tree,
)

# Named exit codes used in assertions.
EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_VERIFY_FAILED = 9

# Common byte-count constants.
SAMPLE_BLOCK = 1024
RESUME_REMOTE_SIZE = 500
RESUME_LOCAL_SIZE = 300
RESUME_DELTA = RESUME_REMOTE_SIZE - RESUME_LOCAL_SIZE  # 200
EQUAL_SIZE = 200
FLAT_FILE_COUNT = 3
FLAT_SUCCESS_BYTES = 40  # 2 files × 20 bytes


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


def _stat_attr(size: int, is_dir: bool = False) -> Any:
    """Build a fake SFTPAttributes-like with ``st_size`` and ``st_mode``."""
    mode = stat.S_IFDIR | 0o755 if is_dir else stat.S_IFREG | 0o644
    return type("A", (), {"st_size": size, "st_mode": mode})()


# -----------------------------------------------------------------------------
# CLI surface
# -----------------------------------------------------------------------------


def test_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """argparse --help exits 0 and lists 6 flags + 3 positionals (no --chmod)."""
    with pytest.raises(SystemExit) as exc_info:
        ssh_download.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    # 3 positionals
    assert "alias" in out
    assert "remote_path" in out
    assert "local_path" in out
    # 6 flags — no --chmod
    for flag in (
        "--resume",
        "--recursive",
        "--no-progress",
        "--bwlimit",
        "--block-size",
        "--json",
    ):
        assert flag in out, f"missing flag in --help: {flag}"
    assert "--chmod" not in out, "ssh_download must not advertise --chmod"


# -----------------------------------------------------------------------------
# Guard branches
# -----------------------------------------------------------------------------


def test_local_is_dir_without_recursive_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI pre-flight: local path is an existing dir w/o --recursive → exit 1."""

    def _refuse_connect(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("SftpSession must not be opened on local-dir guard")

    monkeypatch.setattr(ssh_download, "SftpSession", _refuse_connect)

    rc = ssh_download.main(
        ["pai", "/etc/hostname", str(tmp_path), "--json"]
    )
    assert rc == EXIT_GENERIC_FAILURE
    out = capsys.readouterr().out
    env = json.loads(out)
    assert env["success"] is False
    assert env["exit_code"] == EXIT_GENERIC_FAILURE
    assert env["data"]["failure_class"] == "remote_error"
    assert env["data"]["direction"] == "download"
    assert "directory" in env["stderr"]


def test_remote_not_found_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport-level guard: remote stat returns None → exit 1 remote_error."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: None)

    def fail_get(*_a: Any, **_kw: Any) -> int:
        raise AssertionError("get_chunked must not run when remote is missing")

    monkeypatch.setattr(session, "get_chunked", fail_get)

    tr = download_file(session, "/no/such/remote", str(tmp_path / "dest"))
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert tr.failure_class == "remote_error"
    assert "remote path not found" in tr.stderr


def test_remote_is_dir_without_recursive_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """download_file refuses a remote directory → exit 1 + 'pass --recursive'."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(0, is_dir=True))

    def fail_get(*_a: Any, **_kw: Any) -> int:
        raise AssertionError("get_chunked must not run when remote is a dir")

    monkeypatch.setattr(session, "get_chunked", fail_get)

    tr = download_file(session, "/etc", str(tmp_path / "out"))
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert tr.failure_class == "remote_error"
    assert "--recursive" in tr.stderr


# -----------------------------------------------------------------------------
# download_file — fast path (no resume)
# -----------------------------------------------------------------------------


def test_fast_path_downloads_no_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --resume: get_chunked once at offset 0, no sha256 calls."""
    session = _make_fake_session()
    monkeypatch.setattr(
        session, "stat", lambda _r: _stat_attr(SAMPLE_BLOCK)
    )

    get_calls: list[dict[str, Any]] = []

    def fake_get_chunked(remote_path: str, local_path: str, **kwargs: Any) -> int:
        get_calls.append({"remote": remote_path, "local": local_path, **kwargs})
        # Simulate writing the file (so subsequent local stats would succeed).
        Path(local_path).write_bytes(b"x" * SAMPLE_BLOCK)
        return SAMPLE_BLOCK

    def fail_sha_local(_p: str) -> str:
        raise AssertionError("sha256_streaming_local must not run on fast path")

    def fail_sha_remote(_s: SftpSession, _p: str) -> str:
        raise AssertionError("sha256_streaming_remote must not run on fast path")

    monkeypatch.setattr(session, "get_chunked", fake_get_chunked)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_local", fail_sha_local)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_remote", fail_sha_remote)

    local = tmp_path / "dst.bin"
    tr = download_file(session, "/remote/src", str(local), resume=False)
    assert tr.success is True
    assert tr.exit_code == EXIT_OK
    assert tr.sha256_verified is False
    assert tr.bytes_transferred == SAMPLE_BLOCK
    assert len(get_calls) == 1
    assert get_calls[0]["offset"] == 0


# -----------------------------------------------------------------------------
# download_file — resume path
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


def test_resume_missing_local_fast_path_then_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local target missing on --resume: get from offset 0, then verify."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(100))

    get_calls: list[int] = []

    def fake_get(*_a: Any, **kw: Any) -> int:
        get_calls.append(kw.get("offset", 0))
        # Create the local file so subsequent operations would work.
        Path(_a[1] if len(_a) > 1 else kw["local_path"]).write_bytes(b"a" * 100)
        return 100

    monkeypatch.setattr(session, "get_chunked", fake_get)
    _mock_sha(monkeypatch, "aa" * 16, "aa" * 16)

    local = tmp_path / "dst.bin"
    tr = download_file(session, "/remote/src", str(local), resume=True)
    assert tr.success is True
    assert tr.sha256_verified is True
    assert tr.bytes_resumed_from == 0
    assert get_calls == [0]


def test_resume_equal_size_skips_transfer_runs_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_size == remote_size → no get_chunked, verify only."""
    local = tmp_path / "dst.bin"
    local.write_bytes(b"b" * EQUAL_SIZE)

    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(EQUAL_SIZE))

    def fail_get(*_a: Any, **_kw: Any) -> int:
        raise AssertionError("get_chunked must not run when sizes are equal")

    monkeypatch.setattr(session, "get_chunked", fail_get)
    _mock_sha(monkeypatch, "bb" * 16, "bb" * 16)

    tr = download_file(session, "/remote/src", str(local), resume=True)
    assert tr.success is True
    assert tr.sha256_verified is True
    assert tr.bytes_resumed_from == EQUAL_SIZE
    assert tr.bytes_transferred == 0


def test_resume_partial_continues_from_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_size < remote_size → get with offset=local_size, verify."""
    local = tmp_path / "dst.bin"
    local.write_bytes(b"c" * RESUME_LOCAL_SIZE)

    session = _make_fake_session()
    monkeypatch.setattr(
        session, "stat", lambda _r: _stat_attr(RESUME_REMOTE_SIZE)
    )
    captured: dict[str, Any] = {}

    def fake_get(*_a: Any, **kw: Any) -> int:
        captured.update(kw)
        return RESUME_DELTA

    monkeypatch.setattr(session, "get_chunked", fake_get)
    _mock_sha(monkeypatch, "cc" * 16, "cc" * 16)

    tr = download_file(session, "/remote/src", str(local), resume=True)
    assert tr.success is True
    assert tr.sha256_verified is True
    assert tr.bytes_resumed_from == RESUME_LOCAL_SIZE
    assert tr.bytes_transferred == RESUME_DELTA
    assert captured["offset"] == RESUME_LOCAL_SIZE


def test_resume_local_larger_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_size > remote_size → exit 1 'refuse to truncate'."""
    local = tmp_path / "dst.bin"
    local.write_bytes(b"d" * 500)

    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(100))

    def fail_get(*_a: Any, **_kw: Any) -> int:
        raise AssertionError("get_chunked must not run when local is larger")

    def fail_sha(*_a: Any, **_kw: Any) -> str:
        raise AssertionError("sha256 must not run when refusing truncate")

    monkeypatch.setattr(session, "get_chunked", fail_get)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_local", fail_sha)
    monkeypatch.setattr(sftp_transport, "sha256_streaming_remote", fail_sha)

    tr = download_file(session, "/remote/src", str(local), resume=True)
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert "refuse to truncate" in tr.stderr


def test_resume_sha256_mismatch_returns_9(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sha256 mismatch → exit 9, failure_class=transfer_verify_failed."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(50))

    def fake_get(*_a: Any, **_kw: Any) -> int:
        # Simulate creating the local file mid-test.
        Path(_a[1] if len(_a) > 1 else _kw["local_path"]).write_bytes(b"e" * 50)
        return 50

    monkeypatch.setattr(session, "get_chunked", fake_get)
    _mock_sha(monkeypatch, "aaaa", "bbbb")

    local = tmp_path / "dst.bin"
    tr = download_file(session, "/remote/src", str(local), resume=True)
    assert tr.success is False
    assert tr.exit_code == EXIT_VERIFY_FAILED
    assert tr.failure_class == "transfer_verify_failed"
    assert tr.sha256_local == "aaaa"
    assert tr.sha256_remote == "bbbb"


# -----------------------------------------------------------------------------
# download_tree — recursive logic (SUBSTITUTE for RD3/RD4 manual smoke)
# -----------------------------------------------------------------------------


def _make_listdir_attr(
    entries: list[tuple[str, bool, int]]
) -> list[Any]:
    """Build a list of fake SFTPAttributes for listdir_attr.

    Each tuple is (name, is_dir, size).
    """
    out = []
    for name, is_dir, size in entries:
        mode = stat.S_IFDIR | 0o755 if is_dir else stat.S_IFREG | 0o644
        attr = type(
            "A",
            (),
            {"filename": name, "st_mode": mode, "st_size": size},
        )()
        out.append(attr)
    return out


def test_recursive_empty_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: empty remote dir → success, 0 file calls."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(0, is_dir=True))
    session.sftp.listdir_attr = MagicMock(return_value=[])

    upload_calls: list[Any] = []

    def fake_download_file(*args: Any, **kwargs: Any) -> TransferResult:
        upload_calls.append((args, kwargs))
        return TransferResult(success=True, exit_code=0, bytes_transferred=0)

    monkeypatch.setattr(sftp_transport, "download_file", fake_download_file)

    tr = download_tree(session, "/srv/src", str(tmp_path / "out"))
    assert tr.success is True
    assert tr.exit_code == EXIT_OK
    assert tr.bytes_transferred == 0
    assert upload_calls == []


def test_recursive_flat_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: 3 files in remote root → 3 download_file calls, all success."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(0, is_dir=True))

    def listdir_attr(p: str) -> list[Any]:
        if p == "/srv/src":
            return _make_listdir_attr([
                ("a.txt", False, 1),
                ("b.txt", False, 1),
                ("c.txt", False, 1),
            ])
        return []

    session.sftp.listdir_attr = MagicMock(side_effect=listdir_attr)

    captured: list[tuple[str, str]] = []

    def fake_download_file(
        _sess: SftpSession, remote: str, local: str, **_kw: Any
    ) -> TransferResult:
        captured.append((remote, local))
        return TransferResult(
            success=True, exit_code=0, bytes_transferred=len(remote)
        )

    monkeypatch.setattr(sftp_transport, "download_file", fake_download_file)

    tr = download_tree(session, "/srv/src", str(tmp_path / "out"))
    assert tr.success is True
    assert len(captured) == FLAT_FILE_COUNT
    remotes = sorted(r for r, _ in captured)
    assert remotes == ["/srv/src/a.txt", "/srv/src/b.txt", "/srv/src/c.txt"]


def test_recursive_nested_tree_uses_posixpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: nested tree → POSIX separators on remote paths only."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(0, is_dir=True))

    def listdir_attr(p: str) -> list[Any]:
        if p == "/srv/x":
            return _make_listdir_attr([
                ("a.txt", False, 1),
                ("sub", True, 0),
            ])
        if p == "/srv/x/sub":
            return _make_listdir_attr([
                ("b.txt", False, 1),
                ("deep", True, 0),
            ])
        if p == "/srv/x/sub/deep":
            return _make_listdir_attr([("c.txt", False, 1)])
        return []

    session.sftp.listdir_attr = MagicMock(side_effect=listdir_attr)

    captured_remotes: list[str] = []

    def fake_download_file(
        _sess: SftpSession, remote: str, _local: str, **_kw: Any
    ) -> TransferResult:
        captured_remotes.append(remote)
        return TransferResult(success=True, exit_code=0, bytes_transferred=1)

    monkeypatch.setattr(sftp_transport, "download_file", fake_download_file)

    out_dir = tmp_path / "out"
    tr = download_tree(session, "/srv/x", str(out_dir))
    assert tr.success is True
    assert set(captured_remotes) == {
        "/srv/x/a.txt",
        "/srv/x/sub/b.txt",
        "/srv/x/sub/deep/c.txt",
    }
    # No backslashes leaked into remote paths.
    for r in captured_remotes:
        assert "\\" not in r
    # Local directories were created.
    assert (out_dir / "sub").is_dir()
    assert (out_dir / "sub" / "deep").is_dir()


def test_recursive_continues_on_error_aggregates_failed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: one file fails, two succeed → exit 1, failed_files has 1 entry."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(0, is_dir=True))
    session.sftp.listdir_attr = MagicMock(
        return_value=_make_listdir_attr([
            ("a.txt", False, 20),
            ("b.txt", False, 20),
            ("c.txt", False, 20),
        ])
    )

    def fake_download_file(
        _sess: SftpSession, remote: str, _local: str, **_kw: Any
    ) -> TransferResult:
        if remote.endswith("b.txt"):
            return TransferResult(
                success=False,
                exit_code=1,
                bytes_transferred=0,
                failure_class="remote_error",
                stderr="boom",
            )
        return TransferResult(success=True, exit_code=0, bytes_transferred=20)

    monkeypatch.setattr(sftp_transport, "download_file", fake_download_file)

    tr = download_tree(session, "/srv/src", str(tmp_path / "out"))
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert tr.failure_class == "remote_error"
    assert tr.failed_files is not None
    assert len(tr.failed_files) == 1
    assert tr.failed_files[0]["path"].endswith("b.txt")
    assert "boom" in tr.failed_files[0]["error"]
    assert tr.bytes_transferred == FLAT_SUCCESS_BYTES


def test_recursive_separator_line_emitted_when_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SUBSTITUTE: progress=True prints `--- file i/N: ... ---` for each file."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(0, is_dir=True))
    session.sftp.listdir_attr = MagicMock(
        return_value=_make_listdir_attr([
            ("a.txt", False, 1),
            ("b.txt", False, 1),
        ])
    )

    monkeypatch.setattr(
        sftp_transport,
        "download_file",
        lambda *_a, **_kw: TransferResult(
            success=True, exit_code=0, bytes_transferred=1
        ),
    )

    download_tree(session, "/srv/src", str(tmp_path / "out"), progress=True)
    err = capsys.readouterr().err
    assert "--- file 1/2:" in err
    assert "--- file 2/2:" in err

    # Now repeat with progress=False — no separator lines.
    download_tree(session, "/srv/src", str(tmp_path / "out2"), progress=False)
    err2 = capsys.readouterr().err
    assert "--- file" not in err2


def test_recursive_remote_not_found_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: download_tree of a missing remote dir → exit 1 remote_error."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: None)

    tr = download_tree(session, "/no/such/dir", str(tmp_path / "out"))
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert tr.failure_class == "remote_error"
    assert "remote path not found" in tr.stderr


def test_recursive_remote_is_file_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUBSTITUTE: download_tree on a regular file → exit 1 remote_error."""
    session = _make_fake_session()
    monkeypatch.setattr(session, "stat", lambda _r: _stat_attr(10, is_dir=False))

    tr = download_tree(session, "/etc/hostname", str(tmp_path / "out"))
    assert tr.success is False
    assert tr.exit_code == EXIT_GENERIC_FAILURE
    assert tr.failure_class == "remote_error"
    assert "omit --recursive" in tr.stderr
