#!/usr/bin/env python3
"""request_delete.py — draft a remote delete request (v1.0).

The most dangerous of the request_* family. Risk summary is mandatory
and ``irreversible=true`` is hard-coded for delete. Recursive deletes
get the ``tree`` scope.

v1.0 surface:
    request_delete.py <alias>
        --remote ~/workspace/<id>/stale.log
        --reason "log older than retention policy"
        [--recursive]                       (required if remote path is dir)
        [--expires-in 1h]
        [--project-id <id>]                 (SUPPRESS)
        [--json]                            (SUPPRESS)

Payload (operation="delete"):
    { "remote_path": "...", "recursive": bool, "is_directory_pre_check": bool }

Implementation notes:
- Drafter runs a one-shot ``ssh_execute`` round-trip to test ``-d``/``-e``
  on the remote (design D6 B3 drift — replaces direct paramiko SftpSession
  import, design §7 forbids cross-plugin imports). Network/auth failure
  → exit 49 ``pre_check_stat_failed``. Result ``"dir"`` and
  ``--recursive`` missing → exit 46 ``recursive_required_for_directory``.
- Denylist match warns + writes (C3 pedagogical: explicit refusal at
  execute time).
- ``risk_summary.irreversible`` is ALWAYS true for delete (D4).
- ``risk_summary.scope`` is ``tree`` iff ``--recursive`` else ``single-file``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import (  # noqa: E402, I001
    SSH_CORE_SCRIPTS_DIR,
    ArtifactError,
    RequestArtifact,
    allocate_request_id,
    compute_risk,
    is_in_denylist,
    is_inside_sandbox,
    iso_utc_now,
    iso_utc_offset,
    normalize_remote_path,
    parse_expires_in,
    resolve_project_id,
)

# --- Constants --------------------------------------------------------------

OPERATION = "delete"

_EXIT_CODES: dict[str, int] = {
    "remote_path_outside_sandbox": 45,
    "recursive_required_for_directory": 46,
    "path_in_denylist": 47,  # reserved; drafter only warns
    "pre_check_stat_failed": 49,
}

_HUMAN_HINTS: dict[str, str] = {
    "recursive_required_for_directory": "remote is a directory; pass --recursive to delete",
    "pre_check_stat_failed": "drafter could not stat remote path (network/auth/permission)",
}

PRE_CHECK_TIMEOUT_SECONDS = 30


# --- Parser -----------------------------------------------------------------


def _build_common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--project-id", default=argparse.SUPPRESS)
    p.add_argument("--no-redact", action="store_true", default=argparse.SUPPRESS)
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    p = argparse.ArgumentParser(
        prog="request_delete.py",
        parents=[common],
        description="Draft a remote delete request artifact for review/execute.",
    )
    p.add_argument("alias")
    p.add_argument("--remote", required=True,
                   help="Remote path; must resolve inside ~/workspace/<project_id>/")
    p.add_argument("--reason", required=True)
    p.add_argument("--recursive", action="store_true",
                   help="Required if the remote path is a directory")
    p.add_argument("--expires-in", dest="expires_in", default="1h",
                   help="<n>{m,h,d}; min 60s, max 7d, default 1h")
    return p


# --- Remote stat via ssh_execute (B3 drift) ---------------------------------


def _remote_stat_kind(alias: str, remote_path: str) -> str:
    """Return ``"dir"`` | ``"file"`` | ``"none"`` for ``remote_path`` on ``alias``.

    Implemented via a single ``ssh_execute.py`` subprocess: we run a
    POSIX-shell expression that prints one of those three literals.
    Subprocess or transport failure raises ``ArtifactError`` with
    ``pre_check_stat_failed``/exit 49.
    """
    ssh_execute = SSH_CORE_SCRIPTS_DIR / "ssh_execute.py"
    q = shlex.quote(remote_path)
    # Print exactly one of dir/file/none. The wrapping is safe because we
    # only ever quote remote_path once.
    remote_cmd = (
        f"if [ -d {q} ]; then echo dir; "
        f"elif [ -e {q} ]; then echo file; "
        f"else echo none; fi"
    )
    argv = [
        sys.executable,
        str(ssh_execute),
        alias,
        remote_cmd,
        "--json",
        "--timeout",
        str(PRE_CHECK_TIMEOUT_SECONDS),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=PRE_CHECK_TIMEOUT_SECONDS + 5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise ArtifactError(
            failure_class="pre_check_stat_failed",
            exit_code=_EXIT_CODES["pre_check_stat_failed"],
            message=f"ssh_execute pre-check failed: {exc}",
        ) from exc
    if proc.returncode != 0:
        raise ArtifactError(
            failure_class="pre_check_stat_failed",
            exit_code=_EXIT_CODES["pre_check_stat_failed"],
            message=(
                f"ssh_execute pre-check returned {proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}"
            ),
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            failure_class="pre_check_stat_failed",
            exit_code=_EXIT_CODES["pre_check_stat_failed"],
            message=f"ssh_execute pre-check returned non-JSON output: {exc}",
        ) from exc
    if not envelope.get("success"):
        # Remote shell failure or ssh transport problem.
        raise ArtifactError(
            failure_class="pre_check_stat_failed",
            exit_code=_EXIT_CODES["pre_check_stat_failed"],
            message=(
                "ssh_execute pre-check non-success: "
                f"{(envelope.get('stderr') or '').strip()[:200]}"
            ),
        )
    out = (envelope.get("stdout") or "").strip().splitlines()
    # Take the last non-empty line so any motd noise gets skipped.
    last = next((ln.strip() for ln in reversed(out) if ln.strip()), "")
    if last not in ("dir", "file", "none"):
        raise ArtifactError(
            failure_class="pre_check_stat_failed",
            exit_code=_EXIT_CODES["pre_check_stat_failed"],
            message=f"unexpected stat result: {last!r}",
        )
    return last


# --- Helpers ---------------------------------------------------------------


def _resolve_requests_dir() -> Path:
    skill_root = _THIS.parent.parent
    return skill_root / "reports" / "requests"


def _emit(envelope: dict[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(envelope, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if envelope["success"]:
        data = envelope["data"]
        sys.stdout.write(
            f"drafted {data['request_id']} -> {data['artifact_path']}\n"
            f"risk: {data['risk_summary']['blast_radius']}\n"
        )
    else:
        err = envelope["error"]
        sys.stderr.write(f"{err['failure_class']}: {err['message']}\n")


# --- Main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        expires_seconds = parse_expires_in(args.expires_in)
        project_id = resolve_project_id(args, dict(os.environ), Path.cwd())

        normalized_remote = normalize_remote_path(args.remote)
        in_sandbox = is_inside_sandbox(normalized_remote, project_id)
        if not in_sandbox:
            sys.stderr.write(
                f"warning: --remote {normalized_remote!r} is outside "
                f"~/workspace/{project_id}/; executor will refuse "
                "(exit 50 workdir_outside_sandbox)\n"
            )
        if is_in_denylist(normalized_remote):
            sys.stderr.write(
                f"warning: --remote {normalized_remote!r} matches denylist; "
                "executor will refuse (exit 51 path_in_denylist)\n"
            )

        # Pre-check remote via ssh_execute. We still pre-check even when
        # out-of-sandbox / denylist hits, so the artifact carries the
        # accurate is_directory_pre_check value. Failures bubble as exit 49.
        kind = _remote_stat_kind(args.alias, normalized_remote)
        if kind == "none":
            sys.stderr.write(
                f"note: --remote {normalized_remote!r} does not exist on "
                f"{args.alias} at draft time; rm -f will be a no-op at execute\n"
            )
        if kind == "dir" and not args.recursive:
            raise ArtifactError(
                failure_class="recursive_required_for_directory",
                exit_code=_EXIT_CODES["recursive_required_for_directory"],
                message=(
                    f"remote {normalized_remote!r} is a directory on "
                    f"{args.alias}; pass --recursive"
                ),
            )

        payload: dict[str, Any] = {
            "remote_path": normalized_remote,
            "recursive": bool(args.recursive),
            "is_directory_pre_check": (kind == "dir"),
        }

        risk = compute_risk(OPERATION, payload, args.alias)
        # Sanity: delete is always irreversible by lib contract.
        assert risk.irreversible is True

        requests_dir = _resolve_requests_dir()
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

        envelope = {
            "success": True,
            "data": {
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
            "error": None,
        }
        _emit(envelope, as_json)
        return 0
    except ArtifactError as exc:
        envelope = {
            "success": False,
            "data": None,
            "error": {
                "message": exc.message,
                "failure_class": exc.failure_class,
                "exit_code": exc.exit_code,
            },
        }
        _emit(envelope, as_json)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
