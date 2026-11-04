#!/usr/bin/env python3
"""ssh_server_transfer.py — move bytes between two remote hosts (v1.4.0).

v1.2 surface::

    ssh_server_transfer.py <src_alias> <src_path> <dst_alias> <dst_path>
        [--mode auto|direct|stream|hybrid]    default: auto
        [--use-rsync]                          direct path only
        [--recursive]                          tree copy
        [--bwlimit KB/s]
        [--ssh-config PATH] [--accept-new-hostkey]
        [--audit-log|--no-audit] [--redact|--no-redact]
        [--json] [--version]

Modes (SSH-14 — all four are implemented now)
---------------------------------------------
``stream``
    Two open SFTP sessions, bytes piped through this process in memory. Needs
    paramiko; supports bwlimit and per-file progress.
``direct``
    The two hosts talk to each other: ``ssh src 'tar -cf -' | ssh dst 'tar -xf -'``
    (or ``rsync`` started on the source host with ``--use-rsync``). No paramiko,
    no local disk, and the machine running the script only shuttles bytes.
``hybrid``
    ``direct`` first; if either host lacks ``tar``/``rsync`` it falls back to
    ``stream``. Reported as ``data.hybrid_resolved_to``.
``auto``
    ``stream`` when paramiko is importable (it can verify and resume),
    ``direct`` otherwise. Reported as ``data.auto_resolved_to``.

Exit codes come from :mod:`lib.exit_codes` (SSH-19). Every mode is implemented
(SSH-14 / DV-20), so the old ``mode_deferred``/``flag_deferred`` classes are no
longer emitted: ``network_src``/``network_dst`` map
to 102, ``auth_src``/``auth_dst`` to 100, ``src_path_missing`` to 162,
``dst_path_invalid`` to 163, ``transfer_io`` to 164, ``timeout`` to 124.
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

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402, I001
    append_audit,
    audit_cli_flags,
    audit_enabled,
    emit,
    json_result,
)
from lib.auth import AUTH_CHOICES, DEFAULT_PASSWORD_ENV  # noqa: E402
from lib.auth import resolve as resolve_auth  # noqa: E402
from lib.cli import SSHArgumentParser, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_DEPENDENCY_MISSING,
    EXIT_HOST_KEY,
    EXIT_INTERNAL,
    EXIT_MODE_UNSUPPORTED,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_REMOTE_PATH_INVALID,
    EXIT_REMOTE_PATH_MISSING,
    EXIT_TIMEOUT,
    EXIT_TRANSFER_IO,
    exit_code_for,
)
from lib.redact import redactor_for  # noqa: E402
from lib.sftp_transport import (  # noqa: E402, I001
    AuthenticationException,
    DependencyMissingError,
    HostKeyRejectedError,
    SftpSession,
    SSHException,
)
from lib.transfer import (  # noqa: E402
    have,
    paramiko_available,
    run_rsync,
    run_tar_stream,
)

DEFAULT_BLOCK_SIZE = 131072

FAIL_NETWORK_SRC = "network_src"
FAIL_NETWORK_DST = "network_dst"
FAIL_AUTH_SRC = "auth_src"
FAIL_AUTH_DST = "auth_dst"
FAIL_SRC_PATH_MISSING = "src_path_missing"
FAIL_SRC_IS_DIRECTORY = "src_is_directory"
FAIL_DST_PATH_INVALID = "dst_path_invalid"
FAIL_TRANSFER_IO = "transfer_io"
FAIL_TIMEOUT = "timeout"
FAIL_HOST_KEY = "host_key"
FAIL_DEPENDENCY_MISSING = "dependency_missing"
FAIL_REMOTE_ERROR = "remote_error"

_HUMAN_HINTS: dict[str, str] = {
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
    FAIL_HOST_KEY: (
        "a host key is unknown or changed: {detail}. Verify the fingerprint, "
        "then rerun with --accept-new-hostkey."
    ),
    FAIL_DEPENDENCY_MISSING: "no usable transfer backend: {detail}",
    FAIL_REMOTE_ERROR: "unexpected error: {detail}",
}


class _TransferIOError(OSError):
    """An OSError that happened *after* the destination file was opened."""


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_server_transfer.py",
        description=(
            "Move bytes between two remote hosts. stream = SFTP through this "
            "host (needs paramiko); direct = tar/rsync between the two hosts "
            "(no paramiko); auto picks stream when paramiko is available."
        ),
    )
    p.add_argument("src_alias")
    p.add_argument("src_path")
    p.add_argument("dst_alias")
    p.add_argument("dst_path")
    p.add_argument(
        "--mode", choices=["auto", "direct", "stream", "hybrid"], default="auto",
        help="Transfer mode (default: auto)",
    )
    p.add_argument(
        "--use-rsync", action="store_true",
        help=(
            "Use rsync for the direct path (incremental). rsync must be "
            "installed on the SOURCE host: both endpoints are remote, so it "
            "cannot run from this machine"
        ),
    )
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--bwlimit", type=int, default=None, help="KB/s cap (1 KB = 1024 B/s)")
    p.add_argument("--ssh-config", default=None)
    p.add_argument("--accept-new-hostkey", action="store_true")
    p.add_argument(
        "--auth", choices=AUTH_CHOICES, default="auto",
        help=(
            "Authentication routing for the direct (tar/rsync) backends: "
            "'auto' reads '# auth: password' metadata for BOTH aliases, "
            "'password' answers the prompt through SSH_ASKPASS. Without it a "
            "password-only host could not copy server-to-server (SSHCORE-026)"
        ),
    )
    p.add_argument(
        "--password-env", default=DEFAULT_PASSWORD_ENV,
        help=f"Environment variable holding the password (default: {DEFAULT_PASSWORD_ENV})",
    )
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument(
        "--redact", dest="redact", action="store_true", default=True,
        help="Hide host identifiers in the envelope (default: on)",
    )
    p.add_argument("--no-redact", dest="redact", action="store_false", help=argparse.SUPPRESS)
    audit_cli_flags(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", "-V", action="store_true")
    return p


def _format_hint(fclass: str, args: argparse.Namespace, **fmt_args: Any) -> str:
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
    transport: str,
    bytes_total: int,
    files: int,
    elapsed: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "mode_used": mode_used,
        "transport": transport,
        "bytes": bytes_total,
        "files_transferred": files,
        "elapsed_s": round(elapsed, 3),
        "src": f"{args.src_alias}:{args.src_path}",
        "dst": f"{args.dst_alias}:{args.dst_path}",
        "failure_class": None,
    }
    if args.mode == "auto":
        data["auto_resolved_to"] = mode_used
    elif args.mode == "hybrid":
        data["hybrid_resolved_to"] = mode_used
    if extra:
        data.update(extra)
    human = (
        f"transferred {files} file(s), {bytes_total} bytes in "
        f"{round(elapsed, 3)}s via {mode_used} "
        f"({args.src_alias}:{args.src_path} -> {args.dst_alias}:{args.dst_path})\n"
    )
    return json_result(success=True, exit_code=EXIT_OK, stdout=human, stderr="", data=data)


def _failure_envelope(
    args: argparse.Namespace,
    fclass: str,
    *,
    partial_bytes: int = 0,
    partial_files: int = 0,
    elapsed: float = 0.0,
    detail: str = "",
    mode_used: str | None = None,
    transport: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "mode_used": mode_used,
        "transport": transport,
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
        exit_code=exit_code_for(fclass, default=EXIT_INTERNAL),
        stdout="",
        stderr=stderr,
        data=data,
    )


def _classify_paramiko_failure(exc: BaseException, *, which_side: str) -> str:
    if isinstance(exc, HostKeyRejectedError):
        return FAIL_HOST_KEY
    if isinstance(exc, AuthenticationException):
        return FAIL_AUTH_SRC if which_side == "src" else FAIL_AUTH_DST
    if isinstance(exc, (socket.gaierror, ConnectionRefusedError, socket.timeout)):
        return FAIL_NETWORK_SRC if which_side == "src" else FAIL_NETWORK_DST
    if isinstance(exc, SSHException):
        return FAIL_AUTH_SRC if which_side == "src" else FAIL_AUTH_DST
    if isinstance(exc, OSError):
        return FAIL_NETWORK_SRC if which_side == "src" else FAIL_NETWORK_DST
    return FAIL_REMOTE_ERROR


# --- stream (SFTP) path -----------------------------------------------------


def _stat_or_none(session: SftpSession, path: str) -> Any:
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
    parent = posixpath.dirname(path)
    if parent and parent not in ("/", ""):
        session.mkdir_p(parent)


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
    assert src_session.sftp is not None
    assert dst_session.sftp is not None
    src_attr = src_session.sftp.stat(src_path)
    _ = int(src_attr.st_size or 0)
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
                    src_session, src_p, dst_session, dst_p,
                    block_size=block_size, bwlimit_kbs=bwlimit_kbs,
                    start_time=start_time, running_bytes=total_bytes,
                )
                files += 1
                total_bytes += bytes_done
                state["bytes_done"] = total_bytes
                state["files_done"] = files
    return files, total_bytes


def _run_stream(args: argparse.Namespace) -> dict[str, Any]:
    start_time = time.monotonic()
    state: dict[str, int] = {"bytes_done": 0, "files_done": 0}
    try:
        with contextlib.ExitStack() as stack:
            try:
                src_session = stack.enter_context(
                    SftpSession(
                        args.src_alias,
                        ssh_config=args.ssh_config,
                        accept_new_hostkey=args.accept_new_hostkey,
                    )
                )
            except Exception as e:  # noqa: BLE001 — paramiko surface is broad
                fclass = (
                    FAIL_DEPENDENCY_MISSING
                    if isinstance(e, DependencyMissingError)
                    else _classify_paramiko_failure(e, which_side="src")
                )
                return _failure_envelope(
                    args, fclass, elapsed=time.monotonic() - start_time,
                    detail=str(e), mode_used="stream", transport="sftp",
                )
            try:
                dst_session = stack.enter_context(
                    SftpSession(
                        args.dst_alias,
                        ssh_config=args.ssh_config,
                        accept_new_hostkey=args.accept_new_hostkey,
                    )
                )
            except Exception as e:  # noqa: BLE001
                fclass = (
                    FAIL_DEPENDENCY_MISSING
                    if isinstance(e, DependencyMissingError)
                    else _classify_paramiko_failure(e, which_side="dst")
                )
                return _failure_envelope(
                    args, fclass, elapsed=time.monotonic() - start_time,
                    detail=str(e), mode_used="stream", transport="sftp",
                )
            try:
                src_attr = _stat_or_none(src_session, args.src_path)
            except OSError as e:
                return _failure_envelope(
                    args, FAIL_REMOTE_ERROR, elapsed=time.monotonic() - start_time,
                    detail=str(e), mode_used="stream", transport="sftp",
                )
            if src_attr is None:
                return _failure_envelope(
                    args, FAIL_SRC_PATH_MISSING,
                    elapsed=time.monotonic() - start_time,
                    mode_used="stream", transport="sftp",
                )
            is_dir = _stat.S_ISDIR(int(src_attr.st_mode or 0))
            if is_dir and not args.recursive:
                return _failure_envelope(
                    args, FAIL_SRC_IS_DIRECTORY,
                    elapsed=time.monotonic() - start_time,
                    mode_used="stream", transport="sftp",
                )
            try:
                if is_dir:
                    files, bytes_total = _pipe_tree(
                        src_session, args.src_path, dst_session, args.dst_path,
                        block_size=DEFAULT_BLOCK_SIZE, bwlimit_kbs=args.bwlimit,
                        state=state,
                    )
                else:
                    bytes_done = _pipe_file(
                        src_session, args.src_path, dst_session, args.dst_path,
                        block_size=DEFAULT_BLOCK_SIZE, bwlimit_kbs=args.bwlimit,
                        start_time=start_time, running_bytes=0,
                    )
                    state["bytes_done"] = bytes_done
                    state["files_done"] = 1
                    files, bytes_total = 1, bytes_done
            except _TransferIOError as e:
                return _failure_envelope(
                    args, FAIL_TRANSFER_IO,
                    partial_bytes=state["bytes_done"],
                    partial_files=state["files_done"],
                    elapsed=time.monotonic() - start_time,
                    detail=str(e.__cause__ or e),
                    mode_used="stream", transport="sftp",
                )
            except OSError as e:
                return _failure_envelope(
                    args, FAIL_DST_PATH_INVALID,
                    partial_bytes=state["bytes_done"],
                    partial_files=state["files_done"],
                    elapsed=time.monotonic() - start_time,
                    detail=str(e), mode_used="stream", transport="sftp",
                )
            return _success_envelope(
                args, "stream", "sftp", bytes_total, files,
                time.monotonic() - start_time,
            )
    except Exception as e:  # noqa: BLE001 — catch-all for unexpected errors
        return _failure_envelope(
            args, FAIL_REMOTE_ERROR, elapsed=time.monotonic() - start_time,
            detail=str(e), mode_used="stream", transport="sftp",
        )


# --- direct / hybrid --------------------------------------------------------


def _run_direct(args: argparse.Namespace, password: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    if args.use_rsync:
        # FA-04: rsync runs *on the source host*, because rsync refuses a remote
        # source and a remote destination in one command. Only `ssh` has to
        # exist here; `rsync` has to exist on the source.
        if not have("ssh"):
            return _failure_envelope(
                args, FAIL_DEPENDENCY_MISSING,
                detail=(
                    "rsync must run on the source host, which needs an ssh "
                    "client here; ssh is not on PATH"
                ),
                mode_used="direct", transport="rsync",
            )
        outcome = run_rsync(
            args.src_alias, args.src_path, args.dst_alias, args.dst_path,
            recursive=args.recursive, bwlimit_kbs=args.bwlimit,
            timeout=args.timeout, ssh_config=args.ssh_config,
            accept_new_hostkey=args.accept_new_hostkey,
            password=password,
        )
    else:
        if not have("ssh"):
            return _failure_envelope(
                args, FAIL_DEPENDENCY_MISSING, detail="ssh is not on PATH",
                mode_used="direct", transport="tar",
            )
        outcome = run_tar_stream(
            args.src_alias, args.src_path, args.dst_alias, args.dst_path,
            recursive=args.recursive, timeout=args.timeout,
            bwlimit_kbs=args.bwlimit, ssh_config=args.ssh_config,
            accept_new_hostkey=args.accept_new_hostkey,
            password=password,
        )
    elapsed = outcome.elapsed_s or (time.monotonic() - started)
    if not outcome.success:
        return _failure_envelope(
            args, outcome.failure_class or FAIL_TRANSFER_IO,
            elapsed=elapsed, detail=outcome.stderr,
            mode_used="direct", transport=outcome.transport,
        )
    files = 1
    return _success_envelope(
        args, "direct", outcome.transport, outcome.bytes_transferred, files,
        elapsed, extra={"detail": outcome.detail} if outcome.detail else None,
    )


def _route(args: argparse.Namespace) -> tuple[str, dict[str, Any] | None]:
    """Decide the effective mode. Returns ``(mode, early_failure)``."""
    if args.mode == "stream":
        if not paramiko_available():
            return "stream", _failure_envelope(
                args, FAIL_DEPENDENCY_MISSING,
                detail=(
                    "stream mode pipes bytes through SFTP, which needs "
                    "paramiko. Install it, or use --mode direct/auto."
                ),
                mode_used="stream", transport="sftp",
            )
        return "stream", None
    if args.mode == "direct":
        return "direct", None
    if args.mode == "hybrid":
        # direct first (no paramiko, no local disk); stream is the backstop.
        return "direct", None
    # auto
    if args.use_rsync:
        return "direct", None
    if paramiko_available():
        return "stream", None
    return "direct", None


def _audit(args: argparse.Namespace, result: dict) -> None:
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    data = result.get("data") or {}
    append_audit(
        args.src_alias,
        {
            "host": args.src_alias,
            "dst_host": args.dst_alias,
            "route": "server_transfer",
            "mode": data.get("mode_used"),
            "transport": data.get("transport"),
            "src": f"{args.src_alias}:{args.src_path}",
            "dst": f"{args.dst_alias}:{args.dst_path}",
            "bytes": data.get("bytes", 0),
            "success": result.get("success"),
            "exit_code": result.get("exit_code"),
            "failure_class": data.get("failure_class"),
            "elapsed_s": data.get("elapsed_s"),
        },
    )


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_server_transfer.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    mode, early_failure = _route(args)
    if early_failure is not None:
        emit(early_failure, as_json=args.json)
        return int(early_failure["exit_code"])

    # SSHCORE-026 follow-up: the direct (tar/rsync) path needs a credential on
    # BOTH hops; --auth auto reads the metadata, --auth password pins it. A
    # missing secret is `password_required` (105) before any spawn.
    auth_method, password = resolve_auth(
        args.src_alias, args.auth, args.ssh_config, args.password_env
    )
    if auth_method == "key":
        dst_method, dst_password = resolve_auth(
            args.dst_alias, args.auth, args.ssh_config, args.password_env
        )
        if dst_method == "password":
            auth_method, password = dst_method, dst_password
    if auth_method == "password" and not password:
        result = _failure_envelope(
            args, "password_required",
            detail=(
                f"{args.password_env} is not set; the direct transport has to "
                "authenticate both hops"
            ),
            mode_used=mode, transport="tar" if mode != "stream" else "sftp",
        )
        result["data"]["auth"] = auth_method
        emit(result, as_json=args.json, redactor=redactor_for(args.src_alias, args.ssh_config))
        return int(result["exit_code"])

    if mode == "stream":
        result = _run_stream(args)
    else:
        result = _run_direct(args, password)
        if (
            args.mode == "hybrid"
            and not result.get("success")
            and (result.get("data") or {}).get("failure_class") == FAIL_DEPENDENCY_MISSING
            and paramiko_available()
        ):
            fallback = _run_stream(args)
            fallback.setdefault("data", {})["hybrid_resolved_to"] = "stream"
            fallback["data"]["hybrid_fallback_reason"] = "direct transport unavailable"
            result = fallback
    result.setdefault("data", {})["auth"] = auth_method

    _audit(args, result)
    redactor = redactor_for(args.src_alias, args.ssh_config, enabled=args.redact)
    emit(result, as_json=args.json, redactor=redactor)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
