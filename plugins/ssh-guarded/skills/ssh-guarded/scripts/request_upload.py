#!/usr/bin/env python3
"""request_upload.py — draft a file-upload request (v1.0).

Produces an artifact under ``$SSH_GUARDED_HOME/reports/<project_id>/requests/``
(``upload-<id>.json``). Does NOT execute. Hand the artifact to
``run_request.py --execute`` after human review.

v1.0 surface:
    request_upload.py <alias>
        --local <abs-or-rel-path>           (required)
        --remote <remote-path>              (required; inside sandbox)
        --reason "<intent>"                 (required)
        [--overwrite]                       (risk auto-flags irreversible)
        [--confirm-sensitive-local-upload]  (required when --local outside upload_roots)
        [--expires-in 1h]
        [--project-id <id>]                 (SUPPRESS)
        [--reports-dir <path>]              (override the state root)
        [--json]                            (SUPPRESS)

Payload (operation="upload"):
    {
        "local_path": "...",  "local_sha256": "...",  "local_size": int,
        "remote_path": "...", "overwrite": bool,      "sensitive_local": bool
    }

Implementation notes:
- SHA-256 + size are computed by a 128 KiB streaming read of the local
  file (matches ADR-003 D2 block size).
- Hard cap 256 MiB on local_size (D5 + ``MAX_UPLOAD_BYTES``); checked
  after the stream so the size in the artifact is real bytes read.
- ``upload_roots`` are hard-coded for v1.0: ``~/Downloads``, ``~/Desktop``,
  ``cwd`` at draft time, ``~/workspace``. v1.1 will allow override via
  ``~/.devops/upload_roots.json``.
- Remote outside sandbox → warn + write (C3 pedagogical).
- State lives outside the plugin tree (GAP-006): see ``lib/state.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import (  # noqa: E402, I001
    ArtifactError,
    GuardedArgumentParser,
    RequestArtifact,
    add_version_flag,
    allocate_request_id,
    compute_risk,
    emit,
    failure_envelope,
    handle_version,
    is_inside_sandbox,
    iso_utc_now,
    iso_utc_offset,
    normalize_remote_path,
    parse_expires_in,
    resolve_project_id,
    success_envelope,
    warn_legacy_reports,
)
from lib import requests_dir as state_requests_dir  # noqa: E402
from lib.request_artifact import MAX_UPLOAD_BYTES  # noqa: E402

# --- Constants --------------------------------------------------------------

OPERATION = "upload"

_EXIT_CODES: dict[str, int] = {
    "local_path_missing": 35,
    "local_path_outside_upload_roots": 36,  # reserved; B4 uses sensitive_local_unconfirmed
    "sha256_compute_failed": 37,
    "sensitive_local_unconfirmed": 38,
    "size_limit_exceeded": 39,
}

_HUMAN_HINTS: dict[str, str] = {
    "local_path_missing": "--local file not found / not a regular file",
    "sha256_compute_failed": "I/O error while reading --local for sha256",
    "sensitive_local_unconfirmed": (
        "--local is outside upload_roots (~/Downloads, ~/Desktop, cwd, ~/workspace); "
        "re-run with --confirm-sensitive-local-upload to acknowledge"
    ),
    "size_limit_exceeded": f"--local exceeds the 256 MiB cap ({MAX_UPLOAD_BYTES} bytes)",
}

SHA256_BLOCK_BYTES = 131072  # 128 KiB (matches ADR-003 D2)


# --- Parser -----------------------------------------------------------------


def _build_common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--project-id", default=argparse.SUPPRESS)
    p.add_argument("--no-redact", action="store_true", default=argparse.SUPPRESS)
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    p = GuardedArgumentParser(
        prog="request_upload.py",
        parents=[common],
        description="Draft a file-upload request artifact for review/execute.",
    )
    p.add_argument("alias")
    p.add_argument("--local", required=True,
                   help="Local file path (abs or relative)")
    p.add_argument("--remote", required=True,
                   help="Remote path; must resolve inside ~/workspace/<project_id>/")
    p.add_argument("--reason", required=True)
    p.add_argument("--overwrite", action="store_true",
                   help="Allow overwrite (auto-flags risk.irreversible)")
    p.add_argument("--confirm-sensitive-local-upload",
                   dest="confirm_sensitive_local_upload",
                   action="store_true",
                   help="Required when --local is outside upload_roots")
    p.add_argument("--expires-in", dest="expires_in", default="1h",
                   help="<n>{m,h,d}; min 60s, max 7d, default 1h")
    p.add_argument("--reports-dir", dest="reports_dir", default=None,
                   help="Override the state root (default: $SSH_GUARDED_HOME/reports)")
    add_version_flag(p)
    return p


# --- Helpers ---------------------------------------------------------------


def _sha256_streaming(path: Path, block: int = SHA256_BLOCK_BYTES) -> tuple[str, int]:
    """Stream-hash a local file. Returns ``(hex, bytes_read)``.

    Raises ``ArtifactError(sha256_compute_failed, 37)`` on IOError mid-stream.
    """
    h = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as fp:
            while True:
                chunk = fp.read(block)
                if not chunk:
                    break
                h.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ArtifactError(
            failure_class="sha256_compute_failed",
            exit_code=_EXIT_CODES["sha256_compute_failed"],
            message=f"failed to read {path} for sha256: {exc}",
        ) from exc
    return h.hexdigest(), size


def _upload_roots(cwd: Path) -> list[Path]:
    """The 4 hard-coded v1.0 upload roots (implement.md B4 + Open item 2)."""
    home = Path.home()
    return [
        (home / "Downloads").resolve(strict=False),
        (home / "Desktop").resolve(strict=False),
        cwd.resolve(strict=False),
        (home / "workspace").resolve(strict=False),
    ]


def _is_inside_any_root(p: Path, roots: list[Path]) -> bool:
    """Return True iff ``p`` is inside any of ``roots`` (after resolve)."""
    try:
        rp = p.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _resolve_local_path(value: str) -> Path:
    """Expand ``~`` + make absolute. Does NOT enforce existence."""
    return Path(value).expanduser().resolve(strict=False)


def _resolve_requests_dir(
    project_id: str,
    reports_dir: str | None = None,
) -> Path:
    """Return ``$SSH_GUARDED_HOME/reports/<project_id>/requests``."""
    return state_requests_dir(project_id, reports_dir=reports_dir)


# --- Main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    early = handle_version("request_upload.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        expires_seconds = parse_expires_in(args.expires_in)
        project_id = resolve_project_id(args, dict(os.environ), Path.cwd())

        local_path = _resolve_local_path(args.local)
        if not local_path.exists() or not local_path.is_file():
            raise ArtifactError(
                failure_class="local_path_missing",
                exit_code=_EXIT_CODES["local_path_missing"],
                message=f"--local {args.local!r} is not an existing regular file",
            )

        roots = _upload_roots(Path.cwd())
        inside_roots = _is_inside_any_root(local_path, roots)
        sensitive_local = not inside_roots
        if sensitive_local and not args.confirm_sensitive_local_upload:
            raise ArtifactError(
                failure_class="sensitive_local_unconfirmed",
                exit_code=_EXIT_CODES["sensitive_local_unconfirmed"],
                message=(
                    f"--local {str(local_path)!r} is outside upload_roots; "
                    "pass --confirm-sensitive-local-upload to acknowledge"
                ),
            )

        # sha256 stream — also gives us the authoritative size.
        sha_hex, byte_count = _sha256_streaming(local_path)
        if byte_count > MAX_UPLOAD_BYTES:
            raise ArtifactError(
                failure_class="size_limit_exceeded",
                exit_code=_EXIT_CODES["size_limit_exceeded"],
                message=(
                    f"--local size {byte_count} exceeds 256 MiB cap "
                    f"({MAX_UPLOAD_BYTES} bytes)"
                ),
            )

        normalized_remote = normalize_remote_path(args.remote)
        if not is_inside_sandbox(normalized_remote, project_id):
            sys.stderr.write(
                f"warning: --remote {normalized_remote!r} is outside "
                f"~/workspace/{project_id}/; executor will refuse "
                "(exit 50 workdir_outside_sandbox)\n"
            )

        payload: dict[str, Any] = {
            "local_path": str(local_path),
            "local_sha256": sha_hex,
            "local_size": byte_count,
            "remote_path": normalized_remote,
            "overwrite": bool(args.overwrite),
            "sensitive_local": sensitive_local,
        }

        risk = compute_risk(OPERATION, payload, args.alias)

        warn_legacy_reports()
        requests_dir = _resolve_requests_dir(
            project_id, getattr(args, "reports_dir", None)
        )
        request_id = allocate_request_id(OPERATION, requests_dir)
        artifact_path = requests_dir / f"{request_id}.json"

        artifact = RequestArtifact(
            version=1,
            request_id=request_id,
            operation=OPERATION,
            server=args.alias,
            reason=args.reason,
            created_at=iso_utc_now(),
            expires_at=iso_utc_offset(expires_seconds),
            payload=payload,
            risk_summary=risk,
        )
        artifact.save(artifact_path)

        envelope = success_envelope(
            stdout=(
                f"drafted {request_id} -> {artifact_path}\n"
                f"risk: {risk.blast_radius}\n"
            ),
            data={
                "artifact_path": str(artifact_path),
                "request_id": request_id,
                "operation": OPERATION,
                "server": args.alias,
                "expires_at": artifact.expires_at,
                "risk_summary": {
                    "irreversible": risk.irreversible,
                    "scope": risk.scope,
                    "blast_radius": risk.blast_radius,
                },
            },
        )
        emit(envelope, as_json)
        return 0
    except ArtifactError as exc:
        envelope = failure_envelope(
            failure_class=exc.failure_class,
            message=exc.message,
            exit_code=exc.exit_code,
            stderr=f"{exc.failure_class}: {exc.message}\n",
            data=None,
        )
        emit(envelope, as_json)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
