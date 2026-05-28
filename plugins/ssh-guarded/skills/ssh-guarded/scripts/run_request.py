#!/usr/bin/env python3
"""run_request.py — validate-only or validate-and-execute (v1.0).

Reads a request artifact emitted by one of ``request_command.py`` /
``request_upload.py`` / ``request_mkdir.py`` / ``request_delete.py``,
runs the locked 7-step validation (design.md §3 D7), and either reports
the validation outcome (default) or dispatches the action to ssh-core
and writes ``<id>.result.json`` (with ``--execute``).

v1.0 surface:
    run_request.py --request <path-to-artifact.json>
        [--execute]                       (default: validate-only)
        [--confirm-delete]                (REQUIRED when delete + irreversible)
        [--server <alias>]                (must equal artifact.server if given)
        [--show-sensitive]                (disable redaction in human print)
        [--no-redact]                     (SUPPRESS; disable redaction in result.json)
        [--project-id <id>]               (SUPPRESS)
        [--json]                          (SUPPRESS)

Validation order (locked, design D7):
    1. schema (load)             → exit 53 artifact_invalid
    2. expiry                    → exit 53 artifact_expired
    3. server consistency        → exit 53 artifact_invalid (data.reason=server_mismatch)
    4. denylist                  → exit 51 path_in_denylist
    5. sandbox                   → exit 50 workdir_outside_sandbox
    6. SHA-256 (upload only)     → exit 56 sha256_mismatch
    7. lock acquire (execute)    → exit 54 lock_held

Concurrency: ``<artifact>.lock`` via ``lib.exclusive_lock`` held for the
duration of dispatch (step 7 + subprocess call). Validation steps 1-6
do NOT hold the lock so failed validations never block other processes.

See:
    .trellis/tasks/05-25-ssh-guarded-request-pipeline/{prd,design,implement}.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import (  # noqa: E402, I001
    SSH_CORE_SCRIPTS_DIR,
    ArtifactError,
    LockHeld,
    Redactor,
    RequestArtifact,
    exclusive_lock,
    iso_utc_now,
    resolve_project_id,
    save_result,
)

# --- Constants --------------------------------------------------------------

_EXIT_CODES: dict[str, int] = {
    "workdir_outside_sandbox": 50,
    "path_in_denylist": 51,
    "artifact_invalid": 53,
    "artifact_expired": 53,
    "lock_held": 54,
    "sha256_mismatch": 56,
    "transport_failed": 57,
    "delete_confirm_required": 59,
}

_HUMAN_HINTS: dict[str, str] = {
    "workdir_outside_sandbox": "remote path / workdir is outside ~/workspace/<project_id>/",
    "path_in_denylist": "remote path matches the denylist",
    "artifact_invalid": "request artifact failed schema or consistency check",
    "artifact_expired": "request artifact is past its expires_at",
    "lock_held": "another --execute holds the lock on this artifact",
    "sha256_mismatch": "local file SHA-256 changed since the artifact was drafted",
    "transport_failed": "ssh-core subprocess did not return a valid JSON envelope",
    "delete_confirm_required": "delete is irreversible; pass --confirm-delete to proceed",
}

DEFAULT_REDACT_CATALOG = Path.home() / ".devops" / "redact.json"
DEFAULT_TIMEOUT_SECONDS = 600
SHA256_BLOCK_BYTES = 131072  # 128 KiB (mirrors ADR-003 D2 / Phase B request_upload)

# Single source of truth for stderr-tail length in human-mode print.
_STDERR_TAIL_CHARS = 200
_TRANSPORT_STDERR_CAP = 4000


# --- Parser -----------------------------------------------------------------


def _build_common_parser() -> argparse.ArgumentParser:
    """Shared SUPPRESS flags (design D10) — same pattern as drafters."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--project-id", default=argparse.SUPPRESS)
    p.add_argument("--no-redact", action="store_true", default=argparse.SUPPRESS)
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    p = argparse.ArgumentParser(
        prog="run_request.py",
        parents=[common],
        description=(
            "Validate (default) or execute (--execute) a request artifact. "
            "Without --execute, runs steps 1-6 of the locked validation "
            "order and reports the outcome without touching ssh-core."
        ),
    )
    p.add_argument("--request", required=True, help="Path to request JSON artifact")
    p.add_argument(
        "--execute",
        action="store_true",
        help="Acquire the lock and dispatch to ssh-core. Without this "
        "flag, the script only validates.",
    )
    p.add_argument(
        "--confirm-delete",
        dest="confirm_delete",
        action="store_true",
        help="Required when operation=delete and risk_summary.irreversible=true.",
    )
    p.add_argument(
        "--server",
        default=None,
        help="Optional alias; if given, must equal artifact.server.",
    )
    p.add_argument(
        "--show-sensitive",
        dest="show_sensitive",
        action="store_true",
        help="Disable redaction in the human-mode print. The on-disk "
        "result.json is governed by --no-redact, not this flag.",
    )
    return p


