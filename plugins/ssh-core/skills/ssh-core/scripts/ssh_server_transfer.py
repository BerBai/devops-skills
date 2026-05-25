#!/usr/bin/env python3
"""ssh_server_transfer.py — move bytes between two remote hosts (v1.0).

v1.0 surface:
    ssh_server_transfer.py <src_alias> <src_path> <dst_alias> <dst_path>
        [--mode auto|direct|stream|hybrid]    default: auto
        [--use-rsync]                          (deferred v1.1)
        [--recursive]                          tree copy
        [--bwlimit KB/s]                       1 KB = 1024 B/s; tree-wide pacing
        [--json]

Implementation notes:
    - v1.0 ships **stream mode only**: opens two ``SftpSession``s and
      pipes bytes src->dst chunk-by-chunk in memory. Never staged to
      local disk.
    - ``--mode auto`` / ``--mode hybrid`` route to stream and log
      ``data.{auto,hybrid}_resolved_to``; ``--mode direct`` refuses with
      ``failure_class="mode_deferred"``; ``--use-rsync`` refuses with
      ``failure_class="flag_deferred"``. Both light up in v1.1.
    - Recursive walk skips symlinks / device / fifo silently (scp-like).
      No resume / no sha256 verify in v1.0 (matches scp semantics).
    - Bwlimit pacing is tree-wide: ``start_time`` + running byte counter
      thread across every ``_pipe_file`` call inside ``_pipe_tree``.

Failure classes (per-script namespace): ``mode_deferred`` 20,
``flag_deferred`` 21, ``network_src`` 22, ``network_dst`` 23,
``auth_src`` 24, ``auth_dst`` 25, ``src_path_missing`` 26,
``src_is_directory`` 27, ``dst_path_invalid`` 28, ``transfer_io`` 29,
``remote_error`` 1, ``timeout`` 124 (reserved).

See:
    .trellis/tasks/05-24-ssh-core-ssh-server-transfer/{prd,design,implement}.md
    .trellis/spec/backend/adr-003-sftp-transport.md (SftpSession contract)
    .trellis/spec/backend/error-handling.md (exit-code namespace)
    references/workflows.md §3 (4-mode table)
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import posixpath
import socket
import stat as _stat
import sys
import time
from pathlib import Path
from typing import Any

import paramiko  # type: ignore[import-untyped]  # isinstance() only — exception surface

sys.path.insert(0, str(Path(__file__).parent))
from lib import emit, json_result  # noqa: E402, I001
from lib.sftp_transport import SftpSession  # noqa: E402

# --- Constants --------------------------------------------------------------

DEFAULT_BLOCK_SIZE = 131072  # 128 KiB; matches lib.sftp_transport precedent

# Failure classes (per-script namespace; do not extend lib.classify_failure).
FAIL_MODE_DEFERRED = "mode_deferred"
FAIL_FLAG_DEFERRED = "flag_deferred"
FAIL_NETWORK_SRC = "network_src"
FAIL_NETWORK_DST = "network_dst"
FAIL_AUTH_SRC = "auth_src"
FAIL_AUTH_DST = "auth_dst"
FAIL_SRC_PATH_MISSING = "src_path_missing"
FAIL_SRC_IS_DIRECTORY = "src_is_directory"
FAIL_DST_PATH_INVALID = "dst_path_invalid"
FAIL_TRANSFER_IO = "transfer_io"
FAIL_TIMEOUT = "timeout"
FAIL_REMOTE_ERROR = "remote_error"

# Exit codes — per-script range 20-29 registered in error-handling.md (2026-05-25).
_EXIT_CODES: dict[str, int] = {
    FAIL_MODE_DEFERRED: 20,
    FAIL_FLAG_DEFERRED: 21,
    FAIL_NETWORK_SRC: 22,
    FAIL_NETWORK_DST: 23,
    FAIL_AUTH_SRC: 24,
    FAIL_AUTH_DST: 25,
    FAIL_SRC_PATH_MISSING: 26,
    FAIL_SRC_IS_DIRECTORY: 27,
    FAIL_DST_PATH_INVALID: 28,
    FAIL_TRANSFER_IO: 29,
    FAIL_TIMEOUT: 124,
    FAIL_REMOTE_ERROR: 1,
}

# Human-mode stderr hints (one-line per failure class).
_HUMAN_HINTS: dict[str, str] = {
    FAIL_MODE_DEFERRED: "--mode direct is deferred to v1.1; use --mode stream",
    FAIL_FLAG_DEFERRED: "--use-rsync requires --mode direct (v1.1); drop the flag",
    FAIL_NETWORK_SRC: "cannot connect to source alias {src_alias}: {detail}",
    FAIL_NETWORK_DST: "cannot connect to destination alias {dst_alias}: {detail}",
    FAIL_AUTH_SRC: "authentication failed against {src_alias}: {detail}",
    FAIL_AUTH_DST: "authentication failed against {dst_alias}: {detail}",
    FAIL_SRC_PATH_MISSING: "source path does not exist on {src_alias}: {src_path}",
    FAIL_SRC_IS_DIRECTORY: "source is a directory on {src_alias}; pass --recursive",
    FAIL_DST_PATH_INVALID: (
        "cannot write to destination on {dst_alias}: {dst_path} "
        "(check parent exists / permissions): {detail}"
    ),
    FAIL_TRANSFER_IO: "transfer interrupted after {partial_bytes} bytes: {detail}",
    FAIL_REMOTE_ERROR: "unexpected error: {detail}",
}


class _TransferIOError(OSError):
    """Internal marker: an OSError that happened *after* the dst file was opened.

    Caller distinguishes this (→ ``transfer_io``) from a bare ``OSError``
    raised before / during dst open (→ ``dst_path_invalid``). Because it
    subclasses ``OSError``, ``except OSError`` still catches it — order
    the handler so ``_TransferIOError`` is checked first.
    """


# --- Parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ssh_server_transfer.py",
        description=(
            "Move bytes between two remote hosts by streaming through two open "
            "SFTP sessions (no local disk staging). v1.0 ships --mode stream "
            "only; --mode auto / hybrid route to stream; --mode direct refuses "
            "with failure_class=mode_deferred."
        ),
    )
    p.add_argument("src_alias", help="Source host alias from ~/.ssh/config")
    p.add_argument("src_path", help="Source path on src_alias (POSIX)")
    p.add_argument("dst_alias", help="Destination host alias from ~/.ssh/config")
    p.add_argument("dst_path", help="Destination path on dst_alias (POSIX)")
    p.add_argument(
        "--mode",
        choices=["auto", "direct", "stream", "hybrid"],
        default="auto",
        help=(
            "Transfer mode (v1.0: auto / hybrid / stream all run stream; "
            "direct deferred to v1.1)"
        ),
    )
    p.add_argument(
        "--use-rsync",
        action="store_true",
        help="(deferred v1.1) Replace scp with rsync in the direct path",
    )
    p.add_argument(
        "--recursive",
        action="store_true",
        help="Recursive directory copy (symlinks / specials silently skipped)",
    )
    p.add_argument(
        "--bwlimit",
        type=int,
        default=None,
        help="Bandwidth cap in KB/s (1 KB = 1024 B/s); pacing is tree-wide",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit shared JSON output envelope on stdout",
    )
    return p


# --- Mode routing -----------------------------------------------------------


def _route_mode(args: argparse.Namespace) -> dict[str, Any] | None:
    """Return a failure envelope if mode is deferred/refused; None to proceed.

    Refusal precedence (matches design.md): ``--use-rsync`` first (it is
    surface-level coupled to ``--mode direct`` regardless of what
    ``--mode`` actually says), then ``--mode direct``. Auto / hybrid /
    stream all return ``None`` (proceed to stream path).
    """
    if args.use_rsync:
        return _failure_envelope(args, FAIL_FLAG_DEFERRED)
    if args.mode == "direct":
        return _failure_envelope(args, FAIL_MODE_DEFERRED)
    return None


# --- Envelope construction --------------------------------------------------


def _format_hint(fclass: str, args: argparse.Namespace, **fmt_args: Any) -> str:
    """Build a one-line human-mode stderr hint for the given failure class."""
    template = _HUMAN_HINTS.get(fclass, "")
    if not template:
        return ""
    return template.format(
        src_alias=args.src_alias,
        src_path=args.src_path,
        dst_alias=args.dst_alias,
        dst_path=args.dst_path,
        partial_bytes=fmt_args.get("partial_bytes", 0),
        detail=fmt_args.get("detail", ""),
    )


def _success_envelope(
    args: argparse.Namespace,
    mode_used: str,
    bytes_total: int,
    files: int,
    elapsed: float,
) -> dict[str, Any]:
    """Build the success envelope per PRD D8."""
    data: dict[str, Any] = {
        "mode_used": mode_used,
        "bytes": bytes_total,
        "files_transferred": files,
        "elapsed_s": round(elapsed, 3),
        "src": f"{args.src_alias}:{args.src_path}",
        "dst": f"{args.dst_alias}:{args.dst_path}",
        "failure_class": None,
    }
    # auto_resolved_to / hybrid_resolved_to: conditional on the *original*
    # --mode value so callers can audit "you asked for auto, you got stream".
    # Skip the marker when --mode stream was passed explicitly.
    if args.mode == "auto":
        data["auto_resolved_to"] = "stream"
    elif args.mode == "hybrid":
        data["hybrid_resolved_to"] = "stream"

    human = (
        f"transferred {files} file(s), {bytes_total} bytes in "
        f"{round(elapsed, 3)}s via {mode_used} "
        f"({args.src_alias}:{args.src_path} -> {args.dst_alias}:{args.dst_path})\n"
    )
    return json_result(success=True, exit_code=0, stdout=human, stderr="", data=data)


def _failure_envelope(
    args: argparse.Namespace,
    fclass: str,
    *,
    partial_bytes: int = 0,
    partial_files: int = 0,
    elapsed: float = 0.0,
    detail: str = "",
) -> dict[str, Any]:
    """Build a failure envelope per PRD D8 / design.md."""
    data: dict[str, Any] = {
        "mode_used": "stream" if args.mode != "direct" else None,
        "bytes": partial_bytes,
        "files_transferred": partial_files,
        "elapsed_s": round(elapsed, 3),
        "src": f"{args.src_alias}:{args.src_path}",
        "dst": f"{args.dst_alias}:{args.dst_path}",
        "failure_class": fclass,
    }
    hint = _format_hint(fclass, args, partial_bytes=partial_bytes, detail=detail)
    stderr = hint if hint.endswith("\n") else (hint + "\n" if hint else "")
    return json_result(
        success=False,
        exit_code=_EXIT_CODES.get(fclass, 1),
        stdout="",
        stderr=stderr,
        data=data,
    )


# --- Failure classification -------------------------------------------------


def _classify_paramiko_failure(exc: BaseException, *, which_side: str) -> str:
    """Map a paramiko / socket exception → failure_class for src or dst side.

    ``which_side`` is ``"src"`` or ``"dst"``. The same exception type
    means different things depending on which ``SftpSession.__init__``
    raised it — that's why callers pass the side explicitly.
    """
    if isinstance(exc, paramiko.AuthenticationException):
        return FAIL_AUTH_SRC if which_side == "src" else FAIL_AUTH_DST
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError, socket.timeout)):
        return FAIL_NETWORK_SRC if which_side == "src" else FAIL_NETWORK_DST
    if isinstance(exc, paramiko.SSHException):
        # SSHException is paramiko's broad transport bucket — could be either
        # network or auth; we lean auth because the network-shape exceptions
        # above (gaierror / refused / timeout) already pre-empt that.
        return FAIL_AUTH_SRC if which_side == "src" else FAIL_AUTH_DST
    if isinstance(exc, OSError):
        # Generic network OSError that didn't match the more specific
        # network-shape exceptions above (e.g. ENETUNREACH).
        return FAIL_NETWORK_SRC if which_side == "src" else FAIL_NETWORK_DST
    return FAIL_REMOTE_ERROR


# --- SFTP helpers -----------------------------------------------------------


def _stat_or_none(session: SftpSession, path: str) -> Any:
    """Return SFTP stat for ``path`` or ``None`` if missing.

    Wraps paramiko's "errno ENOENT vs other IOError" inconsistency: None
    is the canonical signal for "src does not exist", so the main flow
    can branch on ``None`` cleanly.
    """
    assert session.sftp is not None
    try:
        return session.sftp.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        if getattr(e, "errno", None) == errno.ENOENT:
            return None
        raise


def _ensure_dst_parent(session: SftpSession, path: str) -> None:
    """Idempotent ``mkdir_p`` of ``dirname(path)`` on the remote session.

    Only acts when the parent is non-empty / non-root. ``mkdir_p`` is
    itself idempotent (already shipped by ``lib.sftp_transport``).
    """
    parent = posixpath.dirname(path)
    if parent and parent not in ("/", ""):
        session.mkdir_p(parent)


# --- Stream pipe primitives -------------------------------------------------


def _pipe_file(
    src_session: SftpSession,
    src_path: str,
    dst_session: SftpSession,
    dst_path: str,
    *,
    block_size: int,
    bwlimit_kbs: int | None,
    start_time: float,
    running_bytes: int,
) -> int:
    """Stream one src file -> dst chunk-by-chunk via in-memory pipe.

    Caller pre-validates ``src_path`` exists (typically via
    ``_stat_or_none``). Steps:

      1. ``mkdir_p`` dst parent (any OSError -> ``dst_path_invalid``).
      2. Open src ``"rb"``, then dst ``"wb"`` (no resume in v1.0).
         OSError on dst.open -> ``dst_path_invalid``.
      3. Chunked read/write; any OSError mid-loop -> ``_TransferIOError``
         (-> ``transfer_io`` in the caller). ``_TransferIOError``
         subclasses ``OSError`` so order the except clauses carefully.

    Bwlimit is tree-wide: callers thread ``start_time`` + ``running_bytes``
    so the KB/s cap holds across files in ``_pipe_tree``. Single-file
    callers pass ``running_bytes=0`` and a fresh ``start_time``.
    """
    assert src_session.sftp is not None
    assert dst_session.sftp is not None

    src_attr = src_session.sftp.stat(src_path)
    _ = int(src_attr.st_size or 0)  # validate stat shape; size is informational
    bytes_done = 0
    target_bps = bwlimit_kbs * 1024 if bwlimit_kbs else None

    _ensure_dst_parent(dst_session, dst_path)

    src_fp = src_session.sftp.open(src_path, "rb")
    try:
        dst_fp = dst_session.sftp.open(dst_path, "wb")
        try:
            src_fp.prefetch()
            dst_fp.set_pipelined(True)
            try:
                while True:
                    chunk = src_fp.read(block_size)
                    if not chunk:
                        break
                    dst_fp.write(chunk)
                    bytes_done += len(chunk)
                    if target_bps:
                        expected = (running_bytes + bytes_done) / target_bps
                        actual = time.monotonic() - start_time
                        if expected > actual:
                            time.sleep(expected - actual)
            except OSError as e:
                raise _TransferIOError(str(e)) from e
        finally:
            with contextlib.suppress(Exception):
                dst_fp.close()
    finally:
        with contextlib.suppress(Exception):
            src_fp.close()

    return bytes_done


def _pipe_tree(
    src_session: SftpSession,
    src_root: str,
    dst_session: SftpSession,
    dst_root: str,
    *,
    block_size: int,
    bwlimit_kbs: int | None,
    state: dict[str, int] | None = None,
) -> tuple[int, int]:
    """Iterative-BFS walk of src_root -> dst_root.

    Iterative (not recursive) so deep trees don't risk Python stack
    overflow. ``posixpath`` (not ``os.path``) throughout — remote paths
    are POSIX even when the script runs on Windows. Only ``S_ISREG``
    files transfer; symlinks / device / fifo silently skipped.

    ``state`` is an optional mutable dict (``{"bytes_done", "files_done"}``)
    for callers to recover partial-progress info on mid-walk exception.
    Bwlimit pacing window is the whole tree (``start_time`` here, not in
    ``_pipe_file``).
    """
    assert src_session.sftp is not None
    assert dst_session.sftp is not None
    if state is None:
        state = {"bytes_done": 0, "files_done": 0}

    files = 0
    total_bytes = 0
    start_time = time.monotonic()

    dst_session.mkdir_p(dst_root)
    stack: list[str] = [src_root]
    while stack:
        src_dir = stack.pop()
        rel_dir = posixpath.relpath(src_dir, src_root)
        dst_dir = dst_root if rel_dir == "." else posixpath.join(dst_root, rel_dir)
        dst_session.mkdir_p(dst_dir)

        for entry in src_session.sftp.listdir_attr(src_dir):
            entry_mode = int(entry.st_mode or 0)
            src_p = posixpath.join(src_dir, entry.filename)
            dst_p = posixpath.join(dst_dir, entry.filename)
            if _stat.S_ISDIR(entry_mode):
                stack.append(src_p)
            elif _stat.S_ISREG(entry_mode):
                bytes_done = _pipe_file(
                    src_session, src_p,
                    dst_session, dst_p,
                    block_size=block_size,
                    bwlimit_kbs=bwlimit_kbs,
                    start_time=start_time,
                    running_bytes=total_bytes,
                )
                files += 1
                total_bytes += bytes_done
                state["bytes_done"] = total_bytes
                state["files_done"] = files
            # else: symlink / device / fifo / socket — silently skipped

    return files, total_bytes


# --- Emit helper ------------------------------------------------------------


def _emit(envelope: dict[str, Any], args: argparse.Namespace) -> int:
    """Emit envelope on the right channel and return its exit code."""
    emit(envelope, as_json=args.json)
    return int(envelope["exit_code"])


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 — distinct failure paths
    args = build_parser().parse_args(argv)

    # Mode routing: --use-rsync and --mode direct refuse before any
    # network touch.
    pre = _route_mode(args)
    if pre is not None:
        return _emit(pre, args)

    start_time = time.monotonic()
    state: dict[str, int] = {"bytes_done": 0, "files_done": 0}

    try:
        with contextlib.ExitStack() as stack:
            # Open src session.
            try:
                src_session = stack.enter_context(SftpSession(args.src_alias))
            except Exception as e:  # noqa: BLE001 — paramiko surface is broad
                fclass = _classify_paramiko_failure(e, which_side="src")
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(args, fclass, elapsed=elapsed, detail=str(e)),
                    args,
                )

            # Open dst session.
            try:
                dst_session = stack.enter_context(SftpSession(args.dst_alias))
            except Exception as e:  # noqa: BLE001
                fclass = _classify_paramiko_failure(e, which_side="dst")
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(args, fclass, elapsed=elapsed, detail=str(e)),
                    args,
                )

            # Stat src — None means missing.
            try:
                src_attr = _stat_or_none(src_session, args.src_path)
            except OSError as e:
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(
                        args, FAIL_REMOTE_ERROR, elapsed=elapsed, detail=str(e),
                    ),
                    args,
                )
            if src_attr is None:
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(args, FAIL_SRC_PATH_MISSING, elapsed=elapsed),
                    args,
                )

            is_dir = _stat.S_ISDIR(int(src_attr.st_mode or 0))
            if is_dir and not args.recursive:
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(args, FAIL_SRC_IS_DIRECTORY, elapsed=elapsed),
                    args,
                )

            # Run the transfer. _TransferIOError comes first because it's
            # an OSError subclass — order matters.
            try:
                if is_dir:
                    files, bytes_total = _pipe_tree(
                        src_session, args.src_path,
                        dst_session, args.dst_path,
                        block_size=DEFAULT_BLOCK_SIZE,
                        bwlimit_kbs=args.bwlimit,
                        state=state,
                    )
                else:
                    bytes_done = _pipe_file(
                        src_session, args.src_path,
                        dst_session, args.dst_path,
                        block_size=DEFAULT_BLOCK_SIZE,
                        bwlimit_kbs=args.bwlimit,
                        start_time=start_time,
                        running_bytes=0,
                    )
                    state["bytes_done"] = bytes_done
                    state["files_done"] = 1
                    files, bytes_total = 1, bytes_done
            except _TransferIOError as e:
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(
                        args, FAIL_TRANSFER_IO,
                        partial_bytes=state["bytes_done"],
                        partial_files=state["files_done"],
                        elapsed=elapsed,
                        detail=str(e.__cause__ or e),
                    ),
                    args,
                )
            except OSError as e:
                # Pre-pipe OSError: parent missing / write permission denied /
                # disk full at dst.open.
                elapsed = time.monotonic() - start_time
                return _emit(
                    _failure_envelope(
                        args, FAIL_DST_PATH_INVALID,
                        partial_bytes=state["bytes_done"],
                        partial_files=state["files_done"],
                        elapsed=elapsed,
                        detail=str(e),
                    ),
                    args,
                )

            elapsed = time.monotonic() - start_time
            return _emit(
                _success_envelope(args, "stream", bytes_total, files, elapsed),
                args,
            )
    except Exception as e:  # noqa: BLE001 — catch-all for unexpected errors
        elapsed = time.monotonic() - start_time
        return _emit(
            _failure_envelope(
                args, FAIL_REMOTE_ERROR, elapsed=elapsed, detail=str(e),
            ),
            args,
        )


if __name__ == "__main__":
    sys.exit(main())
