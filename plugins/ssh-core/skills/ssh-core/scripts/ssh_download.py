#!/usr/bin/env python3
"""ssh_download.py — remote → local copy (SFTP, or scp when paramiko is absent).

v1.2 surface::

    ssh_download.py <alias> <remote-path> <local-path>
        [--transport auto|sftp|scp]   # default: auto (SSH-11)
        [--resume]                    # SFTP only
        [--recursive]
        [--preserve]                  # mtime + mode, like cp -p (DV-19)
        [--delete --yes]              # mirror: prune extra local entries
        [--no-progress]
        [--bwlimit KB/s]
        [--block-size 131072]
        [--retry N] [--retry-delay S] [--retry-backoff F]   # DV-17
        [--ssh-config PATH] [--port N] [--identity PATH]
        [--accept-new-hostkey]
        [--audit-log|--no-audit] [--redact|--no-redact]
        [--json] [--version]

``auto`` uses paramiko/SFTP when importable and falls back to remote
``tar -cf -`` piped into a local ``tar -xf -`` when it is not. Downloading
with scp alone cannot preserve a tree, so the fallback for ``--recursive`` is
the tar stream; for a single file it is ``scp -O``.

Integrity: the sha256 of the local file is compared with the remote
``sha256sum``/``shasum`` after the copy (both transports), so a silent
truncation is caught rather than reported as success.

Metadata and mirroring (DV-19): ``--preserve`` copies the remote file's mtime
and permission bits onto the local copy, and ``--delete --yes`` turns a
recursive download into a mirror by removing local entries that no longer
exist on the remote side.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402
    CHILD_TEXT_KWARGS,
    append_audit,
    audit_cli_flags,
    audit_enabled,
    emit,
    json_result,
    msys_safe_env,
)
from lib.auth import AUTH_CHOICES, DEFAULT_PASSWORD_ENV  # noqa: E402
from lib.auth import auth_options  # noqa: E402
from lib.auth import resolve as resolve_auth  # noqa: E402
from lib.askpass import askpass_env  # noqa: E402
from lib.cli import SSHArgumentParser, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_DEPENDENCY_MISSING,
    EXIT_HOST_KEY,
    EXIT_INVALID_ARGUMENTS,
    EXIT_LOCAL_PATH_IS_DIRECTORY,
    EXIT_MODE_UNSUPPORTED,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_REMOTE_PATH_INVALID,
    EXIT_REMOTE_PATH_MISSING,
    EXIT_TRANSFER_IO,
    EXIT_VERIFY_FAILED,
    exit_code_for,
)
from lib.redact import redactor_for  # noqa: E402
from lib.retry import cli_flags as retry_cli_flags  # noqa: E402
from lib.retry import policy_from_args, retrying  # noqa: E402
from lib.sftp_transport import (  # noqa: E402
    AuthenticationException,
    DependencyMissingError,
    HostKeyRejectedError,
    SftpSession,
    SSHException,
    TransferResult,
    download_file,
    download_tree,
)
from lib.transfer import (  # noqa: E402
    paramiko_available,
    remote_sha256,
    run_scp,
    sha256_file,
)

DEFAULT_BLOCK_SIZE = 131072


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_download.py",
        description=(
            "Copy a file or tree from a remote alias to this host. Uses SFTP "
            "when paramiko is installed and falls back to scp/tar otherwise."
        ),
    )
    p.add_argument("alias")
    p.add_argument("remote_path")
    p.add_argument("local_path")
    p.add_argument(
        "--transport", choices=("auto", "sftp", "scp", "openssh"), default="auto",
        help=(
            "Transfer backend: sftp/paramiko (resume, bwlimit, verify), or "
            "scp/openssh (the OpenSSH client itself, no paramiko needed). "
            "'openssh' is an explicit alias for the scp backend; "
            "'auto' picks sftp when paramiko is importable and scp otherwise "
            "(SSHCORE-026)"
        ),
    )
    p.add_argument("--resume", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument(
        "--preserve", action="store_true",
        help=(
            "Apply the remote file's mtime and permission bits to the local "
            "copy (cp -p semantics); SFTP only, reported in data.preserved "
            "(DV-19)"
        ),
    )
    p.add_argument(
        "--delete", action="store_true",
        help=(
            "MIRROR mode: remove local entries that no longer exist remotely. "
            "Destructive — requires --yes and only runs after a clean copy "
            "(DV-19)"
        ),
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Confirm the destructive --delete mirror (no prompt is ever shown)",
    )
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--bwlimit", type=int, default=None)
    p.add_argument(
        "--block-size", type=int, default=DEFAULT_BLOCK_SIZE,
        help=f"SFTP block size in bytes (default: {DEFAULT_BLOCK_SIZE})",
    )
    p.add_argument("--ssh-config", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--identity", default=None)
    p.add_argument("--accept-new-hostkey", action="store_true")
    p.add_argument(
        "--auth", choices=AUTH_CHOICES, default="auto",
        help=(
            "Authentication routing for the scp/tar backends: 'auto' reads "
            "# auth: password from --ssh-config, 'password' answers the prompt "
            "through SSH_ASKPASS. Without it a password-only host could not "
            "transfer at all without paramiko (SSHCORE-026)"
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
    retry_cli_flags(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", "-V", action="store_true")
    return p


def _failure(
    args: argparse.Namespace,
    *,
    failure_class: str,
    stderr: str,
    transport: str = "sftp",
    exit_code: int | None = None,
) -> dict[str, Any]:
    return json_result(
        success=False,
        exit_code=exit_code if exit_code is not None else exit_code_for(failure_class),
        stderr=stderr,
        data={
            "route": transport,
            "direction": "download",
            "transport": transport,
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


def _sftp_data(args: argparse.Namespace, tr: TransferResult) -> dict[str, Any]:
    data: dict[str, Any] = {
        "route": "sftp",
        "direction": "download",
        "transport": "sftp",
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
    if getattr(args, "preserve", False):
        data["preserve_requested"] = True
        data["preserved"] = tr.preserved
        if tr.preserved is False and tr.stderr:
            data["preserve_note"] = tr.stderr.strip()
    if getattr(args, "delete", False):
        data["mirror"] = True
        data["deleted_files"] = tr.deleted_files or []
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


def _audit(args: argparse.Namespace, result: dict) -> None:
    if not audit_enabled(getattr(args, "audit", None), default=False):
        return
    data = result.get("data") or {}
    append_audit(
        args.alias,
        {
            "host": args.alias,
            "route": "transfer",
            "direction": "download",
            "transport": data.get("transport"),
            "local_path": args.local_path,
            "remote_path": args.remote_path,
            "bytes": data.get("bytes_transferred", 0),
            "success": result.get("success"),
            "exit_code": result.get("exit_code"),
            "failure_class": data.get("failure_class"),
            "elapsed_s": data.get("elapsed_s"),
        },
    )


def _run_sftp(args: argparse.Namespace) -> dict[str, Any]:
    try:
        with SftpSession(
            args.alias,
            ssh_config=args.ssh_config,
            accept_new_hostkey=args.accept_new_hostkey,
            port=args.port,
            identity=args.identity,
        ) as session:
            if args.recursive:
                tr = download_tree(
                    session, args.remote_path, args.local_path,
                    resume=args.resume, block_size=args.block_size,
                    bwlimit_kbs=args.bwlimit, progress=not args.no_progress,
                    preserve=getattr(args, "preserve", False),
                    delete=getattr(args, "delete", False),
                )
            else:
                tr = download_file(
                    session, args.remote_path, args.local_path,
                    resume=args.resume, block_size=args.block_size,
                    bwlimit_kbs=args.bwlimit, progress=not args.no_progress,
                    preserve=getattr(args, "preserve", False),
                )
    except DependencyMissingError as e:
        return _failure(args, failure_class=e.failure_class, stderr=str(e))
    except HostKeyRejectedError as e:
        return _failure(args, failure_class="host_key", stderr=str(e), exit_code=EXIT_HOST_KEY)
    except AuthenticationException as e:
        return _failure(
            args, failure_class="auth",
            stderr=f"ssh authentication failed for alias '{args.alias}': {e}",
            exit_code=EXIT_AUTH,
        )
    except SSHException as e:
        return _failure(
            args, failure_class="network",
            stderr=f"ssh transport error to '{args.alias}': {e}",
            exit_code=EXIT_NETWORK,
        )
    except PermissionError as e:
        return _failure(
            args, failure_class="permission",
            stderr=f"permission denied (remote read or local write): {e}",
            exit_code=EXIT_PERMISSION,
        )
    except FileNotFoundError as e:
        return _failure(
            args, failure_class="remote_path_missing",
            stderr=f"remote path not found: {args.remote_path} ({e})",
            exit_code=EXIT_REMOTE_PATH_MISSING,
        )
    except OSError as e:
        return _failure(
            args, failure_class="transfer_io",
            stderr=f"sftp i/o error: {e}", exit_code=EXIT_TRANSFER_IO,
        )

    if tr.success:
            return json_result(
                success=True, exit_code=EXIT_OK, stderr=tr.stderr,
                data=_sftp_data(args, tr),
            )
    return json_result(
        success=False,
        exit_code=tr.exit_code if tr.exit_code else exit_code_for(tr.failure_class),
        stderr=tr.stderr,
        data=_sftp_data(args, tr),
    )


def _run_scp_file(args: argparse.Namespace, password: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    local = Path(args.local_path)
    preserve = bool(getattr(args, "preserve", False))
    outcome = run_scp(
        f"{args.alias}:{args.remote_path}",
        args.local_path,
        timeout=args.timeout,
        port=args.port,
        identity=args.identity,
        ssh_config=args.ssh_config,
        accept_new_hostkey=args.accept_new_hostkey,
        preserve=preserve,
        local_path=None,  # verification below, direction reversed
        delete_local_on_failure=True,
        password=password,
    )
    verified = False
    detail = ""
    if outcome.success and local.is_file():
        local_hash = sha256_file(local)
        remote_hash = remote_sha256(
            args.alias, args.remote_path, password=password,
            ssh_config=args.ssh_config,
            accept_new_hostkey=args.accept_new_hostkey,
        )
        if local_hash and remote_hash:
            verified = local_hash == remote_hash
            if not verified:
                return json_result(
                    success=False,
                    exit_code=EXIT_VERIFY_FAILED,
                    stderr=(
                        "sha256 mismatch after scp: "
                        f"local={local_hash} remote={remote_hash}"
                    ),
                    data={
                        "route": "scp", "direction": "download", "transport": "scp",
                        "alias": args.alias, "remote_path": args.remote_path,
                        "local_path": args.local_path,
                        "bytes_transferred": local.stat().st_size,
                        "sha256_local": local_hash, "sha256_remote": remote_hash,
                        "sha256_verified": False, "failure_class": "integrity_mismatch",
                    },
                )
        else:
            detail = "sha256 verification unavailable (no sha256sum/shasum remotely)"
    size = local.stat().st_size if outcome.success and local.is_file() else 0
    data = {
        "route": "scp",
        "direction": "download",
        "transport": "scp",
        "alias": args.alias,
        "remote_path": args.remote_path,
        "local_path": args.local_path,
        "recursive": False,
        "resume": False,
        "bytes_transferred": size,
        "elapsed_s": round(outcome.elapsed_s or (time.monotonic() - started), 3),
        "sha256_verified": verified,
        "failure_class": outcome.failure_class,
        "scp_argv": outcome.argv,
        "transport_note": (
            "scp fallback: paramiko is not installed. --resume/--bwlimit/"
            "--block-size only apply to the sftp transport; --preserve maps to "
            "`scp -p`, and --delete is refused (it needs the sftp transport)."
        ),
    }
    if preserve:
        # FA-05: `scp -p` really does keep mtime/mode.
        data["preserve_requested"] = True
        data["preserved"] = outcome.preserved
    if detail:
        data["detail"] = detail
    return json_result(
        success=outcome.success,
        exit_code=EXIT_OK if outcome.success else exit_code_for(outcome.failure_class),
        stderr="" if outcome.success else outcome.stderr,
        data=data,
    )


def _run_tar_tree(
    args: argparse.Namespace, password: str | None = None
) -> dict[str, Any]:
    """Recursive download without paramiko: remote ``tar`` piped into local tar."""
    started = time.monotonic()
    local = Path(args.local_path).expanduser()
    # FA-03d: `mkdir(parents=True)` raises FileExistsError when the target is an
    # existing *file*, and that escaped as a traceback. Say what is wrong.
    if local.exists() and not local.is_dir():
        return _failure(
            args, failure_class="local_path_is_directory",
            stderr=(
                f"local '{args.local_path}' exists and is not a directory; "
                "--recursive needs a destination directory"
            ),
            transport="tar",
            exit_code=EXIT_LOCAL_PATH_IS_DIRECTORY,
        )
    try:
        local.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failure(
            args, failure_class="transfer_io",
            stderr=f"cannot create local directory {args.local_path}: {exc}",
            transport="tar",
            exit_code=EXIT_TRANSFER_IO,
        )
    ssh_base = ["ssh", *auth_options(bool(password)), "-o", "ConnectTimeout=8"]
    if args.accept_new_hostkey:
        ssh_base += ["-o", "StrictHostKeyChecking=accept-new"]
    elif password:
        ssh_base += ["-o", "StrictHostKeyChecking=yes"]
    if args.ssh_config:
        ssh_base += ["-F", args.ssh_config]
    if args.port is not None:
        ssh_base += ["-p", str(args.port)]
    if args.identity:
        ssh_base += ["-i", str(Path(args.identity).expanduser())]
    quoted = "'" + args.remote_path.replace("'", "'\\''") + "'"
    # -C <parent> + the bare leaf name: extracting "/etc/ssh" verbatim would
    # recreate the whole absolute path under local_path.
    trimmed = args.remote_path.rstrip("/")
    parent, _, leaf = trimmed.rpartition("/")
    producer = [
        *ssh_base, "--", args.alias,
        f"tar -C '{parent or '/'}' -cf - '{leaf or trimmed}'",
    ]
    consumer = ["tar", "-xf", "-", "-C", str(local)]
    env = msys_safe_env()
    ctx = None
    if password:
        ctx = askpass_env(password)
        env = ctx.__enter__()
    try:
        src = subprocess.Popen(
            producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        dst = subprocess.Popen(
            consumer, stdin=src.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=msys_safe_env(),
        )
        if src.stdout is not None:
            src.stdout.close()
        assert dst.stderr is not None
        dst_err = dst.stderr.read().decode("utf-8", "replace")
        assert src.stderr is not None
        src_err = src.stderr.read().decode("utf-8", "replace")
        dst_rc = dst.wait(timeout=args.timeout)
        src_rc = src.wait(timeout=args.timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return _failure(
            args, failure_class="transfer_io",
            stderr=f"tar stream failed: {exc}", transport="tar",
            exit_code=EXIT_TRANSFER_IO,
        )
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    elapsed = round(time.monotonic() - started, 3)
    stderr = (src_err + dst_err).strip()
    if src_rc != 0 or dst_rc != 0:
        return _failure(
            args,
            failure_class="remote_path_missing" if "No such file" in stderr else "transfer_io",
            stderr=stderr or f"tar stream failed (src={src_rc}, dst={dst_rc})",
            transport="tar",
            exit_code=EXIT_REMOTE_PATH_MISSING if "No such file" in stderr else EXIT_TRANSFER_IO,
        )
    return json_result(
        success=True,
        exit_code=EXIT_OK,
        data={
            "route": "tar",
            "direction": "download",
            "transport": "tar",
            "alias": args.alias,
            "remote_path": args.remote_path,
            "local_path": args.local_path,
            "recursive": True,
            "resume": False,
            "bytes_transferred": 0,
            "elapsed_s": elapsed,
            "sha256_verified": False,
            "failure_class": None,
            "transport_note": (
                "tar-over-ssh fallback: paramiko is not installed. Per-file "
                "verification is not available in this mode."
            ),
        },
    )


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_download.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    local_path = Path(args.local_path)
    if local_path.is_dir() and not args.recursive:
        result = _failure(
            args, failure_class="local_path_is_directory",
            stderr=(
                f"local '{args.local_path}' is a directory; pass --recursive "
                "or specify a file path"
            ),
            exit_code=exit_code_for("local_path_is_directory"),
        )
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    if args.delete and not args.yes:
        result = _failure(
            args, failure_class="confirmation_required",
            stderr=(
                "ssh_download: --delete mirrors the source and DELETES local "
                "files that no longer exist remotely. Re-run with --yes if "
                "that is what you want, or drop --delete for a plain copy.\n"
            ),
            exit_code=exit_code_for("confirmation_required"),
        )
        result["data"]["requires"] = "--yes"
        result["data"]["mirror"] = True
        emit(result, as_json=args.json)
        return int(result["exit_code"])
    if args.delete and not args.recursive:
        result = _failure(
            args, failure_class="invalid_arguments",
            stderr=(
                "ssh_download: --delete only makes sense with --recursive "
                "(it mirrors a tree, not a single file).\n"
            ),
            exit_code=EXIT_INVALID_ARGUMENTS,
        )
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    use_sftp = args.transport == "sftp" or (
        args.transport == "auto" and paramiko_available()
    )
    if args.transport == "sftp" and not paramiko_available():
        result = _failure(
            args, failure_class="dependency_missing",
            stderr=(
                "ssh_download: --transport sftp requires paramiko. Install it "
                "or use --transport auto/scp."
            ),
            exit_code=EXIT_DEPENDENCY_MISSING,
        )
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    # SSHCORE-026: the OpenSSH client is a first-class backend. Asking for
    # features it cannot provide (resume/bwlimit) used to be silently ignored,
    # which is the same class of lie FA-05 removed for --delete.
    if not use_sftp:
        deferred = [
            flag for flag in (
                ("--resume", args.resume),
                ("--bwlimit", args.bwlimit is not None),
            ) if flag[1]
        ]
        if deferred:
            names = ", ".join(name for name, _ in deferred)
            result = _failure(
                args, failure_class="mode_deferred",
                stderr=(
                    f"ssh_download: {names} need the sftp transport "
                    "(paramiko), which is not available here. Use "
                    "--transport auto with paramiko installed, or drop the "
                    "flag for a plain scp copy (SSHCORE-026).\n"
                ),
                transport="scp",
                exit_code=EXIT_MODE_UNSUPPORTED,
            )
            result["data"]["deferred_transport"] = "sftp"
            result["data"]["deferred_flags"] = [name for name, _ in deferred]
            emit(result, as_json=args.json)
            return int(result["exit_code"])

    if not use_sftp and args.delete:
        # FA-05: neither tar nor scp can prune, and the tar branch did not even
        # emit `mirror`/`deleted_files`, so `--delete --yes` returned
        # `success: true` while the stale local entries stayed. Refuse instead.
        result = _failure(
            args, failure_class="mode_deferred",
            stderr=(
                "ssh_download: --delete needs the sftp transport (paramiko), "
                "which is not installed here. Run `pip install "
                "'paramiko>=3.0,<4'`, or drop --delete for a plain copy: tar "
                "and scp cannot prune, and reporting success without pruning "
                "would be a lie.\n"
            ),
            transport="sftp" if args.transport == "sftp" else "tar",
            exit_code=EXIT_MODE_UNSUPPORTED,
        )
        result["data"]["mirror"] = True
        result["data"]["deferred_transport"] = "sftp"
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    # SSHCORE-026 follow-up: the scp/tar backends now speak the same auth
    # profiles as exec/tunnel/daemon, so a password-only host can transfer
    # without paramiko. Resolved *before* spawning: a missing secret is
    # `password_required` (105), never a misleading "Permission denied".
    auth_method, password = resolve_auth(
        args.alias, args.auth, args.ssh_config, args.password_env
    )
    if auth_method == "password" and not password:
        result = _failure(
            args, failure_class="password_required",
            stderr=(
                f"ssh_download: {args.alias} needs password auth but "
                f"${args.password_env} is not set. Export it (never pass "
                "passwords on the command line), or mark the host "
                "`# auth: key` if a key is installed.\n"
            ),
            transport="sftp" if use_sftp else "scp",
            exit_code=exit_code_for("password_required"),
        )
        result["data"]["auth"] = auth_method
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    if use_sftp:
        def _attempt(_n: int) -> dict[str, Any]:
            return _run_sftp(args)
    elif args.recursive:
        def _attempt(_n: int) -> dict[str, Any]:
            return _run_tar_tree(args, password)
    else:
        def _attempt(_n: int) -> dict[str, Any]:
            return _run_scp_file(args, password)
    policy = policy_from_args(args)
    result = retrying(_attempt, policy) if policy.enabled else _attempt(1)
    result.setdefault("data", {})["auth"] = auth_method

    _audit(args, result)
    redactor = redactor_for(args.alias, args.ssh_config, enabled=args.redact)
    emit(result, as_json=args.json, redactor=redactor)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
