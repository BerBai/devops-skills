"""Request artifact dataclass + schema validation for ssh-guarded.

Stable surface::

    class ArtifactError(Exception)                       # re-exported from _errors
    @dataclass class RiskSummary
    @dataclass class RequestArtifact
        - RequestArtifact.load(path) -> RequestArtifact
        - art.save(path) -> None                          # atomic, mode 0o600
        - art.validate(*, project_id, expected_server=None) -> None

    allocate_request_id(operation, requests_dir) -> str   # uuid4().hex[:8]
    parse_expires_in(s: str | None) -> int                # seconds; 60s floor + 7d cap
    save_result(art, result_dict, redactor) -> None       # atomic <id>.result.json
    SSH_CORE_SCRIPTS_DIR                                  # Path constant, monkeypatchable

Design source: design.md §1 C1/C2/C3/C4/C5/C9 + §3 D1/D3/D4/D5/D7/D11 + §4.4.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

from . import sandbox
from ._errors import ArtifactError
from ._ssh_core_locator import ssh_core_script as _ssh_core_script
from .redact import Redactor

# ---- Constants -----------------------------------------------------------

Operation = Literal["command", "upload", "mkdir", "delete"]
Scope = Literal["single-file", "tree", "service", "host"]

_OPERATIONS: tuple[str, ...] = get_args(Operation)
_SCOPES: tuple[str, ...] = get_args(Scope)

SCHEMA_VERSION = 1

# Project default expiry: 1h. Floors and caps from D11.
DEFAULT_EXPIRES_SECONDS = 3600
MIN_EXPIRES_SECONDS = 60
MAX_EXPIRES_SECONDS = 7 * 24 * 3600  # 7 days

# 256 MiB hard cap for upload payloads (PRD non-functional + D5).
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

# Filesystem mode for artifact / result / placeholder files.
ARTIFACT_FILE_MODE = 0o600

# Path to ssh-core scripts dir, used by run_request to dispatch.
# Monkeypatched in unit tests; see design.md §3 D8. Resolved through the shared
# locator so DEVOPS_SSH_CORE_DIR is honoured and the marketplace layout (each
# plugin installed into its own directory) keeps working.
SSH_CORE_SCRIPTS_DIR: Path = _ssh_core_script("ssh_execute.py").parent

# ---- Expiry parsing (D11) ------------------------------------------------

_EXPIRES_RE = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_expires_in(s: str | None) -> int:
    """Parse ``--expires-in`` value to seconds.

    Accepts ``<int><unit>`` literals where unit is one of ``m``/``h``/``d``.
    Defaults to 1h. Below 60s → raises ``expiry_invalid``. Above 7d → same.
    """
    if s is None:
        return DEFAULT_EXPIRES_SECONDS
    cleaned = s.strip()
    m = _EXPIRES_RE.fullmatch(cleaned)
    if not m:
        raise ArtifactError(
            failure_class="expiry_invalid",
            exit_code=34,
            message=f"--expires-in: '{s}' not in <n><m|h|d> form",
        )
    n, unit = int(m.group(1)), m.group(2)
    seconds = n * _UNIT_SECONDS[unit]
    if seconds < MIN_EXPIRES_SECONDS:
        raise ArtifactError(
            failure_class="expiry_invalid",
            exit_code=34,
            message=f"--expires-in: '{s}' below 60s floor",
        )
    if seconds > MAX_EXPIRES_SECONDS:
        raise ArtifactError(
            failure_class="expiry_invalid",
            exit_code=34,
            message=f"--expires-in: '{s}' exceeds 7d cap",
        )
    return seconds


# ---- Atomic write helper -------------------------------------------------


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path`` atomically with mode 0o600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
                fp.write("\n")
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        # chmod is best-effort on Windows (NTFS ACL); same posture as
        # ssh_config_manager atomic_write_with_backup (ADR-002 D4).
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, ARTIFACT_FILE_MODE)
        os.replace(tmp_path, path)
    finally:
        # If os.replace succeeded the tmp file is gone; otherwise clean it.
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()


# ---- Dataclasses ---------------------------------------------------------


@dataclass
class RiskSummary:
    """Drafter-computed risk classification.

    Users / agents cannot override these fields post-write; ``run_request``
    re-reads them as policy input (delete + irreversible=true forces
    ``--confirm-delete``).
    """

    irreversible: bool
    scope: str  # one of Scope literals
    blast_radius: str


@dataclass
class RequestArtifact:
    """The JSON artifact on disk under ``<reports>/<project_id>/requests/<op>-<id>.json``.

    The reports root is ``$SSH_GUARDED_HOME/reports`` (or ``--reports-dir``),
    never the plugin tree — see :mod:`lib.state` (DEVOPS-GAP-006).
    """

    version: int
    request_id: str
    operation: str  # one of Operation literals
    server: str
    reason: str
    created_at: str
    expires_at: str
    payload: dict[str, Any]
    risk_summary: RiskSummary

    # ----- factory + load ----------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> RequestArtifact:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact not found: {path}",
            ) from exc
        except OSError as exc:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"cannot read artifact {path}: {exc}",
            ) from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} is not valid JSON: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} top-level is not an object",
            )
        if data.get("version") != SCHEMA_VERSION:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=(
                    f"artifact {path} has unsupported version "
                    f"{data.get('version')!r} (expected {SCHEMA_VERSION})"
                ),
            )
        required: tuple[str, ...] = (
            "request_id",
            "operation",
            "server",
            "reason",
            "created_at",
            "expires_at",
            "payload",
            "risk_summary",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} missing fields: {', '.join(missing)}",
            )

        op_val = data["operation"]
        if op_val not in _OPERATIONS:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} has unknown operation '{op_val}'",
            )
        if not isinstance(data["payload"], dict):
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} payload is not an object",
            )
        if not isinstance(data["risk_summary"], dict):
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} risk_summary is not an object",
            )
        rs_raw = data["risk_summary"]
        try:
            risk = RiskSummary(
                irreversible=bool(rs_raw["irreversible"]),
                scope=str(rs_raw["scope"]),
                blast_radius=str(rs_raw["blast_radius"]),
            )
        except KeyError as exc:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} risk_summary missing field: {exc}",
            ) from exc
        if risk.scope not in _SCOPES:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"artifact {path} risk_summary.scope '{risk.scope}' is unknown",
            )
        return cls(
            version=int(data["version"]),
            request_id=str(data["request_id"]),
            operation=str(op_val),
            server=str(data["server"]),
            reason=str(data["reason"]),
            created_at=str(data["created_at"]),
            expires_at=str(data["expires_at"]),
            payload=dict(data["payload"]),
            risk_summary=risk,
        )

    # ----- save --------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Ensure key order for human readability (version first).
        return {
            "version": d["version"],
            "request_id": d["request_id"],
            "operation": d["operation"],
            "server": d["server"],
            "reason": d["reason"],
            "created_at": d["created_at"],
            "expires_at": d["expires_at"],
            "payload": d["payload"],
            "risk_summary": d["risk_summary"],
        }

    def save(self, path: Path) -> None:
        """Atomic write of this artifact to ``path`` (mode 0o600)."""
        _atomic_write_json(path, self.to_dict())

    # ----- validate ----------------------------------------------------------

    def validate(
        self,
        *,
        project_id: str,
        expected_server: str | None = None,
    ) -> None:
        """Run the locked validation order (schema → expiry → server → denylist → sandbox).

        SHA-256 verify and lock acquisition are intentionally out of scope
        for this method (see design.md §3 D1 docstring).

        Denylist runs before sandbox so paths like ``~/.ssh/authorized_keys``
        — which are outside the workspace AND match a denied prefix —
        surface as ``path_in_denylist`` (exit 51) rather than the more
        generic ``workdir_outside_sandbox`` (exit 50). Denylist is the
        "absolute red line" (specific dangerous files), sandbox the
        "default boundary"; the red line takes precedence so audit logs
        attribute the refusal to the rule that exists specifically to
        catch it (design.md §3 D7 drift, 2026-05-28).
        """
        # 1. schema is already enforced by load(); here we additionally
        #    sanity-check expires_at parseability.
        try:
            expires_dt = _parse_iso(self.expires_at)
        except ValueError as exc:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=f"expires_at is not parseable ISO8601: {self.expires_at}",
            ) from exc

        # 2. expiry
        if datetime.now(timezone.utc) >= expires_dt:
            raise ArtifactError(
                failure_class="artifact_expired",
                exit_code=53,
                message=f"artifact expired at {self.expires_at}",
            )

        # 3. server consistency
        if expected_server is not None and expected_server != self.server:
            raise ArtifactError(
                failure_class="artifact_invalid",
                exit_code=53,
                message=(
                    f"server mismatch: --server {expected_server!r} != "
                    f"artifact.server {self.server!r}"
                ),
            )

        # 4. denylist  → exit 51  (red line, checked first)
        # 5. sandbox   → exit 50  (default boundary)
        check_paths = self._paths_to_check()
        for path_value in check_paths:
            if sandbox.is_in_denylist(path_value):
                raise ArtifactError(
                    failure_class="path_in_denylist",
                    exit_code=51,
                    message=f"path '{path_value}' matches denylist",
                )
            if not sandbox.is_inside_sandbox(path_value, project_id):
                raise ArtifactError(
                    failure_class="workdir_outside_sandbox",
                    exit_code=50,
                    message=(
                        f"path '{path_value}' is outside "
                        f"~/workspace/{project_id}/"
                    ),
                )

    def _paths_to_check(self) -> list[str]:
        op = self.operation
        out: list[str] = []
        if op == "command":
            workdir = self.payload.get("workdir")
            if workdir:
                out.append(str(workdir))
        elif op in ("upload", "mkdir", "delete"):
            remote = self.payload.get("remote_path")
            if remote:
                out.append(str(remote))
        return out


# ---- ISO8601 helpers ------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """Parse a strict ISO8601 UTC string; ``Z`` suffix accepted."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_utc_now() -> str:
    """Return current UTC time as an ISO8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_utc_offset(seconds: int) -> str:
    """Return ``now + seconds`` as an ISO8601 UTC string."""
    base = datetime.now(timezone.utc)
    delta = base.timestamp() + seconds
    out = datetime.fromtimestamp(delta, tz=timezone.utc)
    return out.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- request_id allocation (D3) -------------------------------------------


def allocate_request_id(operation: str, requests_dir: Path) -> str:
    """Allocate a unique request_id and create an ``O_EXCL`` placeholder file.

    The placeholder is the artifact path itself (``<op>-<id>.json``); the
    caller subsequently overwrites it atomically via ``RequestArtifact.save``.

    Three retries on collision. Three failures → ``ArtifactError(exit_code=1)``.
    """
    if operation not in _OPERATIONS:
        raise ArtifactError(
            failure_class="artifact_invalid",
            exit_code=53,
            message=f"unknown operation for id allocation: {operation!r}",
        )
    requests_dir.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for _ in range(3):
        rid_short = uuid.uuid4().hex[:8]
        rid_full = f"{operation}-{rid_short}"
        path = requests_dir / f"{rid_full}.json"
        try:
            fd = os.open(
                str(path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                ARTIFACT_FILE_MODE,
            )
        except FileExistsError as exc:
            last_exc = exc
            continue
        os.close(fd)
        return rid_full
    raise ArtifactError(
        failure_class="request_id_exhausted",
        exit_code=1,
        message=(
            "unable to allocate request_id after 3 attempts "
            f"(last={last_exc!r})"
        ),
    )


# ---- risk computation (D4) ------------------------------------------------

# Regex set for command risk-summary irreversible heuristic.
_RISK_RM_RE = re.compile(r"\brm\b")
_RISK_MV_RE = re.compile(r"\bmv\b")
_RISK_REDIRECT_RE = re.compile(r">[^>]")
_RISK_SYSTEMCTL_RE = re.compile(r"\bsystemctl\b")
_RISK_SUDO_RE = re.compile(r"\bsudo\b")


def _command_risk(payload: dict[str, Any], server: str) -> RiskSummary:
    command = str(payload.get("command", ""))
    workdir = payload.get("workdir") or "default workdir"
    user = os.getenv("USER") or "unknown"
    has_sudo = bool(_RISK_SUDO_RE.search(command))
    has_systemctl = bool(_RISK_SYSTEMCTL_RE.search(command))
    has_rm = bool(_RISK_RM_RE.search(command))
    has_mv = bool(_RISK_MV_RE.search(command))
    has_redirect = bool(_RISK_REDIRECT_RE.search(command))
    irreversible = has_sudo or has_systemctl or has_rm or has_mv or has_redirect
    if has_sudo:
        scope = "host"
    elif has_systemctl:
        scope = "service"
    elif has_rm or has_mv:
        scope = "tree"
    else:
        scope = "single-file"
    snippet = command[:60]
    blast = f"runs '{snippet}' as {user}@{server} in {workdir}"
    return RiskSummary(irreversible=irreversible, scope=scope, blast_radius=blast)


def _upload_risk(payload: dict[str, Any], server: str) -> RiskSummary:
    overwrite = bool(payload.get("overwrite", False))
    size = payload.get("local_size", 0)
    remote = payload.get("remote_path", "?")
    blast = (
        f"writes {size} bytes to {remote} on {server}; overwrite={overwrite}"
    )
    return RiskSummary(
        irreversible=overwrite,
        scope="single-file",
        blast_radius=blast,
    )


def _mkdir_risk(payload: dict[str, Any], server: str) -> RiskSummary:
    remote = payload.get("remote_path", "?")
    mode = payload.get("mode", "0755")
    blast = f"creates {remote} (mode {mode}) on {server}"
    return RiskSummary(
        irreversible=False,
        scope="single-file",
        blast_radius=blast,
    )


def _delete_risk(payload: dict[str, Any], server: str) -> RiskSummary:
    remote = payload.get("remote_path", "?")
    recursive = bool(payload.get("recursive", False))
    # BUG-001: a path the drafter *knows* is a directory is a tree even if the
    # caller did not pass --recursive. Reporting single-file for a directory
    # made the risk summary — the artifact a human reviews — wrong, which is
    # the whole reason the --recursive gate exists.
    is_directory = bool(payload.get("is_directory_pre_check", False))
    scope = "tree" if (recursive or is_directory) else "single-file"
    blast = f"deletes {remote} on {server}; recursive={recursive}"
    return RiskSummary(
        irreversible=True,
        scope=scope,
        blast_radius=blast,
    )


def compute_risk(operation: str, payload: dict[str, Any], server: str) -> RiskSummary:
    """Public entrypoint for drafter code (see design D4)."""
    if operation == "command":
        return _command_risk(payload, server)
    if operation == "upload":
        return _upload_risk(payload, server)
    if operation == "mkdir":
        return _mkdir_risk(payload, server)
    if operation == "delete":
        return _delete_risk(payload, server)
    raise ArtifactError(
        failure_class="artifact_invalid",
        exit_code=53,
        message=f"unknown operation for risk computation: {operation!r}",
    )


# Private alias to match design.md §3 D4 wording.
_compute_risk = compute_risk


# ---- result.json write (D9 + C9) ------------------------------------------


def save_result(
    art: RequestArtifact,
    result_dict: dict[str, Any],
    redactor: Redactor | None,
    *,
    out_dir: Path | None = None,
) -> Path:
    """Write ``<id>.result.json`` next to the artifact.

    ``redactor=None`` bypasses redaction (audit-only ``--no-redact`` path);
    field names are unchanged either way so downstream readers don't branch
    on a schema variant.

    The result dict must already contain everything except the
    redaction-applied stdout/stderr; this helper applies the redactor (if
    any) to ``stdout_redacted`` and ``stderr_redacted`` keys, then writes
    atomically to ``<id>.result.json``.

    Returns the result path.
    """
    base_dir = out_dir or Path(".")
    result_path = base_dir / f"{art.request_id}.result.json"
    out = dict(result_dict)
    out.setdefault("version", SCHEMA_VERSION)
    out.setdefault("request_id", art.request_id)
    if redactor is not None:
        if "stdout_redacted" in out and isinstance(out["stdout_redacted"], str):
            redacted, _ = redactor.apply(out["stdout_redacted"])
            out["stdout_redacted"] = redacted
        if "stderr_redacted" in out and isinstance(out["stderr_redacted"], str):
            redacted, _ = redactor.apply(out["stderr_redacted"])
            out["stderr_redacted"] = redacted
    _atomic_write_json(result_path, out)
    return result_path


__all__ = [
    "ARTIFACT_FILE_MODE",
    "ArtifactError",
    "DEFAULT_EXPIRES_SECONDS",
    "MAX_EXPIRES_SECONDS",
    "MAX_UPLOAD_BYTES",
    "MIN_EXPIRES_SECONDS",
    "Operation",
    "RequestArtifact",
    "RiskSummary",
    "SCHEMA_VERSION",
    "SSH_CORE_SCRIPTS_DIR",
    "Scope",
    "allocate_request_id",
    "compute_risk",
    "iso_utc_now",
    "iso_utc_offset",
    "parse_expires_in",
    "save_result",
]

# Keep linter happy on `sys` — imported for future stderr helpers.
_ = sys
