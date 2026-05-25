"""Unit tests for ssh_server_transfer v1.0.

Paramiko is never invoked — every test mocks ``SftpSession`` (the only
external dependency) at the module-level call site
(``ssh_server_transfer.SftpSession``). ``_pipe_file`` / ``_pipe_tree``
get their per-call SFTP behaviour from ``_FakeSession`` instances whose
``.sftp`` attribute is a ``MagicMock``.

The integration counterpart in
``tests/test_ssh_server_transfer_integration.py`` exercises the same
code paths against a real host via the ``live_ssh`` marker.

Coverage target per implement.md Phase 5.4: ≥37 cases, ≥90% statement
coverage on ``ssh_server_transfer.py``.
"""

from __future__ import annotations

import argparse
import json
import socket
import stat as _stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import paramiko  # type: ignore[import-untyped]
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "plugins" / "ssh-core" / "skills" / "ssh-core" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import ssh_server_transfer as sst  # noqa: E402
from lib.sftp_transport import SftpSession  # noqa: E402

# Named exit codes for assertions (PRD D5 / design.md failure-class table).
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_MODE_DEFERRED = 20
EXIT_FLAG_DEFERRED = 21
EXIT_NETWORK_SRC = 22
EXIT_NETWORK_DST = 23
EXIT_AUTH_SRC = 24
EXIT_AUTH_DST = 25
EXIT_SRC_PATH_MISSING = 26
EXIT_SRC_IS_DIRECTORY = 27
EXIT_DST_PATH_INVALID = 28
EXIT_TRANSFER_IO = 29

