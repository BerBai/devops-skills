#!/usr/bin/env python3
"""ssh_upload.py — local → remote, SFTP with resume.

v1.0 surface:
    ssh_upload.py <alias> <local-path> <remote-path>
        [--resume]                # SFTP partial + offset retry
        [--recursive]
        [--no-progress]
        [--chmod 0644]
        [--bwlimit KB/s]
        [--block-size 131072]
        [--json]

Implementation notes:
    - Default 128 KB block size, configurable via --block-size.
    - Progress callback prints every 5% to stderr unless --no-progress.
    - --resume:
        1. SFTP stat remote target
        2. If exists and size < local, seek both files to remote size
        3. Continue write
        4. After EOF, compare sha256 of both ends; mismatch → exit 9.
    - All file-path argv goes through MSYS_NO_PATHCONV=1 (Windows safety).
    - Errors are mapped per design.md §7 to the shared JSON envelope.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import paramiko  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).parent))
from lib import emit, json_result  # noqa: E402
from lib.sftp_transport import (  # noqa: E402
    SftpSession,
    TransferResult,
    upload_file,
    upload_tree,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_upload.py")
    p.add_argument("alias")
    p.add_argument("local_path")
    p.add_argument("remote_path")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--chmod", default=None)
    p.add_argument("--bwlimit", type=int, default=None)
    p.add_argument("--block-size", type=int, default=131072)
    p.add_argument("--json", action="store_true")
    return p


def _emit_failure(
    args: argparse.Namespace,
    *,
    exit_code: int,
    stderr: str,
    failure_class: str | None,
) -> int:
    """Emit a failure envelope with the minimal required ``data`` shape."""
    result = json_result(
        success=False,
        exit_code=exit_code,
        stderr=stderr,
        data={
            "route": "sftp",
            "alias": args.alias,
            "local_path": args.local_path,
            "remote_path": args.remote_path,
            "recursive": args.recursive,
            "resume": args.resume,
            "bytes_transferred": 0,
            "elapsed_s": 0.0,
            "sha256_verified": False,
            "failure_class": failure_class,
        },
    )
    emit(result, as_json=args.json)
    return exit_code


def _build_data_block(args: argparse.Namespace, tr: TransferResult) -> dict[str, Any]:
    data: dict[str, Any] = {
        "route": "sftp",
        "alias": args.alias,
        "local_path": args.local_path,
        "remote_path": args.remote_path,
        "recursive": args.recursive,
        "resume": args.resume,
        "bytes_transferred": tr.bytes_transferred,
        "elapsed_s": tr.elapsed_s,
        "sha256_verified": tr.sha256_verified,
        "failure_class": tr.failure_class,
    }
    # sha256_local / sha256_remote only on mismatch (PRD R8).
    if (
        tr.sha256_local is not None
        and tr.sha256_remote is not None
        and tr.sha256_local != tr.sha256_remote
    ):
        data["sha256_local"] = tr.sha256_local
        data["sha256_remote"] = tr.sha256_remote
    if tr.bytes_resumed_from is not None:
        data["bytes_resumed_from"] = tr.bytes_resumed_from
    if tr.failed_files:
        data["failed_files"] = tr.failed_files
    return data


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912
    args = build_parser().parse_args(argv)

    # ------------------------------------------------------------------
    # Pre-flight guards (run before opening an SFTP session to fail fast
    # without a network round-trip on common usage errors).
    # ------------------------------------------------------------------
    local_path = Path(args.local_path)
    if not local_path.exists():
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"local path not found: {args.local_path}",
            failure_class="remote_error",
        )
    if local_path.is_dir() and not args.recursive:
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"'{args.local_path}' is a directory; pass --recursive",
            failure_class="remote_error",
        )

    # Parse --chmod from octal string (e.g. "0644") to int.
    chmod_int: int | None
    if args.chmod is not None:
        try:
            chmod_int = int(args.chmod, 8)
        except (TypeError, ValueError):
            return _emit_failure(
                args,
                exit_code=1,
                stderr=f"invalid --chmod (expected octal like 0644): {args.chmod}",
                failure_class="remote_error",
            )
    else:
        chmod_int = None

    # ------------------------------------------------------------------
    # Open SFTP session and dispatch to upload_file / upload_tree.
    # Exceptions from paramiko are mapped to the shared envelope shape
    # per design.md §7.
    # ------------------------------------------------------------------
    try:
        with SftpSession(args.alias) as session:
            if args.recursive:
                tr = upload_tree(
                    session,
                    args.local_path,
                    args.remote_path,
                    resume=args.resume,
                    chmod=chmod_int,
                    block_size=args.block_size,
                    bwlimit_kbs=args.bwlimit,
                    progress=not args.no_progress,
                )
            else:
                tr = upload_file(
                    session,
                    args.local_path,
                    args.remote_path,
                    resume=args.resume,
                    chmod=chmod_int,
                    block_size=args.block_size,
                    bwlimit_kbs=args.bwlimit,
                    progress=not args.no_progress,
                )
    except paramiko.AuthenticationException as e:
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"ssh authentication failed for alias '{args.alias}': {e}",
            failure_class="auth",
        )
    except paramiko.SSHException as e:
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"ssh transport error to '{args.alias}': {e}",
            failure_class="network",
        )
    except PermissionError as e:
        return _emit_failure(
            args,
            exit_code=8,
            stderr=f"remote write permission denied: {args.remote_path} ({e})",
            failure_class="permission",
        )
    except FileNotFoundError as e:
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"remote path not found: {args.remote_path} ({e})",
            failure_class="remote_error",
        )
    except NotImplementedError as e:
        # Catches upload_tree placeholder until Phase R lands.
        return _emit_failure(
            args,
            exit_code=1,
            stderr=str(e),
            failure_class="remote_error",
        )
    except OSError as e:
        # IOError is an alias for OSError on Python 3.
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"sftp i/o error: {e}",
            failure_class="remote_error",
        )

    result = json_result(
        success=tr.success,
        exit_code=tr.exit_code,
        stderr=tr.stderr,
        data=_build_data_block(args, tr),
    )
    emit(result, as_json=args.json)
    return tr.exit_code


if __name__ == "__main__":
    sys.exit(main())
