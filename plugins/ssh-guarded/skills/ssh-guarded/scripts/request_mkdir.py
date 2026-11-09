#!/usr/bin/env python3
"""request_mkdir.py — draft a remote mkdir request (v1.0).

Produces an artifact under ``$SSH_GUARDED_HOME/reports/<project_id>/requests/``
(``mkdir-<id>.json``). Does NOT execute. Hand the artifact to
``run_request.py --execute`` after human review.

v1.0 surface:
    request_mkdir.py <alias>
        --remote ~/workspace/<id>/staging/2026-05-14
        --reason "stage release artifacts before deploy"
        [--mode 0755]                       (octal; 0o0000..0o0777)
        [--parents]                         (mkdir -p semantics)
        [--expires-in 1h]
        [--project-id <id>]                 (SUPPRESS)
        [--reports-dir <path>]              (override the state root)
        [--json]                            (SUPPRESS)

Payload (operation="mkdir"):
    { "remote_path": "...", "mode": "0755", "parents": true }

Implementation notes:
- ``--parents`` heuristic: drafter does NOT stat the remote. Path
  segments relative to the sandbox root ``~/workspace/<project_id>/`` are
  counted; >1 → ``--parents`` required, else exit 42.
- ``remote`` outside the sandbox warns to stderr but the artifact is
  still written (C3 pedagogical: executor refuses with exit 50).
- ``risk_summary`` is computed by ``lib.compute_risk`` (D4 mkdir row).
- State lives outside the plugin tree (GAP-006): see ``lib/state.py``.
"""

from __future__ import annotations

import argparse
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

# --- Constants --------------------------------------------------------------

OPERATION = "mkdir"

_EXIT_CODES: dict[str, int] = {
    "mode_invalid": 40,
    "remote_path_outside_sandbox": 41,  # reserved (warn-but-write at draft)
    "parents_required_for_nested": 42,
}

_HUMAN_HINTS: dict[str, str] = {
    "mode_invalid": "--mode must be octal in 0o0000..0o0777 (e.g. 0755)",
    "parents_required_for_nested": "remote path has multiple missing parents; pass --parents",
}

# Mode bounds: 0o0000..0o0777 (no setuid/setgid/sticky in this v1.0).
MIN_MODE = 0o0000
MAX_MODE = 0o0777


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
        prog="request_mkdir.py",
        parents=[common],
        description="Draft a remote mkdir request artifact for review/execute.",
    )
    p.add_argument("alias")
    p.add_argument("--remote", required=True,
                   help="Remote path; must resolve inside ~/workspace/<project_id>/")
    p.add_argument("--reason", required=True, help="One-line intent (required)")
    p.add_argument("--mode", default="0755",
                   help="Octal mode (default 0755; 0o0000..0o0777)")
    p.add_argument("--parents", action="store_true",
                   help="mkdir -p semantics; required if >1 missing parent")
    p.add_argument("--expires-in", dest="expires_in", default="1h",
                   help="<n>{m,h,d}; min 60s, max 7d, default 1h")
    p.add_argument("--reports-dir", dest="reports_dir", default=None,
                   help="Override the state root (default: $SSH_GUARDED_HOME/reports)")
    add_version_flag(p)
    return p


# --- Helpers ---------------------------------------------------------------


def _parse_mode(s: str) -> int:
    try:
        m = int(s, 8)
    except (TypeError, ValueError) as exc:
        raise ArtifactError(
            failure_class="mode_invalid",
            exit_code=_EXIT_CODES["mode_invalid"],
            message=f"--mode {s!r} is not a valid octal value",
        ) from exc
    if m < MIN_MODE or m > MAX_MODE:
        raise ArtifactError(
            failure_class="mode_invalid",
            exit_code=_EXIT_CODES["mode_invalid"],
            message=f"--mode {s!r} ({m:o}) not in 0o0000..0o0777",
        )
    return m


def _sandbox_segment_count(normalized: str, project_id: str) -> int:
    """Count path segments under ``~/workspace/<project_id>/`` (0 if not in sandbox)."""
    root = f"~/workspace/{project_id}"
    if normalized == root:
        return 0
    if not normalized.startswith(root + "/"):
        return 0
    suffix = normalized[len(root) + 1:]
    return len([seg for seg in suffix.split("/") if seg])


def _resolve_requests_dir(
    project_id: str,
    reports_dir: str | None = None,
) -> Path:
    """Return ``$SSH_GUARDED_HOME/reports/<project_id>/requests``."""
    return state_requests_dir(project_id, reports_dir=reports_dir)


# --- Main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    early = handle_version("request_mkdir.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        mode_int = _parse_mode(args.mode)
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
        else:
            # Only enforce the --parents heuristic for in-sandbox paths;
            # out-of-sandbox is going to be refused by the executor anyway.
            seg_count = _sandbox_segment_count(normalized_remote, project_id)
            if seg_count > 1 and not args.parents:
                raise ArtifactError(
                    failure_class="parents_required_for_nested",
                    exit_code=_EXIT_CODES["parents_required_for_nested"],
                    message=(
                        f"--remote has {seg_count} segments below sandbox root; "
                        "pass --parents to allow mkdir -p"
                    ),
                )

        # Store mode as the canonical octal string with leading 0 so the
        # executor can pass it to `mkdir -m` directly.
        mode_str = f"{mode_int:04o}"

        payload: dict[str, Any] = {
            "remote_path": normalized_remote,
            "mode": mode_str,
            "parents": bool(args.parents),
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