# Magic numbers used in body to dodge PLR2004.
SAMPLE_BLOCK = 256
THREE_CHUNKS = 3
TEST_TREE_FILE_COUNT = 3
BYTES_PER_FAKE_FILE = 10
DIR_MODE_BITS = _stat.S_IFDIR | 0o755
FILE_MODE_BITS = _stat.S_IFREG | 0o644
LINK_MODE_BITS = _stat.S_IFLNK | 0o777
SUCCESS_ENVELOPE_BYTES = 42
HAPPY_PATH_BYTES = 99
TREE_TOTAL_BYTES = 30


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class _FakeSession(SftpSession):
    """SftpSession subclass that skips paramiko connect."""

    def __init__(self, alias: str = "fake") -> None:  # noqa: D401 — override
        # Skip super().__init__ so no paramiko handshake happens.
        self.alias = alias
        self._client = MagicMock()
        self.sftp = MagicMock()

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _make_ns(**overrides: Any) -> argparse.Namespace:
    """Build a populated argparse.Namespace with default-ish flag values."""
    base: dict[str, Any] = {
        "src_alias": "src",
        "src_path": "/tmp/src",
        "dst_alias": "dst",
        "dst_path": "/tmp/dst",
        "mode": "auto",
        "use_rsync": False,
        "recursive": False,
        "bwlimit": None,
        "json": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _attr(*, mode: int, size: int = 0, filename: str = "") -> Any:
    """Build a paramiko-shaped SFTPAttributes stand-in."""
    a = MagicMock()
    a.st_mode = mode
    a.st_size = size
    a.filename = filename
    return a


# -----------------------------------------------------------------------------
# build_parser — 4 cases
# -----------------------------------------------------------------------------


def test_parser_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """`--help` exits 0 and prints usage + all 5 flags + 4 positionals."""
    with pytest.raises(SystemExit) as exc_info:
        sst.main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()
    for positional in ("src_alias", "src_path", "dst_alias", "dst_path"):
        assert positional in out, f"missing positional: {positional}"
    for flag in ("--mode", "--use-rsync", "--recursive", "--bwlimit", "--json"):
        assert flag in out, f"missing flag: {flag}"


def test_parser_requires_all_positionals(capsys: pytest.CaptureFixture[str]) -> None:
    """Missing a positional → SystemExit (argparse exit code 2)."""
    with pytest.raises(SystemExit):
        sst.build_parser().parse_args(["pai", "/a", "pai"])  # 3 of 4
    err = capsys.readouterr().err
    assert "required" in err.lower() or "arguments" in err.lower()


def test_parser_mode_choices_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """`--mode bogus` rejected by argparse choices=."""
    with pytest.raises(SystemExit):
        sst.build_parser().parse_args(
            ["a", "/x", "b", "/y", "--mode", "bogus"]
        )
    err = capsys.readouterr().err
    assert "invalid choice" in err.lower()


def test_parser_bwlimit_is_int(capsys: pytest.CaptureFixture[str]) -> None:
    """`--bwlimit abc` rejected (type=int)."""
    with pytest.raises(SystemExit):
        sst.build_parser().parse_args(
            ["a", "/x", "b", "/y", "--bwlimit", "abc"]
        )
    err = capsys.readouterr().err
    assert "invalid" in err.lower()


# -----------------------------------------------------------------------------
# _route_mode — 5 cases
# -----------------------------------------------------------------------------


def test_route_mode_direct_refuses_with_mode_deferred() -> None:
    env = sst._route_mode(_make_ns(mode="direct"))
    assert env is not None
    assert env["exit_code"] == EXIT_MODE_DEFERRED
    assert env["data"]["failure_class"] == "mode_deferred"


def test_route_mode_use_rsync_refuses_with_flag_deferred() -> None:
    env = sst._route_mode(_make_ns(use_rsync=True))
    assert env is not None
    assert env["exit_code"] == EXIT_FLAG_DEFERRED
    assert env["data"]["failure_class"] == "flag_deferred"


def test_route_mode_use_rsync_takes_precedence_over_direct() -> None:
    """If both --use-rsync and --mode direct are passed, --use-rsync wins."""
    env = sst._route_mode(_make_ns(mode="direct", use_rsync=True))
    assert env is not None
    assert env["data"]["failure_class"] == "flag_deferred"


@pytest.mark.parametrize("mode", ["auto", "hybrid", "stream"])
def test_route_mode_passthroughs_return_none(mode: str) -> None:
    """auto / hybrid / stream all proceed (return None)."""
    assert sst._route_mode(_make_ns(mode=mode)) is None


def test_route_mode_envelope_carries_alias_pair() -> None:
    """Refusal envelopes still carry the src / dst alias pair in data."""
    env = sst._route_mode(_make_ns(mode="direct", src_alias="A", dst_alias="B"))
    assert env is not None
    assert env["data"]["src"].startswith("A:")
    assert env["data"]["dst"].startswith("B:")


# -----------------------------------------------------------------------------
# _pipe_file — 6 cases
# -----------------------------------------------------------------------------


def _wire_session_for_file(
    session: _FakeSession,
    *,
    src_size: int,
    chunks: list[bytes],
    dst_open_raises: Exception | None = None,
    write_raises: Exception | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build the .sftp.stat / .sftp.open mock chain for a single _pipe_file call.

    Returns (src_fp, dst_fp) so tests can introspect the mock file objects.
    """
    src_fp = MagicMock()
    src_fp.read.side_effect = [*chunks, b""]  # final empty signals EOF
    dst_fp = MagicMock()
    if write_raises is not None:
        dst_fp.write.side_effect = write_raises

    def _open(path: str, mode: str) -> MagicMock:
        if mode == "rb":
            return src_fp
        if mode == "wb":
            if dst_open_raises is not None:
                raise dst_open_raises
            return dst_fp
        raise AssertionError(f"unexpected open mode: {mode!r}")

    session.sftp.stat.return_value = _attr(mode=FILE_MODE_BITS, size=src_size)
    session.sftp.open.side_effect = _open
    return src_fp, dst_fp


def test_pipe_file_empty_file_writes_nothing() -> None:
    """A 0-byte src → 0 bytes_done, no write calls."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    _wire_session_for_file(src, src_size=0, chunks=[])
    # dst side: no open failures, no writes expected
    dst.sftp.open.return_value = MagicMock()

    n = sst._pipe_file(
        src, "/src", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
        start_time=0.0, running_bytes=0,
    )
    assert n == 0


def test_pipe_file_small_file_single_chunk() -> None:
    """One chunk smaller than block_size → 1 read of N bytes, 1 write."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    src_fp, dst_fp = _wire_session_for_file(
        src, src_size=BYTES_PER_FAKE_FILE, chunks=[b"x" * BYTES_PER_FAKE_FILE]
    )
    dst.sftp.open.side_effect = src.sftp.open.side_effect

    n = sst._pipe_file(
        src, "/src", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
        start_time=0.0, running_bytes=0,
    )
    assert n == BYTES_PER_FAKE_FILE
    dst_fp.write.assert_called_once_with(b"x" * BYTES_PER_FAKE_FILE)


def test_pipe_file_multi_chunk_loop() -> None:
    """Three full chunks → 3 reads + 3 writes, bytes_done == sum."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    chunks = [b"a" * SAMPLE_BLOCK, b"b" * SAMPLE_BLOCK, b"c" * SAMPLE_BLOCK]
    src_fp, dst_fp = _wire_session_for_file(
        src, src_size=THREE_CHUNKS * SAMPLE_BLOCK, chunks=chunks
    )
    dst.sftp.open.side_effect = src.sftp.open.side_effect

    n = sst._pipe_file(
        src, "/src", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
        start_time=0.0, running_bytes=0,
    )
    assert n == THREE_CHUNKS * SAMPLE_BLOCK
    assert dst_fp.write.call_count == THREE_CHUNKS


def test_pipe_file_bwlimit_triggers_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a tiny bwlimit, time.sleep is called at least once mid-loop."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    chunks = [b"x" * SAMPLE_BLOCK] * THREE_CHUNKS
    _wire_session_for_file(src, src_size=THREE_CHUNKS * SAMPLE_BLOCK, chunks=chunks)
    dst.sftp.open.side_effect = src.sftp.open.side_effect

    sleeps: list[float] = []
    monkeypatch.setattr(sst.time, "sleep", sleeps.append)
    # Pin monotonic so expected > actual on the first chunk.
    fake_now = [0.0]
    monkeypatch.setattr(sst.time, "monotonic", lambda: fake_now[0])

    sst._pipe_file(
        src, "/src", dst, "/dst",
        block_size=SAMPLE_BLOCK,
        bwlimit_kbs=1,  # 1 KB/s → 1024 B/s target → SAMPLE_BLOCK takes 0.25s
        start_time=0.0, running_bytes=0,
    )
    assert len(sleeps) >= 1, f"expected ≥1 sleep, got {sleeps}"
    assert all(s > 0 for s in sleeps)


def test_pipe_file_calls_ensure_dst_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ensure_dst_parent is invoked once with dst_session + dst_path."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    _wire_session_for_file(src, src_size=0, chunks=[])
    dst.sftp.open.side_effect = src.sftp.open.side_effect

    calls: list[tuple[Any, str]] = []
    monkeypatch.setattr(
        sst, "_ensure_dst_parent",
        lambda sess, path: calls.append((sess, path)),
    )

    sst._pipe_file(
        src, "/src", dst, "/srv/sub/dst.bin",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
        start_time=0.0, running_bytes=0,
    )
    assert calls == [(dst, "/srv/sub/dst.bin")]


def test_pipe_file_mid_pipe_oserror_wraps_as_transfer_io() -> None:
    """OSError thrown mid-chunk-loop wraps as _TransferIOError."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    boom = OSError("connection dropped")
    _wire_session_for_file(
        src, src_size=SAMPLE_BLOCK, chunks=[b"x" * SAMPLE_BLOCK],
        write_raises=boom,
    )
    dst.sftp.open.side_effect = src.sftp.open.side_effect

    with pytest.raises(sst._TransferIOError) as exc_info:
        sst._pipe_file(
            src, "/src", dst, "/dst",
            block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
            start_time=0.0, running_bytes=0,
        )
    assert exc_info.value.__cause__ is boom


def test_pipe_file_dst_open_failure_bubbles_as_oserror() -> None:
    """OSError on dst.open propagates as plain OSError (→ dst_path_invalid)."""
    src = _FakeSession("s")
    dst = _FakeSession("d")

    # src stat returns file shape; src.open returns ok; dst.open raises.
    src_fp = MagicMock()
    src_fp.read.side_effect = [b""]
    src.sftp.stat.return_value = _attr(mode=FILE_MODE_BITS, size=0)

    def _open(path: str, mode: str) -> Any:
        if mode == "rb":
            return src_fp
        raise PermissionError("permission denied")

    src.sftp.open.side_effect = _open
    dst.sftp.open.side_effect = _open

    with pytest.raises(OSError) as exc_info:
        sst._pipe_file(
            src, "/src", dst, "/dst",
            block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
            start_time=0.0, running_bytes=0,
        )
    # Importantly NOT _TransferIOError.
    assert not isinstance(exc_info.value, sst._TransferIOError)


# -----------------------------------------------------------------------------
# _pipe_tree — 6 cases
# -----------------------------------------------------------------------------


def test_pipe_tree_empty_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUBSTITUTE: empty src → 0 files, 0 bytes; mkdir_p called for root."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    src.sftp.listdir_attr.return_value = []
    mkdir_calls: list[str] = []
    dst.mkdir_p = MagicMock(side_effect=lambda p, mode=0o755: mkdir_calls.append(p))

    called: list[Any] = []
    monkeypatch.setattr(
        sst, "_pipe_file", lambda *a, **kw: called.append((a, kw)) or 0
    )

    files, total = sst._pipe_tree(
        src, "/root", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
    )
    assert (files, total) == (0, 0)
    assert called == []
    # mkdir for /dst (root) and for /dst (the relpath="." branch)
    assert "/dst" in mkdir_calls


def test_pipe_tree_single_file_flat(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUBSTITUTE: one file in root → 1 _pipe_file call, posix paths joined."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    src.sftp.listdir_attr.return_value = [
        _attr(mode=FILE_MODE_BITS, size=BYTES_PER_FAKE_FILE, filename="a.txt"),
    ]
    dst.mkdir_p = MagicMock()

    captured: list[tuple[str, str]] = []

    def fake_pipe_file(
        src_session: Any, src_p: str, dst_session: Any, dst_p: str,
        **_kw: Any,
    ) -> int:
        captured.append((src_p, dst_p))
        return BYTES_PER_FAKE_FILE

    monkeypatch.setattr(sst, "_pipe_file", fake_pipe_file)

    files, total = sst._pipe_tree(
        src, "/root", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
    )
    assert (files, total) == (1, BYTES_PER_FAKE_FILE)
    assert captured == [("/root/a.txt", "/dst/a.txt")]


def test_pipe_tree_nested_3_deep(monkeypatch: pytest.MonkeyPatch) -> None:
    """SUBSTITUTE: nested tree → posix joins on remote paths only."""
    src = _FakeSession("s")
    dst = _FakeSession("d")

    # /root: a.txt + sub/  ; /root/sub: b.txt + deep/ ; /root/sub/deep: c.txt
    def listdir(path: str) -> list[Any]:
        if path == "/root":
            return [
                _attr(mode=FILE_MODE_BITS, filename="a.txt", size=1),
                _attr(mode=DIR_MODE_BITS, filename="sub", size=0),
            ]
        if path == "/root/sub":
            return [
                _attr(mode=FILE_MODE_BITS, filename="b.txt", size=1),
                _attr(mode=DIR_MODE_BITS, filename="deep", size=0),
            ]
        if path == "/root/sub/deep":
            return [
                _attr(mode=FILE_MODE_BITS, filename="c.txt", size=1),
            ]
        return []

    src.sftp.listdir_attr.side_effect = listdir
    mkdir_calls: list[str] = []
    dst.mkdir_p = MagicMock(side_effect=lambda p, mode=0o755: mkdir_calls.append(p))

    captured_remotes: list[str] = []
    monkeypatch.setattr(
        sst, "_pipe_file",
        lambda *a, **kw: captured_remotes.append(a[3]) or 1,
    )

    files, total = sst._pipe_tree(
        src, "/root", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
    )
    assert (files, total) == (TEST_TREE_FILE_COUNT, TEST_TREE_FILE_COUNT)
    assert set(captured_remotes) == {
        "/dst/a.txt",
        "/dst/sub/b.txt",
        "/dst/sub/deep/c.txt",
    }
    for r in captured_remotes:
        assert "\\" not in r, "POSIX-only on remote leg"
    for d in ("/dst", "/dst/sub", "/dst/sub/deep"):
        assert d in mkdir_calls, f"missing mkdir for {d}"


def test_pipe_tree_skips_symlinks_and_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUBSTITUTE: symlinks / specials silently skipped (only S_ISREG xfer)."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    src.sftp.listdir_attr.return_value = [
        _attr(mode=FILE_MODE_BITS, filename="real.txt", size=1),
        _attr(mode=LINK_MODE_BITS, filename="link", size=0),
        _attr(mode=_stat.S_IFCHR | 0o600, filename="dev", size=0),
    ]
    dst.mkdir_p = MagicMock()
    captured: list[str] = []
    monkeypatch.setattr(
        sst, "_pipe_file",
        lambda *a, **kw: captured.append(a[1]) or 1,
    )

    files, total = sst._pipe_tree(
        src, "/root", dst, "/dst",
        block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
    )
    assert (files, total) == (1, 1)
    assert captured == ["/root/real.txt"]


def test_pipe_tree_partial_failure_updates_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SUBSTITUTE: mid-walk OSError → state reflects partial progress."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    src.sftp.listdir_attr.return_value = [
        _attr(mode=FILE_MODE_BITS, filename="a.txt", size=BYTES_PER_FAKE_FILE),
        _attr(mode=FILE_MODE_BITS, filename="b.txt", size=BYTES_PER_FAKE_FILE),
        _attr(mode=FILE_MODE_BITS, filename="c.txt", size=BYTES_PER_FAKE_FILE),
    ]
    dst.mkdir_p = MagicMock()

    def fake_pipe_file(_ss: Any, src_p: str, *args: Any, **_kw: Any) -> int:
        if src_p.endswith("b.txt"):
            raise sst._TransferIOError("network jitter")
        return BYTES_PER_FAKE_FILE

    monkeypatch.setattr(sst, "_pipe_file", fake_pipe_file)
    state = {"bytes_done": 0, "files_done": 0}

    with pytest.raises(sst._TransferIOError):
        sst._pipe_tree(
            src, "/root", dst, "/dst",
            block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
            state=state,
        )
    # a.txt succeeded; b.txt blew up; c.txt never ran.
    assert state == {"bytes_done": BYTES_PER_FAKE_FILE, "files_done": 1}


def test_pipe_tree_mkdir_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mkdir_p OSError propagates (→ dst_path_invalid in caller)."""
    src = _FakeSession("s")
    dst = _FakeSession("d")
    dst.mkdir_p = MagicMock(side_effect=PermissionError("no write here"))

    monkeypatch.setattr(
        sst, "_pipe_file", lambda *a, **kw: 0  # never reached
    )

    with pytest.raises(OSError):
        sst._pipe_tree(
            src, "/root", dst, "/dst",
            block_size=SAMPLE_BLOCK, bwlimit_kbs=None,
        )


# -----------------------------------------------------------------------------
# _classify_paramiko_failure — 6 cases
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_factory", "side", "expected"),
    [
        (lambda: paramiko.AuthenticationException("nope"), "src", "auth_src"),
        (lambda: paramiko.AuthenticationException("nope"), "dst", "auth_dst"),
        (lambda: socket.gaierror("nodename"), "src", "network_src"),
        (lambda: socket.gaierror("nodename"), "dst", "network_dst"),
        (lambda: paramiko.SSHException("transport bad"), "src", "auth_src"),
        (lambda: RuntimeError("unknown"), "dst", "remote_error"),
    ],
)
def test_classify_paramiko_failure(
    exc_factory: Any, side: str, expected: str,
) -> None:
    """Each (exception, side) maps to the expected failure_class string."""
    assert sst._classify_paramiko_failure(exc_factory(), which_side=side) == expected


def test_classify_connection_refused_dst() -> None:
    """ConnectionRefusedError on dst → network_dst."""
    assert (
        sst._classify_paramiko_failure(
            ConnectionRefusedError("refused"), which_side="dst"
        )
        == "network_dst"
    )


def test_classify_generic_oserror_buckets_as_network() -> None:
    """OSError that isn't gaierror / refused / timeout still → network_*."""
    assert (
        sst._classify_paramiko_failure(OSError("ENETUNREACH"), which_side="src")
        == "network_src"
    )


# -----------------------------------------------------------------------------
# Envelope wiring — 5 cases
# -----------------------------------------------------------------------------


def test_success_envelope_basic_shape() -> None:
    env = sst._success_envelope(
        _make_ns(mode="stream"), "stream",
        bytes_total=SUCCESS_ENVELOPE_BYTES, files=1, elapsed=0.5,
    )
    assert env["success"] is True
    assert env["exit_code"] == 0
    assert env["data"]["mode_used"] == "stream"
    assert env["data"]["bytes"] == SUCCESS_ENVELOPE_BYTES
    assert env["data"]["files_transferred"] == 1
    assert env["data"]["failure_class"] is None
    # No marker when --mode stream was passed explicitly.
    assert "auto_resolved_to" not in env["data"]
    assert "hybrid_resolved_to" not in env["data"]


def test_success_envelope_auto_marker_set() -> None:
    env = sst._success_envelope(
        _make_ns(mode="auto"), "stream", bytes_total=10, files=1, elapsed=0.1
    )
    assert env["data"].get("auto_resolved_to") == "stream"
    assert "hybrid_resolved_to" not in env["data"]


def test_success_envelope_hybrid_marker_set() -> None:
    env = sst._success_envelope(
        _make_ns(mode="hybrid"), "stream", bytes_total=10, files=1, elapsed=0.1
    )
    assert env["data"].get("hybrid_resolved_to") == "stream"
    assert "auto_resolved_to" not in env["data"]


def test_failure_envelope_carries_failure_class() -> None:
    env = sst._failure_envelope(
        _make_ns(mode="stream"), "src_path_missing",
        partial_bytes=0, partial_files=0, elapsed=0.05,
    )
    assert env["success"] is False
    assert env["exit_code"] == EXIT_SRC_PATH_MISSING
    assert env["data"]["failure_class"] == "src_path_missing"
    assert "stderr" in env and env["stderr"]


def test_failure_envelope_mode_used_none_for_direct() -> None:
    """When args.mode == 'direct', failure envelope reports mode_used=None."""
    env = sst._failure_envelope(_make_ns(mode="direct"), "mode_deferred")
    assert env["data"]["mode_used"] is None


# -----------------------------------------------------------------------------
# main() integration — 6 cases
# -----------------------------------------------------------------------------


def _capture_main(
    argv: list[str], capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, Any]]:
    rc = sst.main(argv)
    env = json.loads(capsys.readouterr().out)
    return rc, env


