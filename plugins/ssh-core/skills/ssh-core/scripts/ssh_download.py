#!/usr/bin/env python3
"""ssh_download.py — remote → local, SFTP with resume.

v1.0 surface:
    ssh_download.py <alias> <remote-path> <local-path>
        [--resume]                # SFTP partial + offset retry
        [--recursive]
        [--no-progress]
        [--bwlimit KB/s]
        [--block-size 131072]
        [--json]

Implementation notes:
    - Default 128 KB block size, configurable via --block-size.
    - Progress callback prints every 5% to stderr unless --no-progress.
    - --resume:
        1. stat local target
        2. If exists and size < remote, seek local to local size,
           continue reading remote from local_size
        3. After EOF, compare sha256 of both ends; mismatch → exit 9.
    - Errors are mapped per design.md §5 to the shared JSON envelope.
    - Mirror of ssh_upload.py with the direction inverted (no --chmod —
      local file modes are governed by the user's umask).
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
    download_file,
    download_tree,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssh_download.py")
    p.add_argument("alias")
    p.add_argument("remote_path")
    p.add_argument("local_path")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--no-progress", action="store_true")
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
            "direction": "download",
            "alias": args.alias,
            "remote_path": args.remote_path,
            "local_path": args.local_path,
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
        "direction": "download",
        "alias": args.alias,
        "remote_path": args.remote_path,
        "local_path": args.local_path,
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


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    args = build_parser().parse_args(argv)

    # ------------------------------------------------------------------
    # Pre-flight guard (local-only, no network round-trip needed):
    # refuse single-file download into an existing local directory —
    # the user must either pass --recursive or specify a file path.
    # Mirror of ssh_upload's "local dir without --recursive" guard.
    # ------------------------------------------------------------------
    local_path = Path(args.local_path)
    if local_path.is_dir() and not args.recursive:
        return _emit_failure(
            args,
            exit_code=1,
            stderr=(
                f"local '{args.local_path}' is a directory; "
                "pass --recursive or specify a file path"
            ),
            failure_class="remote_error",
        )

    # ------------------------------------------------------------------
    # Open SFTP session and dispatch to download_file / download_tree.
    # Exceptions from paramiko are mapped to the shared envelope shape
    # per design.md §5.
    # ------------------------------------------------------------------
    try:
        with SftpSession(args.alias) as session:
            if args.recursive:
                tr = download_tree(
                    session,
                    args.remote_path,
                    args.local_path,
                    resume=args.resume,
                    block_size=args.block_size,
                    bwlimit_kbs=args.bwlimit,
                    progress=not args.no_progress,
                )
            else:
                tr = download_file(
                    session,
                    args.remote_path,
                    args.local_path,
                    resume=args.resume,
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
            stderr=(
                f"permission denied (remote read or local write): "
                f"{args.remote_path} → {args.local_path} ({e})"
            ),
            failure_class="permission",
        )
    except FileNotFoundError as e:
        return _emit_failure(
            args,
            exit_code=1,
            stderr=f"remote path not found: {args.remote_path} ({e})",
            failure_class="remote_error",
        )
    except OSError as e:
        # IOError is an alias for OSError on Python 3. Cover the
        # local-write-failed (partition full, etc.) edge case here.
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