# --- Envelope helpers -------------------------------------------------------


def _envelope_error(
    exc: ArtifactError,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"failure_class": exc.failure_class}
    if extra_data:
        data.update(extra_data)
    return {
        "success": False,
        "data": data,
        "error": {
            "message": exc.message,
            "failure_class": exc.failure_class,
            "exit_code": exc.exit_code,
        },
    }


def _envelope_success(data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None}


def _emit(envelope: dict[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(envelope, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if envelope["success"]:
        data = envelope["data"]
        if data.get("validate_only"):
            sys.stdout.write(
                f"validated {data['request_id']} ({data['operation']}) — "
                f"ready to --execute\n"
            )
        else:
            sys.stdout.write(
                f"executed {data['request_id']} → {data['result_path']}\n"
                f"transport={data['transport']} "
                f"exit_code={data['exit_code']} "
                f"duration_ms={data['duration_ms']}\n"
            )
            tail = data.get("stderr_tail") or ""
            if tail:
                sys.stdout.write(f"stderr_tail: {tail}\n")
    else:
        err = envelope["error"]
        sys.stderr.write(f"{err['failure_class']}: {err['message']}\n")


# --- SHA-256 streaming (mirrors Phase B request_upload._sha256_streaming) ---


def _sha256_streaming(path: Path, block: int = SHA256_BLOCK_BYTES) -> str:
    """Stream-hash a local file. Raises OSError if the path can't be read."""
    h = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(block)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _verify_upload_sha256(art: RequestArtifact) -> None:
    """Re-hash the local file and compare to artifact's recorded sha256.

    Raises ``ArtifactError(sha256_mismatch, 56)`` on drift; attaches
    ``expected_sha256`` / ``actual_sha256`` to the exception so the
    envelope can carry both fields (PRD FR-4).

    No-op when ``operation != "upload"``.
    """
    if art.operation != "upload":
        return
    local_raw = art.payload.get("local_path")
    expected = art.payload.get("local_sha256")
    if not local_raw or not expected:
        raise ArtifactError(
            failure_class="artifact_invalid",
            exit_code=53,
            message="upload artifact missing local_path or local_sha256",
        )
    local_path = Path(str(local_raw)).expanduser()
    try:
        actual = _sha256_streaming(local_path)
    except FileNotFoundError as exc:
        raise ArtifactError(
            failure_class="sha256_mismatch",
            exit_code=56,
            message=(
                f"local file {local_path} disappeared between draft and execute"
            ),
        ) from exc
    except OSError as exc:
        raise ArtifactError(
            failure_class="sha256_mismatch",
            exit_code=56,
            message=f"cannot re-hash local file {local_path}: {exc}",
        ) from exc
    if actual != expected:
        err = ArtifactError(
            failure_class="sha256_mismatch",
            exit_code=56,
            message=(
                f"local file SHA-256 drifted: expected {expected[:16]}…, "
                f"got {actual[:16]}…"
            ),
        )
        err.expected_sha256 = expected  # type: ignore[attr-defined]
        err.actual_sha256 = actual  # type: ignore[attr-defined]
        raise err


# --- Transport argv (design D8) ---------------------------------------------


def _ssh_execute_path() -> Path:
    return SSH_CORE_SCRIPTS_DIR / "ssh_execute.py"


def _ssh_upload_path() -> Path:
    return SSH_CORE_SCRIPTS_DIR / "ssh_upload.py"


_REMOTE_HOME_TIMEOUT_SECONDS = 10


def _resolve_remote_home(
    server: str,
    timeout_seconds: int = _REMOTE_HOME_TIMEOUT_SECONDS,
) -> str:
    """Resolve ``$HOME`` on the remote host so transport can expand ``~/``.

    Why: paramiko SFTP and ``shlex.quote('~/foo')`` both keep ``~`` literal,
    so dispatching artifact paths verbatim writes to ``/root/~/workspace/...``
    instead of the user's home. We resolve once per ``run_request --execute``
    via a single ``BatchMode`` SSH probe and use the result to materialise
    payload paths inside :func:`_build_argv` (design.md §3 D8 transport
    tilde-expansion, drift recorded 2026-05-28).

    Raises ``ArtifactError(transport_failed, 57)`` on ssh failure, timeout,
    or non-absolute output — never silently falls back, since a wrong home
    silently corrupts the remote filesystem.
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_seconds}",
        server,
        'printf %s "$HOME"',
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 5,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArtifactError(
            failure_class="transport_failed",
            exit_code=57,
            message=(
                f"resolving remote $HOME on '{server}' timed out after "
                f"{timeout_seconds}s"
            ),
        ) from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[:200]
        raise ArtifactError(
            failure_class="transport_failed",
            exit_code=57,
            message=(
                f"resolving remote $HOME on '{server}' failed "
                f"(ssh exit={proc.returncode}): {stderr_tail or '<no stderr>'}"
            ),
        )
    home = (proc.stdout or "").strip()
    if not home.startswith("/"):
        raise ArtifactError(
            failure_class="transport_failed",
            exit_code=57,
            message=(
                f"resolving remote $HOME on '{server}' produced "
                f"non-absolute path: {home!r}"
            ),
        )
    return home


def _expand_remote_tilde(path: str, remote_home: str) -> str:
    """Expand a leading ``~/`` (or bare ``~``) to ``<remote_home>/...``.

    Only ``~/`` and ``~`` forms are touched; ``~user/`` and any path
    that does not start with ``~`` pass through unchanged. ``remote_home``
    must be an absolute POSIX path (guaranteed by :func:`_resolve_remote_home`).
    """
    if path == "~":
        return remote_home
    if path.startswith("~/"):
        suffix = path[2:]
        return f"{remote_home}/{suffix}" if suffix else remote_home
    return path


def _build_argv(
    art: RequestArtifact,
    remote_home: str | None = None,
) -> list[str]:
    """Compose subprocess argv per design D8 table.

    Quoting: ``shlex.quote`` on remote-shell strings (the inner mkdir /
    rm one-liners). The outer argv is always a Python list (ADR-001 D1
    + CONTRIBUTING.md:37).

    When ``remote_home`` is provided, ``~/`` prefixes in payload
    ``remote_path`` (upload/mkdir/delete) are expanded to the absolute
    remote home so SFTP and ``shlex.quote``-wrapped shell strings both
    target the real path (design.md §3 D8 transport tilde-expansion).
    """
    op = art.operation
    server = art.server
    py = sys.executable or "python3"

    def _expand(p: str) -> str:
        return _expand_remote_tilde(p, remote_home) if remote_home else p

    if op == "command":
        timeout = int(art.payload.get("timeout") or 60)
        return [
            py,
            str(_ssh_execute_path()),
            server,
            str(art.payload["command"]),
            "--timeout",
            str(timeout),
            "--json",
        ]
    if op == "upload":
        local = str(art.payload["local_path"])
        remote = _expand(str(art.payload["remote_path"]))
        overwrite = bool(art.payload.get("overwrite", False))
        argv = [py, str(_ssh_upload_path()), server, local, remote]
        if overwrite:
            argv.append("--overwrite")
        argv.append("--json")
        return argv
    if op == "mkdir":
        remote = _expand(str(art.payload["remote_path"]))
        mode = str(art.payload.get("mode") or "0755")
        parents = bool(art.payload.get("parents", False))
        quoted = shlex.quote(remote)
        flag_p = "-p " if parents else ""
        cmd = f"mkdir {flag_p}-m {mode} -- {quoted}"
        return [py, str(_ssh_execute_path()), server, cmd, "--json"]
    if op == "delete":
        remote = _expand(str(art.payload["remote_path"]))
        recursive = bool(art.payload.get("recursive", False))
        quoted = shlex.quote(remote)
        flag = "-rf" if recursive else "-f"
        cmd = f"rm {flag} -- {quoted}"
        return [py, str(_ssh_execute_path()), server, cmd, "--json"]
    raise ArtifactError(
        failure_class="artifact_invalid",
        exit_code=53,
        message=f"unknown operation '{op}' in transport routing",
    )


# --- Dispatch ---------------------------------------------------------------


def _dispatch_subprocess(
    argv: list[str],
    timeout_seconds: int,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    """Run the ssh-core subprocess; return (child_envelope, completed_process).

    Raises ``ArtifactError(transport_failed, 57)`` when stdout is not a
    well-shaped JSON envelope. Raises ``ArtifactError(timeout, 124)``
    on ``subprocess.TimeoutExpired``.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArtifactError(
            failure_class="timeout",
            exit_code=124,
            message=f"transport timed out after {timeout_seconds}s",
        ) from exc

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        err = ArtifactError(
            failure_class="transport_failed",
            exit_code=57,
            message="ssh-core subprocess did not emit JSON on stdout",
        )
        err.transport_returncode = proc.returncode  # type: ignore[attr-defined]
        err.transport_stderr = (proc.stderr or "")[:_TRANSPORT_STDERR_CAP]  # type: ignore[attr-defined]
        raise err from exc

    if not isinstance(envelope, dict) or "success" not in envelope:
        err = ArtifactError(
            failure_class="transport_failed",
            exit_code=57,
            message="ssh-core subprocess envelope is malformed",
        )
        err.transport_returncode = proc.returncode  # type: ignore[attr-defined]
        err.transport_stderr = (proc.stderr or "")[:_TRANSPORT_STDERR_CAP]  # type: ignore[attr-defined]
        raise err

    return envelope, proc


# --- Main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912, PLR0915
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    no_redact = getattr(args, "no_redact", False)

    art_path = Path(args.request).expanduser().resolve()

    try:
        # Step 1: schema (load enforces version + required fields)
        art = RequestArtifact.load(art_path)

        # Steps 2-5: expiry + server + sandbox + denylist
        project_id = resolve_project_id(args, dict(os.environ), Path.cwd())
        try:
            art.validate(
                project_id=project_id,
                expected_server=args.server,
            )
        except ArtifactError as exc:
            # Decorate server_mismatch with data.reason so callers can
            # distinguish it from generic artifact_invalid hits.
            if (
                exc.failure_class == "artifact_invalid"
                and args.server is not None
                and args.server != art.server
            ):
                envelope = _envelope_error(
                    exc,
                    extra_data={
                        "reason": "server_mismatch",
                        "expected_server": args.server,
                        "artifact_server": art.server,
                    },
                )
                _emit(envelope, as_json)
                return exc.exit_code
            raise

        # Step 6: SHA-256 (upload only). Runs in BOTH validate-only and
        # execute modes — design D7 places sha256 verify BEFORE the lock
        # acquire, and the lock only matters for dispatch.
        try:
            _verify_upload_sha256(art)
        except ArtifactError as exc:
            extra: dict[str, Any] = {}
            if hasattr(exc, "expected_sha256"):
                extra["expected_sha256"] = exc.expected_sha256  # type: ignore[attr-defined]
            if hasattr(exc, "actual_sha256"):
                extra["actual_sha256"] = exc.actual_sha256  # type: ignore[attr-defined]
            envelope = _envelope_error(exc, extra_data=extra)
            _emit(envelope, as_json)
            return exc.exit_code

        # Pre-execute policy gate: delete + irreversible requires
        # --confirm-delete. Checked here so validate-only also surfaces
        # the requirement — helpful when reviewing artifacts before
        # coming back with --execute.
        if (
            art.operation == "delete"
            and art.risk_summary.irreversible
            and not args.confirm_delete
        ):
            confirm_exc = ArtifactError(
                failure_class="delete_confirm_required",
                exit_code=59,
                message=(
                    "delete is irreversible; re-run with --confirm-delete "
                    "after reviewing the artifact"
                ),
            )
            envelope = _envelope_error(confirm_exc)
            _emit(envelope, as_json)
            return confirm_exc.exit_code

        # Validate-only mode: stop here with a success envelope. No
        # lock, no result file, no ssh-core touch.
        if not args.execute:
            envelope = _envelope_success(
                {
                    "validate_only": True,
                    "artifact_path": str(art_path),
                    "request_id": art.request_id,
                    "operation": art.operation,
                    "server": art.server,
                },
            )
            _emit(envelope, as_json)
            return 0

        # Step 7: lock acquire + dispatch + write result.json
        try:
            with exclusive_lock(art_path):
                start = time.monotonic()
                executed_at = iso_utc_now()
                # Resolve remote $HOME so transport (paramiko SFTP /
                # shlex.quote'd shell strings) can expand ``~/`` in
                # payload.remote_path. Failure → ArtifactError(57)
                # caught by the outer ArtifactError handler.
                remote_home = _resolve_remote_home(art.server)
                argv_list = _build_argv(art, remote_home=remote_home)
                # command op respects payload.timeout; other ops fall
                # back to the transport default. PRD §v1.0 surface notes
                # --timeout-execute is deferred to v1.1.
                if art.operation == "command":
                    timeout = int(
                        art.payload.get("timeout") or DEFAULT_TIMEOUT_SECONDS
                    )
                else:
                    timeout = DEFAULT_TIMEOUT_SECONDS

                child_env, proc = _dispatch_subprocess(argv_list, timeout)
                duration_ms = int((time.monotonic() - start) * 1000)

                # Redactor: instantiated only when --no-redact is NOT set
                # (Phase A Redactor never bypasses internally; the CLI
                # layer decides whether to apply it). Catalog path is
                # ~/.devops/redact.json; missing → default catalog (the
                # Redactor handles the fallback).
                redactor: Redactor | None = (
                    None if no_redact else Redactor(DEFAULT_REDACT_CATALOG)
                )

                child_exit_code = int(
                    child_env.get("exit_code", proc.returncode),
                )
                child_stdout = str(child_env.get("stdout", "") or "")
                child_stderr = str(child_env.get("stderr", "") or "")
                transport = (
                    "ssh_upload" if art.operation == "upload" else "ssh_execute"
                )

                result: dict[str, Any] = {
                    "executed_at": executed_at,
                    "exit_code": child_exit_code,
                    "duration_ms": duration_ms,
                    "stdout_redacted": child_stdout,
                    "stderr_redacted": child_stderr,
                    "transport": transport,
                    "transport_exit_code": proc.returncode,
                }
                # Per Open Item O4 (user-approved default): transport_argv
                # is included only when --no-redact is set, so credentials
                # / paths inside argv don't leak by default. The field
                # name stays present (null when redacted) so downstream
                # readers don't branch on schema variants.
                result["transport_argv"] = argv_list if no_redact else None

                # save_result applies the redactor to stdout_redacted /
                # stderr_redacted and atomic-writes <id>.result.json next
                # to the artifact (mode 0o600).
                result_path = save_result(
                    art, result, redactor, out_dir=art_path.parent
                )

                envelope_data: dict[str, Any] = {
                    "result_path": str(result_path),
                    "request_id": art.request_id,
                    "transport": transport,
                    "exit_code": child_exit_code,
                    "duration_ms": duration_ms,
                    "stderr_tail": child_stderr[-_STDERR_TAIL_CHARS:],
                }
                # --show-sensitive only affects what's printed; the
                # on-disk result.json is governed by --no-redact. When
                # redaction was applied to result.json and the user has
                # NOT asked to see raw output, also redact the printed
                # stderr_tail.
                if redactor is not None and not args.show_sensitive:
                    redacted_tail, _ = redactor.apply(
                        envelope_data["stderr_tail"]
                    )
                    envelope_data["stderr_tail"] = redacted_tail

                envelope = _envelope_success(envelope_data)
                _emit(envelope, as_json)
                return 0
        except LockHeld as lh:
            lock_exc = ArtifactError(
                failure_class="lock_held",
                exit_code=54,
                message=(
                    f"another --execute holds the lock on {art_path.name}"
                    f" (pid={lh.holder_pid})"
                ),
            )
            envelope = _envelope_error(
                lock_exc, extra_data={"holder_pid": lh.holder_pid}
            )
            _emit(envelope, as_json)
            return lock_exc.exit_code

    except ArtifactError as outer_exc:
        outer_extra: dict[str, Any] = {}
        if hasattr(outer_exc, "transport_returncode"):
            outer_extra["transport_returncode"] = outer_exc.transport_returncode  # type: ignore[attr-defined]
        if hasattr(outer_exc, "transport_stderr"):
            outer_extra["transport_stderr"] = outer_exc.transport_stderr  # type: ignore[attr-defined]
        envelope = _envelope_error(outer_exc, extra_data=outer_extra or None)
        _emit(envelope, as_json)
        return outer_exc.exit_code

    # Defensive fall-through (should be unreachable).
    return 1


if __name__ == "__main__":
    sys.exit(main())