def test_main_direct_exits_20(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """--mode direct → exit 20 mode_deferred without any session opened."""
    monkeypatch.setattr(
        sst, "SftpSession",
        lambda *a, **kw: pytest.fail("SftpSession must not open for --mode direct"),
    )
    rc, env = _capture_main(
        ["pai", "/a", "pai", "/b", "--mode", "direct", "--json"], capsys
    )
    assert rc == EXIT_MODE_DEFERRED
    assert env["data"]["failure_class"] == "mode_deferred"


def test_main_use_rsync_exits_21(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """--use-rsync → exit 21 flag_deferred without any session opened."""
    monkeypatch.setattr(
        sst, "SftpSession",
        lambda *a, **kw: pytest.fail("SftpSession must not open for --use-rsync"),
    )
    rc, env = _capture_main(
        ["pai", "/a", "pai", "/b", "--use-rsync", "--json"], capsys
    )
    assert rc == EXIT_FLAG_DEFERRED
    assert env["data"]["failure_class"] == "flag_deferred"


def test_main_src_path_missing_exits_26(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """stat returning None on src → exit 26 src_path_missing."""
    sessions: list[_FakeSession] = []

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        s.sftp.stat.side_effect = FileNotFoundError("nope")
        sessions.append(s)
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)
    rc, env = _capture_main(
        ["pai", "/nope", "pai", "/dst", "--json"], capsys
    )
    assert rc == EXIT_SRC_PATH_MISSING
    assert env["data"]["failure_class"] == "src_path_missing"


def test_main_src_is_directory_no_recursive_exits_27(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Dir src without --recursive → exit 27 src_is_directory."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        s.sftp.stat.return_value = _attr(mode=DIR_MODE_BITS, size=0)
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)
    rc, env = _capture_main(
        ["pai", "/srv", "pai", "/dst", "--json"], capsys
    )
    assert rc == EXIT_SRC_IS_DIRECTORY
    assert env["data"]["failure_class"] == "src_is_directory"


def test_main_dst_path_invalid_exits_28(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """OSError on mkdir_p before transfer → exit 28 dst_path_invalid."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        s.sftp.stat.return_value = _attr(mode=FILE_MODE_BITS, size=10)
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)

    def bad_pipe_file(*_a: Any, **_kw: Any) -> int:
        raise PermissionError("no write here")

    monkeypatch.setattr(sst, "_pipe_file", bad_pipe_file)

    rc, env = _capture_main(["pai", "/x", "pai", "/y", "--json"], capsys)
    assert rc == EXIT_DST_PATH_INVALID
    assert env["data"]["failure_class"] == "dst_path_invalid"


def test_main_single_file_happy_path_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Stream single file with mocked _pipe_file → exit 0, mode_used=stream."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        s.sftp.stat.return_value = _attr(mode=FILE_MODE_BITS, size=HAPPY_PATH_BYTES)
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)
    monkeypatch.setattr(sst, "_pipe_file", lambda *a, **kw: HAPPY_PATH_BYTES)

    rc, env = _capture_main(["pai", "/x", "pai", "/y", "--json"], capsys)
    assert rc == EXIT_OK
    assert env["success"] is True
    assert env["data"]["mode_used"] == "stream"
    assert env["data"]["bytes"] == HAPPY_PATH_BYTES
    assert env["data"]["files_transferred"] == 1
    # default --mode is auto → auto_resolved_to marker set
    assert env["data"].get("auto_resolved_to") == "stream"


# -----------------------------------------------------------------------------
# main() extras for branch coverage
# -----------------------------------------------------------------------------


def test_main_recursive_happy_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Stream a directory with --recursive → exit 0, _pipe_tree was called."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        s.sftp.stat.return_value = _attr(mode=DIR_MODE_BITS, size=0)
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)
    monkeypatch.setattr(
        sst, "_pipe_tree", lambda *a, **kw: (TEST_TREE_FILE_COUNT, TREE_TOTAL_BYTES)
    )

    rc, env = _capture_main(
        ["pai", "/srv", "pai", "/dst", "--recursive", "--mode", "stream", "--json"],
        capsys,
    )
    assert rc == EXIT_OK
    assert env["data"]["files_transferred"] == TEST_TREE_FILE_COUNT
    assert env["data"]["bytes"] == TREE_TOTAL_BYTES


def test_main_network_src_exits_22(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Connect failure on src session → exit 22 network_src."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        raise socket.gaierror("nodename")

    monkeypatch.setattr(sst, "SftpSession", factory)
    rc, env = _capture_main(["miss", "/a", "pai", "/b", "--json"], capsys)
    assert rc == EXIT_NETWORK_SRC
    assert env["data"]["failure_class"] == "network_src"


def test_main_auth_dst_exits_25(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """AuthenticationException on dst session → exit 25 auth_dst."""
    counter = {"opened": 0}

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        counter["opened"] += 1
        if counter["opened"] == 1:
            return _FakeSession(alias)  # src ok
        raise paramiko.AuthenticationException("bad key")

    monkeypatch.setattr(sst, "SftpSession", factory)
    rc, env = _capture_main(["pai", "/a", "pai", "/b", "--json"], capsys)
    assert rc == EXIT_AUTH_DST
    assert env["data"]["failure_class"] == "auth_dst"


def test_main_transfer_io_exits_29(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """_TransferIOError raised by _pipe_file → exit 29 transfer_io."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        s.sftp.stat.return_value = _attr(mode=FILE_MODE_BITS, size=10)
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)

    def boom(*_a: Any, **_kw: Any) -> int:
        raise sst._TransferIOError("connection dropped")

    monkeypatch.setattr(sst, "_pipe_file", boom)
    rc, env = _capture_main(["pai", "/x", "pai", "/y", "--json"], capsys)
    assert rc == EXIT_TRANSFER_IO
    assert env["data"]["failure_class"] == "transfer_io"


# -----------------------------------------------------------------------------
# Small helpers (_ensure_dst_parent / _stat_or_none / _format_hint)
# -----------------------------------------------------------------------------


def test_ensure_dst_parent_skips_root() -> None:
    """_ensure_dst_parent on a top-level path doesn't call mkdir_p."""
    sess = _FakeSession("d")
    sess.mkdir_p = MagicMock()
    sst._ensure_dst_parent(sess, "/file.txt")
    sess.mkdir_p.assert_not_called()


def test_ensure_dst_parent_creates_when_non_root() -> None:
    sess = _FakeSession("d")
    sess.mkdir_p = MagicMock()
    sst._ensure_dst_parent(sess, "/srv/sub/file.txt")
    sess.mkdir_p.assert_called_once_with("/srv/sub")


def test_stat_or_none_returns_none_on_filenotfounderror() -> None:
    sess = _FakeSession("s")
    sess.sftp.stat.side_effect = FileNotFoundError()
    assert sst._stat_or_none(sess, "/x") is None


def test_stat_or_none_returns_none_on_enoent_oserror() -> None:
    sess = _FakeSession("s")
    err = OSError("nope")
    err.errno = 2  # ENOENT
    sess.sftp.stat.side_effect = err
    assert sst._stat_or_none(sess, "/x") is None


def test_stat_or_none_reraises_other_oserror() -> None:
    sess = _FakeSession("s")
    err = OSError("perm")
    err.errno = 13  # EACCES
    sess.sftp.stat.side_effect = err
    with pytest.raises(OSError):
        sst._stat_or_none(sess, "/x")


def test_format_hint_substitutes_placeholders() -> None:
    """Failure hints interpolate src_alias / dst_alias / detail correctly."""
    msg = sst._format_hint(
        "network_src", _make_ns(src_alias="ALPHA"), detail="DNS failed",
    )
    assert "ALPHA" in msg
    assert "DNS failed" in msg


def test_format_hint_unknown_class_returns_empty() -> None:
    """Unknown failure class → empty string (no crash)."""
    assert sst._format_hint("not_a_real_class", _make_ns()) == ""


# -----------------------------------------------------------------------------
# Pre-existing manifest contract — verify the script still works as a CLI
# -----------------------------------------------------------------------------


def test_emit_returns_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit emits the envelope and returns the exit_code unchanged."""
    args = _make_ns(json=True)
    env = sst._success_envelope(args, "stream", bytes_total=0, files=0, elapsed=0.0)
    rc = sst._emit(env, args)
    assert rc == 0
    # JSON envelope went to stdout
    out = capsys.readouterr().out
    assert json.loads(out)["success"] is True


def test_emit_human_mode_writes_human_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --json, success envelope's human summary lands on stdout."""
    args = _make_ns(json=False)
    env = sst._success_envelope(args, "stream", bytes_total=10, files=1, elapsed=0.2)
    sst._emit(env, args)
    cap = capsys.readouterr()
    assert "transferred 1 file(s)" in cap.out


# -----------------------------------------------------------------------------
# Catch-all error paths in main()
# -----------------------------------------------------------------------------


def test_main_stat_oserror_buckets_as_remote_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-ENOENT OSError on src stat → exit 1 remote_error (catch-all)."""

    def factory(alias: str, **_kw: Any) -> _FakeSession:
        s = _FakeSession(alias)
        err = OSError("permission denied")
        err.errno = 13  # EACCES — not ENOENT
        s.sftp.stat.side_effect = err
        return s

    monkeypatch.setattr(sst, "SftpSession", factory)
    rc, env = _capture_main(["pai", "/x", "pai", "/y", "--json"], capsys)
    assert rc == EXIT_GENERIC
    assert env["data"]["failure_class"] == "remote_error"


def test_main_outer_catchall_handles_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A wholly unexpected exception bubbles to the outer try → remote_error."""

    class _BadStack:
        def __enter__(self) -> Any:
            raise RuntimeError("ExitStack imploded")

        def __exit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(sst.contextlib, "ExitStack", _BadStack)
    rc, env = _capture_main(["pai", "/x", "pai", "/y", "--json"], capsys)
    assert rc == EXIT_GENERIC
    assert env["data"]["failure_class"] == "remote_error"
