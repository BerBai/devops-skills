#!/usr/bin/env python3
"""request_command.py — draft a remote-command request (v1.0).

Produces an artifact under ``$SSH_GUARDED_HOME/reports/<project_id>/requests/``
(``command-<id>.json``). Does NOT execute. Hand the artifact to
``run_request.py --execute`` after human review.

v1.0 surface:
    request_command.py <alias> "<command>"
        --reason "<intent>"                  (required)
        [--workdir ~/workspace/<id>]
        [--timeout 60]                       (1..3600, default 60)
        [--expires-in 1h]                    (30m | 2h | 1d; default 1h; max 7d)
        [--env KEY=VAL ...]                  (repeatable; max 32 entries)
        [--project-id <id>]                  (SUPPRESS)
        [--reports-dir <path>]               (override the state root)
        [--json]                             (SUPPRESS)

Implementation notes:
- Common parser uses ``default=argparse.SUPPRESS`` (design D10) so the
  three shared flags read via ``getattr(args, "X", default)``.
- workdir outside the sandbox warns to stderr but the artifact is still
  written (C3 pedagogical: explicit refusal happens at execute time).
- ``risk_summary`` is computed by ``lib.compute_risk`` (D4).
- State lives outside the plugin tree (GAP-006): see ``lib/state.py``.

See:
    .trellis/tasks/05-25-ssh-guarded-request-pipeline/{prd,design,implement}.md
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

OPERATION = "command"

# Per-script failure_class table + exit codes (design §1 C5 / §6).
_EXIT_CODES: dict[str, int] = {
    "command_payload_invalid": 30,
    "reason_missing": 31,  # reserved; argparse covers the actual case
    "workdir_outside_sandbox": 32,
    "env_too_many": 33,
    "expiry_invalid": 34,
}

_HUMAN_HINTS: dict[str, str] = {
    "command_payload_invalid": "command payload invalid (empty command or --timeout out of [1, 3600])",
    "env_too_many": "--env may not be passed more than 32 times",
    "expiry_invalid": "--expires-in must be <int><m|h|d>; min 60s, max 7d",
    "workdir_outside_sandbox": "--workdir is outside ~/workspace/<project_id>/",
}

MAX_TIMEOUT_SECONDS = 3600
MAX_ENV_ENTRIES = 32


# --- Parser -----------------------------------------------------------------


def _build_common_parser() -> argparse.ArgumentParser:
    """Shared flags (design D10) — SUPPRESS so parents don't clobber subparsers."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.add_argument("--project-id", default=argparse.SUPPRESS)
    p.add_argument("--no-redact", action="store_true", default=argparse.SUPPRESS)
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _build_common_parser()
    p = GuardedArgumentParser(
        prog="request_command.py",
        parents=[common],
        description="Draft a remote-command request artifact for review/execute.",
    )
    p.add_argument("alias", help="Host alias (must exist in ~/.ssh/config when executed)")
    p.add_argument("command", help="Remote command to run")
    p.add_argument("--reason", required=True, help="One-line intent (required)")
    p.add_argument("--workdir", default=None,
                   help="Remote workdir; must resolve inside ~/workspace/<project_id>/")
    p.add_argument("--timeout", type=int, default=60,
                   help="Per-command timeout in seconds (1..3600, default 60)")
    p.add_argument("--expires-in", dest="expires_in", default="1h",
                   help="<n>{m,h,d}; min 60s, max 7d, default 1h")
    p.add_argument("--env", action="append", default=[],
                   help="KEY=VAL (repeatable; max 32 entries)")
    p.add_argument("--reports-dir", dest="reports_dir", default=None,
                   help="Override the state root (default: $SSH_GUARDED_HOME/reports)")
    add_version_flag(p)
    return p


# --- Helpers ---------------------------------------------------------------


def _parse_env_entries(entries: list[str]) -> dict[str, str]:
    """Parse ``KEY=VAL`` entries into a dict.

    Empty list → ``{}``. Missing ``=`` separator → exit 30
    ``command_payload_invalid``. Empty key → same.
    """
    if len(entries) > MAX_ENV_ENTRIES:
        raise ArtifactError(
            failure_class="env_too_many",
            exit_code=_EXIT_CODES["env_too_many"],
            message=f"--env count {len(entries)} exceeds cap {MAX_ENV_ENTRIES}",
        )
    out: dict[str, str] = {}
    for kv in entries:
        if "=" not in kv:
            raise ArtifactError(
                failure_class="command_payload_invalid",
                exit_code=_EXIT_CODES["command_payload_invalid"],
                message=f"--env value not in KEY=VAL form: {kv!r}",
            )
        key, val = kv.split("=", 1)
        if not key:
            raise ArtifactError(
                failure_class="command_payload_invalid",
                exit_code=_EXIT_CODES["command_payload_invalid"],
                message=f"--env entry has empty key: {kv!r}",
            )
        out[key] = val
    return out


def _validate_command_args(args: argparse.Namespace) -> None:
    """Pre-payload sanity checks (raises ArtifactError on failure)."""
    if not args.command or not args.command.strip():
        raise ArtifactError(
            failure_class="command_payload_invalid",
            exit_code=_EXIT_CODES["command_payload_invalid"],
            message="command is empty",
        )
    if args.timeout < 1 or args.timeout > MAX_TIMEOUT_SECONDS:
        raise ArtifactError(
            failure_class="command_payload_invalid",
            exit_code=_EXIT_CODES["command_payload_invalid"],
            message=f"--timeout {args.timeout} not in [1, {MAX_TIMEOUT_SECONDS}]",
        )


def _resolve_requests_dir(
    project_id: str,
    reports_dir: str | None = None,
) -> Path:
    """Return ``$SSH_GUARDED_HOME/reports/<project_id>/requests``."""
    return state_requests_dir(project_id, reports_dir=reports_dir)


# --- Main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    early = handle_version("request_command.py", argv)
    if early is not None:
        return early
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        _validate_command_args(args)
        env_dict = _parse_env_entries(args.env)
        expires_seconds = parse_expires_in(args.expires_in)
        project_id = resolve_project_id(args, dict(os.environ), Path.cwd())

        # workdir warn-but-write (C3): outside sandbox → stderr warn,
        # still proceed with artifact write so user sees executor refusal.
        workdir_raw = args.workdir
        if workdir_raw is not None:
            # Normalize for the artifact + display; reject only on path-shape
            # errors (null, escape) which the lib turns into ArtifactError.
            normalized_workdir = normalize_remote_path(workdir_raw)
            if not is_inside_sandbox(normalized_workdir, project_id):
                sys.stderr.write(
                    f"warning: --workdir {normalized_workdir!r} is outside "
                    f"~/workspace/{project_id}/; executor will refuse "
                    "(exit 50 workdir_outside_sandbox)\n"
                )
            workdir_for_payload: str | None = normalized_workdir
        else:
            workdir_for_payload = None

        payload: dict[str, Any] = {
            "command": args.command,
            "timeout": args.timeout,
        }
        if workdir_for_payload is not None:
            payload["workdir"] = workdir_for_payload
        if env_dict:
            payload["env"] = env_dict

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
