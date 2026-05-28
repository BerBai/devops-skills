"""Shared helpers for ssh-guarded scripts.

Stable surface (re-exported here so scripts say ``from lib import X``):

* sandbox: ``resolve_project_id``, ``is_inside_sandbox``, ``is_in_denylist``,
  ``normalize_remote_path``
* redact: ``Redactor``
* lockfile: ``exclusive_lock``, ``LockHeld``, ``LockInfo``
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
from .lockfile import LockHeld, LockInfo, exclusive_lock
from .redact import Redactor
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

__all__ = [
    "SSH_CORE_SCRIPTS_DIR",
    "ArtifactError",
    "LockHeld",
    "LockInfo",
    "Redactor",
    "RequestArtifact",
    "RiskSummary",
    "allocate_request_id",
    "compute_risk",
    "exclusive_lock",
    "is_in_denylist",
    "is_inside_sandbox",
    "iso_utc_now",
    "iso_utc_offset",
    "normalize_remote_path",
    "parse_expires_in",
    "resolve_project_id",
    "save_result",
]
