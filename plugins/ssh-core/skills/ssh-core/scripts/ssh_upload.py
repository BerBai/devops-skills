#!/usr/bin/env python3
"""ssh_upload.py — local → remote copy (SFTP, or scp when paramiko is absent).

v1.2 surface::

    ssh_upload.py <alias> <local-path> <remote-path>
        [--transport auto|sftp|scp]   # default: auto (SSH-11)
        [--resume]                    # SFTP only; implies --overwrite
        [--recursive]
        [--overwrite]                 # replace an existing remote file
        [--preserve]                  # mtime + mode, like cp -p (DV-19)
        [--delete --yes]              # mirror: prune extra remote entries
        [--no-progress]
        [--chmod 0644]                # SFTP only
        [--bwlimit KB/s]
        [--block-size 131072]
        [--retry N] [--retry-delay S] [--retry-backoff F]   # DV-17
        [--ssh-config PATH] [--port N] [--identity PATH]
        [--accept-new-hostkey]
        [--audit-log|--no-audit] [--redact|--no-redact]
        [--json] [--version]

Transport choice
----------------
``auto`` uses paramiko/SFTP when it is importable and falls back to
``scp -O`` (which ships with every OpenSSH client) when it is not. That
fallback is the difference between "no file transfer at all on this host" and
"transfer works, minus resume/progress" (SSH-11). ``data.transport`` says
which one ran.

Overwrite policy (GAP-003)
--------------------------
The default is **refuse to clobber**. Before a single-file copy the script
asks the remote side once what is at the destination (SFTP ``stat`` when
paramiko is available, otherwise one ``ssh 'test -d/-e/-h'``) and, if anything
is there, fails with ``failure_class: "remote_path_exists"`` and exit **168**
without touching it. ``--overwrite`` allows the replacement; ``--resume`` and
``--delete`` (mirror) imply it, because both are defined as writing over what
is already there. When the destination cannot be inspected at all the copy is
refused as ``transfer_io`` with ``data.remote_path_state: "unknown"`` rather
than writing blind.

``--recursive`` is exempt on purpose: it copies the *contents* of the local
directory into the remote directory, so an existing directory target is the
normal case and must not start failing. Clobbering individual files inside a
tree is what the ``--delete`` mirror is for.

Integrity
---------
* SFTP: ``--resume`` verifies with a streaming sha256 on both ends.
* scp: sha256 is compared with the remote ``sha256sum``/``shasum`` after the
  copy, so the "it exited 0, so it must be fine" assumption is still checked.

Metadata and mirroring (DV-19)
------------------------------
* ``--preserve`` applies the local file's mtime and permission bits to the
  remote copy (``cp -p`` semantics). A server that refuses the SFTP SETSTAT
  call yields ``data.preserved=false`` plus ``data.preserve_note`` — the
  bytes still arrived, so it is not reported as a failed transfer.
* ``--delete`` turns the upload into a *mirror*: entries under the remote
  directory that do not exist locally are removed. It is destructive, so it
  requires ``--yes`` and it never runs when the copy itself failed. Removed
  paths are listed in ``data.deleted_files``.

Failure classes come from :mod:`lib.exit_codes`: ``local_path_missing`` 160,
``local_path_is_directory`` 161, ``remote_path_missing`` 162,
``remote_path_invalid`` 163, ``transfer_io`` 164, ``integrity_mismatch`` 165,
``remote_path_exists`` 168, ``permission`` 8, ``auth`` 100, ``host_key`` 101,
``network`` 102, ``dependency_missing`` 3, ``timeout`` 124.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable

# Keep the tree free of __pycache__/*.pyc: a plain `python3 <script>` would
# otherwise write bytecode into the installed (verified) skill directory.
# Must run before `import lib` below. No-op for behaviour.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))
from lib import (  # noqa: E402
    append_audit,
    audit_cli_flags,
    audit_enabled,
    classify_failure,
    emit,
    json_result,
)
from lib.askpass import askpass_env  # noqa: E402
from lib.auth import (  # noqa: E402
    AUTH_CHOICES,
    DEFAULT_PASSWORD_ENV,
    auth_options,  # noqa: E402
)
from lib.auth import resolve as resolve_auth  # noqa: E402
from lib.cli import SSHArgumentParser, guarded_main, handle_version  # noqa: E402
from lib.exit_codes import (  # noqa: E402
    EXIT_AUTH,
    EXIT_DEPENDENCY_MISSING,
    EXIT_HOST_KEY,
    EXIT_INTERNAL,
    EXIT_INVALID_ARGUMENTS,
    EXIT_LOCAL_PATH_IS_DIRECTORY,
    EXIT_LOCAL_PATH_MISSING,
    EXIT_MODE_UNSUPPORTED,
    EXIT_NETWORK,
    EXIT_OK,
    EXIT_PERMISSION,
    EXIT_REMOTE_PATH_INVALID,
    EXIT_REMOTE_PATH_MISSING,
    EXIT_TIMEOUT,
    EXIT_TRANSFER_INTEGRITY,
    EXIT_TRANSFER_IO,
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
    upload_file,
    upload_tree,
)
from lib.transfer import (  # noqa: E402
    REMOTE_PATH_ABSENT,
    REMOTE_PATH_UNKNOWN,
    paramiko_available,
    probe_remote_path_detail,
    remote_path_kind,
    run_scp,
)

DEFAULT_BLOCK_SIZE = 131072


def build_parser() -> argparse.ArgumentParser:
    p = SSHArgumentParser(
        prog="ssh_upload.py",
        description=(
            "Copy a file or tree from this host to a remote alias. Uses SFTP "
            "when paramiko is installed and falls back to scp otherwise."
        ),
    )
    p.add_argument("alias")
    p.add_argument("local_path")
    p.add_argument("remote_path")
    p.add_argument(
        "--transport", choices=("auto", "sftp", "scp", "openssh"), default="auto",
        help=(
            "Transfer backend: sftp/paramiko (resume, chmod, bwlimit, verify), "
            "or scp/openssh (the OpenSSH client itself, no paramiko needed). "
            "'openssh' is an explicit alias for the scp backend; 'auto' picks "
            "sftp when paramiko is importable and scp otherwise (SSHCORE-026)"
        ),
    )
    p.add_argument("--resume", action="store_true", help="Resume a partial SFTP transfer")
    p.add_argument("--recursive", action="store_true")
    p.add_argument(
        "--overwrite", action="store_true",
        help=(
            "Replace the remote file if it already exists. Without this flag "
            "an existing destination is refused with "
            "failure_class=remote_path_exists (exit 168) and left untouched "
            "(GAP-003). --resume and --delete (mirror) imply it, because both "
            "of them are defined as writing over what is already there. "
            "Recursive uploads land *inside* the remote directory, so an "
            "existing directory target is normal and is never refused"
        ),
    )
    p.add_argument(
        "--preserve", action="store_true",
        help=(
            "Copy mtime and permission bits as well as content (cp -p "
            "semantics); SFTP only, reported in data.preserved (DV-19)"
        ),
    )
    p.add_argument(
        "--delete", action="store_true",
        help=(
            "MIRROR mode: remove remote entries that do not exist locally. "
            "Destructive — requires --yes and only runs after a clean copy "
            "(DV-19)"
        ),
    )
    p.add_argument(
        "--yes", "-y", action="store_true",
        help="Confirm the destructive --delete mirror (no prompt is ever shown)",
    )
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--chmod", default=None, help="Octal mode for the remote file (SFTP)")
    p.add_argument("--bwlimit", type=int, default=None, help="Bandwidth cap in KB/s")
    p.add_argument(
        "--block-size", type=int, default=DEFAULT_BLOCK_SIZE,
        help=f"SFTP block size in bytes (default: {DEFAULT_BLOCK_SIZE})",
    )
    p.add_argument("--ssh-config", default=None, help="Read host/IdentityFile from this file")
    p.add_argument("--port", type=int, default=None, help="Remote port override")
    p.add_argument("--identity", default=None, help="Private key path override")
    p.add_argument(
        "--accept-new-hostkey", action="store_true",
        help="Trust an unknown host key once (accept-new)",
    )
    p.add_argument(
        "--auth", choices=AUTH_CHOICES, default="auto",
        help=(
            "Authentication routing for the scp backend: 'auto' reads "
            "# auth: password from --ssh-config, 'password' answers the prompt "
            "through SSH_ASKPASS. Without it a password-only host could not "
            "transfer at all without paramiko (SSHCORE-026)"
        ),
    )
    p.add_argument(
        "--password-env", default=DEFAULT_PASSWORD_ENV,
        help=f"Environment variable holding the password (default: {DEFAULT_PASSWORD_ENV})",
    )
    p.add_argument(
        "--timeout", type=int, default=1800, help="Transfer timeout in seconds (default: 1800)",
    )
    p.add_argument(
        "--redact", dest="redact", action="store_true", default=True,
        help="Hide host identifiers in the envelope (default: on)",
    )
    p.add_argument("--no-redact", dest="redact", action="store_false", help=argparse.SUPPRESS)
    audit_cli_flags(p)
    retry_cli_flags(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--version", "-V", action="store_true", help="Print the ssh-core version")
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
            "direction": "upload",
            "transport": transport,
            "alias": args.alias,
            "local_path": args.local_path,
            "remote_path": args.remote_path,
            "recursive": args.recursive,
            "resume": args.resume,
            "overwrite": _overwrite_permitted(args),
            "bytes_transferred": 0,
            "elapsed_s": 0.0,
            "sha256_verified": False,
            "failure_class": failure_class,
        },
    )


def _sftp_data(args: argparse.Namespace, tr: TransferResult) -> dict[str, Any]:
    data: dict[str, Any] = {
        "route": "sftp",
        "direction": "upload",
        "transport": "sftp",
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
            "direction": "upload",
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


def _overwrite_permitted(args: argparse.Namespace) -> bool:
    """True when this invocation is allowed to replace an existing target.

    ``--overwrite`` is the explicit form; ``--resume`` and ``--delete`` imply it
    because both are defined as "continue writing into what is already there"
    (a resume with no target is a plain copy, and a mirror without replacement
    is not a mirror). GAP-003.
    """
    return bool(
        getattr(args, "overwrite", False)
        or getattr(args, "resume", False)
        or getattr(args, "delete", False)
    )


def _refuse_existing(
    args: argparse.Namespace,
    failure_class: str,
    stderr: str,
    *,
    kind: str,
    transport: str,
) -> dict[str, Any]:
    result = _failure(
        args,
        failure_class=failure_class,
        stderr=stderr,
        transport=transport,
        exit_code=exit_code_for(failure_class),
    )
    result["data"]["remote_path_state"] = kind
    result["data"]["overwrite"] = False
    result["data"]["requires"] = "--overwrite"
    return result


def _overwrite_guard(
    args: argparse.Namespace,
    probe: Callable[[str], tuple[str, int, str]],
    *,
    transport: str,
) -> dict[str, Any] | None:
    """Refuse to clobber an existing remote file unless it was asked for.

    Returns a failure envelope, or ``None`` when the transfer may proceed. The
    rule (GAP-003):

    * ``--overwrite`` / ``--resume`` / ``--delete`` → always allowed;
    * **single file only** — one probe of the *final* target path. A recursive
      upload copies *into* the remote directory, so an existing directory there
      is the normal case and must not start failing;
    * target present → ``remote_path_exists`` (168), nothing written;
    * probe impossible → fail closed with ``transfer_io`` (164) plus
      ``data.remote_path_state == "unknown"``, instead of assuming the
      destination is free. "I could not tell" is exactly the situation in which
      a default clobber destroys data;
    * ssh itself failed → report that failure class (auth/network/host_key), so
      an unreachable host is not blamed on the destination.
    """
    if _overwrite_permitted(args):
        return None
    if getattr(args, "recursive", False):
        return None
    kind, returncode, stderr_text = probe(args.remote_path)
    if kind == REMOTE_PATH_ABSENT:
        return None
    if kind == REMOTE_PATH_UNKNOWN or returncode not in (0,):
        classified = classify_failure(returncode, stderr_text or "")
        if classified in ("auth", "host_key", "network", "alias_not_found", "kex"):
            detail = (stderr_text or "").strip()
            return _refuse_existing(
                args,
                classified,
                (
                    f"ssh_upload: could not check whether '{args.remote_path}' "
                    f"already exists on {args.alias}: {detail or classified}. "
                    "Nothing was written.\n"
                ),
                kind="unknown",
                transport=transport,
            )
        # Cannot prove the destination is free: fail closed rather than
        # overwrite blind. `transfer_io` (164) is the transfer family's "the
        # copy could not be performed" class; `remote_path_state` says why.
        return _refuse_existing(
            args,
            "transfer_io",
            (
                f"ssh_upload: cannot determine whether '{args.remote_path}' "
                f"already exists on {args.alias} "
                f"({(stderr_text or '').strip() or 'no answer from the remote shell'}). "
                "Refusing to overwrite blind. Re-run with --overwrite if "
                "replacing it is intended, or fix the transport and retry.\n"
            ),
            kind="unknown",
            transport=transport,
        )
    return _refuse_existing(
        args,
        "remote_path_exists",
        (
            f"ssh_upload: '{args.alias}:{args.remote_path}' already exists "
            f"({kind}). Refusing to overwrite it; the remote file was left "
            "untouched. Re-run with --overwrite to replace it, or pick another "
            "destination (GAP-003).\n"
        ),
        kind=kind,
        transport=transport,
    )


def _probe_with_ssh(args: argparse.Namespace, password: str | None):
    """Adapter: ``(kind, rc, stderr)`` from one ``ssh 'test -e'`` probe."""

    def probe(path: str) -> tuple[str, int, str]:
        return probe_remote_path_detail(
            args.alias,
            path,
            password=password,
            ssh_config=args.ssh_config,
            accept_new_hostkey=args.accept_new_hostkey,
            port=args.port,
            identity=args.identity,
            timeout=min(int(getattr(args, "timeout", 30) or 30), 60),
        )

    return probe


def _run_sftp(args: argparse.Namespace, chmod_int: int | None) -> dict[str, Any]:
    try:
        with SftpSession(
            args.alias,
            ssh_config=args.ssh_config,
            accept_new_hostkey=args.accept_new_hostkey,
            port=args.port,
            identity=args.identity,
        ) as session:
            if args.recursive:
                tr = upload_tree(
                    session, args.local_path, args.remote_path,
                    resume=args.resume, chmod=chmod_int,
                    block_size=args.block_size, bwlimit_kbs=args.bwlimit,
                    progress=not args.no_progress,
                    preserve=getattr(args, "preserve", False),
                    delete=getattr(args, "delete", False),
                )
            else:
                # GAP-003: one SFTP stat on the *final* target, taken on the
                # session that is already open. No second connection, no
                # per-file walk, and only for the single-file case: with
                # --recursive the destination is a directory that the contents
                # are placed into, so an existing one is normal.
                def probe(path: str) -> tuple[str, int, str]:
                    return remote_path_kind(args.alias, path, session=session), 0, ""

                guard = _overwrite_guard(args, probe, transport="sftp")
                if guard is not None:
                    return guard
                tr = upload_file(
                    session, args.local_path, args.remote_path,
                    resume=args.resume, chmod=chmod_int,
                    block_size=args.block_size, bwlimit_kbs=args.bwlimit,
                    progress=not args.no_progress,
                    preserve=getattr(args, "preserve", False),
                )
            if tr.success and getattr(args, "delete", False) and not args.recursive:
                # --delete is a *tree* notion; for one file it would mean
                # deleting the whole remote directory, which nobody means.
                tr.deleted_files = None
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
            stderr=f"remote write permission denied: {args.remote_path} ({e})",
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


def _run_scp(args: argparse.Namespace, password: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    preserve = bool(getattr(args, "preserve", False))
    # GAP-003: exactly one `ssh 'test -e'` before the copy, never a per-file
    # storm. It is skipped for --recursive (the destination is a directory the
    # contents go into) and whenever --overwrite/--resume/--delete said the
    # replacement is intended.
    guard = _overwrite_guard(
        args, _probe_with_ssh(args, password), transport="scp",
    )
    if guard is not None:
        return guard
    outcome = run_scp(
        args.local_path,
        f"{args.alias}:{args.remote_path}",
        recursive=args.recursive,
        timeout=args.timeout,
        port=args.port,
        identity=args.identity,
        ssh_config=args.ssh_config,
        accept_new_hostkey=args.accept_new_hostkey,
        preserve=preserve,
        local_path=Path(args.local_path) if not args.recursive else None,
        verify_remote=(
            None if args.recursive else (args.alias, args.remote_path)
        ),
        password=password,
    )
    data = {
        "route": "scp",
        "direction": "upload",
        "transport": "scp",
        "alias": args.alias,
        "local_path": args.local_path,
        "remote_path": args.remote_path,
        "recursive": args.recursive,
        "resume": False,
        "bytes_transferred": outcome.bytes_transferred,
        "elapsed_s": round(outcome.elapsed_s or (time.monotonic() - started), 3),
        "sha256_verified": outcome.sha256_verified,
        "failure_class": outcome.failure_class,
        "scp_argv": outcome.argv,
        "transport_note": (
            "scp fallback: paramiko is not installed. --resume/--chmod/"
            "--bwlimit/--block-size only apply to the sftp transport; "
            "--preserve maps to `scp -p`, and --delete is refused (it needs "
            "the sftp transport) rather than silently skipped."
        ),
    }
    if preserve:
        # FA-05: `scp -p` really does keep mtime/mode, so report it instead of
        # the old `preserved: null` + note while claiming success.
        data["preserve_requested"] = True
        data["preserved"] = outcome.preserved
    if getattr(args, "delete", False):
        data["mirror"] = True
        data["mirror_transport"] = "scp"
    if outcome.detail:
        data["detail"] = outcome.detail
    return json_result(
        success=outcome.success,
        exit_code=EXIT_OK if outcome.success else exit_code_for(outcome.failure_class),
        stderr="" if outcome.success else outcome.stderr,
        data=data,
    )


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912
    argv = list(sys.argv[1:] if argv is None else argv)
    early = handle_version("ssh_upload.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)

    local_path = Path(args.local_path)
    if not local_path.exists():
        result = _failure(
            args, failure_class="local_path_missing",
            stderr=f"local path not found: {args.local_path}",
            exit_code=EXIT_LOCAL_PATH_MISSING,
        )
        emit(result, as_json=args.json)
        return int(result["exit_code"])
    if local_path.is_dir() and not args.recursive:
        result = _failure(
            args, failure_class="local_path_is_directory",
            stderr=f"'{args.local_path}' is a directory; pass --recursive",
            exit_code=EXIT_LOCAL_PATH_IS_DIRECTORY,
        )
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    chmod_int: int | None = None
    if args.chmod is not None:
        try:
            chmod_int = int(args.chmod, 8)
        except (TypeError, ValueError):
            result = _failure(
                args, failure_class="invalid_arguments",
                stderr=f"invalid --chmod (expected octal like 0644): {args.chmod}",
                exit_code=EXIT_INVALID_ARGUMENTS,
            )
            emit(result, as_json=args.json)
            return EXIT_INVALID_ARGUMENTS

    # --delete is destructive by definition: it removes data that exists only
    # on the remote side. ssh-core never prompts (there is no tty guarantee),
    # so the caller has to say --yes. Refusing loudly beats deleting silently.
    if args.delete and not args.yes:
        result = _failure(
            args, failure_class="confirmation_required",
            stderr=(
                "ssh_upload: --delete mirrors the source and DELETES remote "
                "files that do not exist locally. Re-run with --yes if that is "
                "what you want, or drop --delete for a plain copy.\n"
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
                "ssh_upload: --delete only makes sense with --recursive "
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
                "ssh_upload: --transport sftp requires paramiko. Install it "
                "(pip install 'paramiko>=3.0,<4') or use --transport auto/scp."
            ),
            exit_code=EXIT_DEPENDENCY_MISSING,
        )
        emit(result, as_json=args.json)
        return int(result["exit_code"])

    # SSHCORE-026: the OpenSSH/scp backend is a real backend, so asking for an
    # SFTP-only feature on it must fail loudly instead of silently doing less.
    if not use_sftp:
        deferred = [
            name for name, requested in (
                ("--resume", args.resume),
                ("--chmod", args.chmod is not None),
                ("--bwlimit", args.bwlimit is not None),
            ) if requested
        ]
        if deferred:
            names = ", ".join(deferred)
            result = _failure(
                args, failure_class="mode_deferred",
                stderr=(
                    f"ssh_upload: {names} need the sftp transport (paramiko), "
                    "which is not available here. Use --transport auto with "
                    "paramiko installed, or drop the flag for a plain scp copy "
                    "(SSHCORE-026).\n"
                ),
                transport="scp",
                exit_code=EXIT_MODE_UNSUPPORTED,
            )
            result["data"]["deferred_transport"] = "sftp"
            result["data"]["deferred_flags"] = deferred
            emit(result, as_json=args.json)
            return int(result["exit_code"])

    # SSHCORE-026 follow-up: same auth profiles as exec/tunnel/daemon, so a
    # password-only host can transfer without paramiko. A missing secret is
    # `password_required` (105) *before* any spawn, not "Permission denied".
    auth_method, password = resolve_auth(
        args.alias, args.auth, args.ssh_config, args.password_env
    )
    if auth_method == "password" and not password:
        result = _failure(
            args, failure_class="password_required",
            stderr=(
                f"ssh_upload: {args.alias} needs password auth but "
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

    policy = policy_from_args(args)
    if use_sftp:
        def _attempt(_n: int) -> dict[str, Any]:
            return _run_sftp(args, chmod_int)
    else:
        # FA-05: `--delete` (mirror) has no scp implementation. Refuse it up
        # front instead of returning `mirror: true, deleted_files: [], success:
        # true` — a caller that trusted `success` believed the mirror ran while
        # the stale remote files were still there.
        if args.delete:
            result = _failure(
                args, failure_class="mode_deferred",
                stderr=(
                    "ssh_upload: --delete needs the sftp transport (paramiko), "
                    "which is not installed here. Run `pip install "
                    "'paramiko>=3.0,<4'`, or drop --delete for a plain copy: "
                    "scp cannot prune, and reporting success without pruning "
                    "would be a lie.\n"
                ),
                transport="scp",
                exit_code=EXIT_MODE_UNSUPPORTED,
            )
            result["data"]["mirror"] = True
            result["data"]["deferred_transport"] = "sftp"
            emit(result, as_json=args.json)
            return int(result["exit_code"])

        def _attempt(_n: int) -> dict[str, Any]:
            return _run_scp(args, password)
    # Re-opening the SFTP session per attempt is deliberate: a broken
    # transport is exactly what the retry is for, and a half-dead paramiko
    # client cannot be reused safely.
    result = retrying(_attempt, policy) if policy.enabled else _attempt(1)
    result.setdefault("data", {})["auth"] = auth_method
    _audit(args, result)
    redactor = redactor_for(args.alias, args.ssh_config, enabled=args.redact)
    emit(result, as_json=args.json, redactor=redactor)
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(guarded_main("ssh_upload.py", main))
