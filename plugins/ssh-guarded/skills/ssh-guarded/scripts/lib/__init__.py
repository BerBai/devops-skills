"""Shared helpers for ssh-guarded scripts.

Stable surface (re-exported here so scripts say ``from lib import X``):

* envelope: ``json_envelope``, ``success_envelope``, ``failure_envelope``,
  ``error_object``, ``emit``, ``write_json``, ``GuardedArgumentParser``
* state: ``state_home``, ``reports_root``, ``requests_dir``, ``jobs_dir``,
  ``project_ids``, ``legacy_reports_root``, ``legacy_artifact_count``,
  ``warn_legacy_reports``
* remote_path: ``resolve_remote_home``, ``expand_remote_tilde``, ``uses_tilde``
* sandbox: ``resolve_project_id``, ``is_inside_sandbox``, ``is_in_denylist``,
  ``normalize_remote_path``
* redact: ``Redactor``
* lockfile: ``exclusive_lock``, ``LockHeld``, ``LockInfo``
* gc: ``gc_reports``, ``parse_age``, ``GcResult``, ``GcEntry``
* request_artifact: ``ArtifactError``, ``RequestArtifact``, ``RiskSummary``,
  ``allocate_request_id``, ``parse_expires_in``, ``save_result``,
  ``compute_risk``, ``iso_utc_now``, ``iso_utc_offset``,
  ``SSH_CORE_SCRIPTS_DIR``

Anything not listed here is an implementation detail and may move between
versions. Mirrors the ssh-core lib pattern at
``plugins/ssh-core/skills/ssh-core/scripts/lib/__init__.py``.
"""

from __future__ import annotations

from ._errors import ArtifactError
from .envelope import (
    GuardedArgumentParser,
    emit,
    error_object,
    failure_envelope,
    json_envelope,
    success_envelope,
    write_json,
)
from .gc import GcEntry, GcResult, gc_reports, parse_age
from .lockfile import LockHeld, LockInfo, exclusive_lock
from .redact import Redactor
from .remote_path import expand_remote_tilde, resolve_remote_home, uses_tilde
from .request_artifact import (
    SSH_CORE_SCRIPTS_DIR,
    RequestArtifact,
    RiskSummary,
    allocate_request_id,
    compute_risk,
    iso_utc_now,
    iso_utc_offset,
    parse_expires_in,
    save_result,
)
from .sandbox import (
    is_in_denylist,
    is_inside_sandbox,
    normalize_remote_path,
    resolve_project_id,
)
from .state import (
    jobs_dir,
    legacy_artifact_count,
    legacy_reports_root,
    project_ids,
    reports_root,
    requests_dir,
    state_home,
    warn_legacy_reports,
)
from .version_cli import add_version_flag, handle_version, version_line

__all__ = [
    "SSH_CORE_SCRIPTS_DIR",
    "ArtifactError",
    "GcEntry",
    "GcResult",
    "GuardedArgumentParser",
    "gc_reports",
    "parse_age",
    "LockHeld",
    "LockInfo",
    "Redactor",
    "RequestArtifact",
    "RiskSummary",
    "add_version_flag",
    "allocate_request_id",
    "compute_risk",
    "emit",
    "error_object",
    "exclusive_lock",
    "expand_remote_tilde",
    "failure_envelope",
    "handle_version",
    "is_in_denylist",
    "is_inside_sandbox",
    "iso_utc_now",
    "iso_utc_offset",
    "jobs_dir",
    "json_envelope",
    "legacy_artifact_count",
    "legacy_reports_root",
    "normalize_remote_path",
    "parse_expires_in",
    "project_ids",
    "reports_root",
    "requests_dir",
    "resolve_project_id",
    "resolve_remote_home",
    "save_result",
    "state_home",
    "success_envelope",
    "uses_tilde",
    "version_line",
    "warn_legacy_reports",
    "write_json",
]
