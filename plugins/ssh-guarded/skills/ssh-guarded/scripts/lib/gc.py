"""Age-based cleanup for ssh-guarded's runtime artefacts.

``reports/`` accumulates one JSON file per drafted request, one ``.result.json``
per execution, one ``.lock`` per in-flight execute and one JSON per detached
job. Nothing ever deleted them: the skill's own README said cleanup was "the
user's responsibility" and named a ``--gc-older-than`` flag that did not exist.

This module is that flag's implementation. Rules, in order of importance:

* **Never delete a live lock.** ``<id>.lock`` belongs to a running
  ``run_request --execute``; age says nothing about whether it is still held.
* **Never delete a pending request.** A drafted artefact whose ``expires_at``
  is still in the future is waiting for a human decision.
* **Never follow a symlink out of the tree**, and never touch anything whose
  name does not match the artefact pattern.

Everything else older than the cut-off goes. ``dry_run`` reports the same set
without unlinking, which is what the CLI defaults to.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: ``<op>-<8 hex>.json`` / ``...result.json`` / ``...lock`` / ``job-<hex>.json``
_ARTIFACT_RE = re.compile(
    r"^(?:"
    r"(?:command|upload|mkdir|delete)-[0-9a-f]{8}(?:\.result)?\.json"
    r"|(?:command|upload|mkdir|delete)-[0-9a-f]{8}\.lock"
    r"|job-[0-9a-f]{8}(?:\.result)?\.json"
    r")$"
)

_LOCK_SUFFIX = ".lock"
_RESULT_SUFFIX = ".result.json"


@dataclass(frozen=True)
class GcEntry:
    """One candidate: what it is, how old, and what was decided."""

    path: str
    kind: str
    age_seconds: int
    action: str  # "deleted" | "would-delete" | "kept"
    reason: str


@dataclass(frozen=True)
class GcResult:
    root: str
    older_than_seconds: int
    dry_run: bool
    scanned: int
    entries: list[GcEntry]
    errors: list[dict[str, str]]

    @property
    def deleted(self) -> int:
        return sum(1 for e in self.entries if e.action == "deleted")

    @property
    def would_delete(self) -> int:
        return sum(1 for e in self.entries if e.action == "would-delete")

    def as_data(self) -> dict:
        return {
            "root": self.root,
            "older_than_seconds": self.older_than_seconds,
            "dry_run": self.dry_run,
            "scanned": self.scanned,
            "deleted": self.deleted,
            "would_delete": self.would_delete,
            "entries": [
                {
                    "path": e.path,
                    "kind": e.kind,
                    "age_seconds": e.age_seconds,
                    "action": e.action,
                    "reason": e.reason,
                }
                for e in self.entries
            ],
            "errors": self.errors,
        }


def parse_age(spec: str) -> int:
    """Parse ``7d`` / ``12h`` / ``30m`` / ``90s`` / ``3600`` into seconds.

    A bare integer is seconds. Raises ``ValueError`` for anything else so the
    CLI can map it onto ``invalid_arguments``.
    """
    text = (spec or "").strip().lower()
    if not text:
        raise ValueError("empty duration")
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text)
    if match is None:
        raise ValueError(f"not a duration: {spec!r}")
    value = int(match.group(1))
    unit = match.group(2)
    factor = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if value <= 0:
        raise ValueError("duration must be positive")
    return value * factor


def _kind_of(path: Path) -> str:
    name = path.name
    if name.endswith(_LOCK_SUFFIX):
        return "lock"
    if name.endswith(_RESULT_SUFFIX):
        return "result"
    if name.startswith("job-"):
        return "job"
    return "request"


def _is_pending_request(path: Path, now: float) -> tuple[bool, str]:
    """True while a drafted request has not expired yet."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ""
    expires_at = raw.get("expires_at") if isinstance(raw, dict) else None
    if not isinstance(expires_at, str):
        return False, ""
    try:
        deadline = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False, ""
    if deadline.timestamp() > now:
        return True, f"request still valid until {expires_at}"
    return False, ""


def _lock_is_held(path: Path) -> bool:
    """True when something still holds the lock.

    A ``.lock`` is created with ``O_EXCL`` and removed on exit; on POSIX the
    holder also keeps an ``flock``. Trying the flock is the only reliable test,
    and it is unavailable on Windows — there the conservative answer is "held",
    because deleting a live lock is worse than leaving a stale file behind.
    """
    try:
        import fcntl  # noqa: PLC0415 - POSIX only
    except ImportError:
        return True
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # still locked
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def gc_reports(
    reports_dir: Path,
    older_than_seconds: int,
    *,
    now: float | None = None,
    dry_run: bool = True,
) -> GcResult:
    """Delete (or report) artefacts under ``reports_dir`` older than the cut-off."""
    root = Path(reports_dir)
    current = time.time() if now is None else now
    entries: list[GcEntry] = []
    errors: list[dict[str, str]] = []
    scanned = 0

    if not root.is_dir():
        return GcResult(str(root), older_than_seconds, dry_run, 0, [], [])

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if not _ARTIFACT_RE.match(path.name):
            continue
        scanned += 1
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        age = int(current - stat.st_mtime)
        kind = _kind_of(path)

        if age < older_than_seconds:
            entries.append(GcEntry(str(path), kind, age, "kept", "younger than cut-off"))
            continue
        if kind == "lock" and _lock_is_held(path):
            entries.append(GcEntry(str(path), kind, age, "kept", "lock is held"))
            continue
        if kind == "request":
            pending, why = _is_pending_request(path, current)
            if pending:
                entries.append(GcEntry(str(path), kind, age, "kept", why))
                continue

        if dry_run:
            entries.append(
                GcEntry(str(path), kind, age, "would-delete", "older than cut-off")
            )
            continue
        try:
            path.unlink()
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            entries.append(GcEntry(str(path), kind, age, "kept", f"unlink failed: {exc}"))
            continue
        entries.append(GcEntry(str(path), kind, age, "deleted", "older than cut-off"))

    return GcResult(str(root), older_than_seconds, dry_run, scanned, entries, errors)
