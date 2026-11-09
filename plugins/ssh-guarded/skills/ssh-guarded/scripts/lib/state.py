"""Where ssh-guarded keeps runtime state (GAP-006).

The skill used to write its request artifacts, results, locks and job manifests
into ``plugins/ssh-guarded/skills/ssh-guarded/reports/`` — i.e. *inside its own
installation directory*. That breaks on a read-only install (enterprise image,
container), loses pending approvals when the plugin is upgraded, and mixes two
projects' artifacts into one flat folder.

The layout is now user-scoped::

    $SSH_GUARDED_HOME/reports/<project_id>/requests/<request_id>.json
    $SSH_GUARDED_HOME/reports/<project_id>/requests/<request_id>.result.json
    $SSH_GUARDED_HOME/reports/<project_id>/requests/<request_id>.lock
    $SSH_GUARDED_HOME/reports/<project_id>/jobs/<job_id>.json

``$SSH_GUARDED_HOME`` defaults to ``~/.devops/ssh-guarded``
(``%USERPROFILE%\\.devops\\ssh-guarded`` on Windows). ``--reports-dir`` replaces
the ``.../reports`` level directly, so CI can point at a scratch directory
without also having to fake a home directory.

The plugin tree keeps only ``reports/README.md`` as documentation. Nothing is
ever written there any more; :func:`warn_legacy_reports` reports leftovers from
the old layout once per state home instead of deleting them.

Stable surface::

    state_home(*, env=None, home=None) -> Path
    reports_root(*, reports_dir=None, env=None, home=None) -> Path
    requests_dir(project_id, *, reports_dir=None, env=None, home=None) -> Path
    jobs_dir(project_id, *, reports_dir=None, env=None, home=None) -> Path
    project_ids(reports_root) -> list[str]
    legacy_reports_root() -> Path
    legacy_artifact_count() -> int
    warn_legacy_reports(*, stream=None, env=None, home=None) -> str | None
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Environment variable that relocates the whole state tree.
ENV_HOME = "SSH_GUARDED_HOME"

#: Default home, relative to the user's home directory.
DEFAULT_HOME_PARTS: tuple[str, ...] = (".devops", "ssh-guarded")

#: Directory under the state home that holds per-project artifacts.
REPORTS_DIRNAME = "reports"

#: Per-project sub-directories.
REQUESTS_DIRNAME = "requests"
JOBS_DIRNAME = "jobs"

#: Marker written into the state home after the legacy-layout note is shown.
LEGACY_NOTE_MARKER = ".legacy-reports-noted"


def state_home(
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return ``$SSH_GUARDED_HOME`` (default ``~/.devops/ssh-guarded``)."""
    environ = os.environ if env is None else env
    raw = (environ.get(ENV_HOME) or "").strip()
    if raw:
        return Path(raw).expanduser()
    base = Path.home() if home is None else Path(home)
    return base.joinpath(*DEFAULT_HOME_PARTS)


def reports_root(
    *,
    reports_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the directory whose children are ``<project_id>/`` folders.

    ``--reports-dir`` (``reports_dir``) wins over ``$SSH_GUARDED_HOME``; the
    default is ``<state home>/reports``.
    """
    if reports_dir is not None and str(reports_dir).strip():
        return Path(str(reports_dir)).expanduser()
    return state_home(env=env, home=home) / REPORTS_DIRNAME


def _project_dir(
    project_id: str,
    subdir: str,
    *,
    reports_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    if not project_id:
        raise ValueError("project_id must not be empty")
    root = reports_root(reports_dir=reports_dir, env=env, home=home)
    return root / project_id / subdir


def requests_dir(
    project_id: str,
    *,
    reports_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """``.../reports/<project_id>/requests`` for one project."""
    return _project_dir(
        project_id,
        REQUESTS_DIRNAME,
        reports_dir=reports_dir,
        env=env,
        home=home,
    )


def jobs_dir(
    project_id: str,
    *,
    reports_dir: str | Path | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """``.../reports/<project_id>/jobs`` for one project."""
    return _project_dir(
        project_id,
        JOBS_DIRNAME,
        reports_dir=reports_dir,
        env=env,
        home=home,
    )


def project_ids(root: str | Path) -> list[str]:
    """Return the project directories under ``root`` (sorted, dirs only)."""
    base = Path(root)
    try:
        children = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    return [p.name for p in children if not p.name.startswith(".")]


def legacy_reports_root() -> Path:
    """The pre-GAP-006 in-tree location: ``<skill root>/reports``."""
    # lib/state.py -> lib -> scripts -> <skill root>
    return Path(__file__).resolve().parents[2] / "reports"


def legacy_artifact_count() -> int:
    """Count artifacts still sitting in the old in-tree ``reports/`` tree."""
    root = legacy_reports_root()
    total = 0
    for sub in (REQUESTS_DIRNAME, JOBS_DIRNAME):
        directory = root / sub
        if not directory.is_dir():
            continue
        try:
            total += sum(1 for p in directory.glob("*.json") if p.is_file())
        except OSError:  # pragma: no cover - unreadable install dir
            continue
    return total


def warn_legacy_reports(
    *,
    stream: object | None = None,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Print (once per state home) a note about artifacts in the old location.

    Never deletes anything and never raises: a read-only legacy tree is exactly
    the situation this function exists to describe. Returns the message that was
    printed, or ``None`` when there was nothing to say.
    """
    count = legacy_artifact_count()
    if count <= 0:
        return None
    state = state_home(env=env, home=home)
    marker = state / LEGACY_NOTE_MARKER
    if marker.exists():
        return None
    message = (
        f"note: {count} ssh-guarded artifact(s) still live in the old in-tree "
        f"location {legacy_reports_root()}.\n"
        f"      Runtime state now goes to {reports_root(env=env, home=home)} "
        f"(override with {ENV_HOME} or --reports-dir).\n"
        "      Nothing was moved or deleted; move the files by hand if you "
        "still need them.\n"
    )
    out = sys.stderr if stream is None else stream
    try:
        out.write(message)  # type: ignore[attr-defined]
    except (OSError, ValueError, UnicodeEncodeError):  # pragma: no cover
        return message
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("seen\n", encoding="utf-8")
    except OSError:
        # Read-only state home: print every run rather than staying silent.
        pass
    return message


__all__ = [
    "DEFAULT_HOME_PARTS",
    "ENV_HOME",
    "JOBS_DIRNAME",
    "LEGACY_NOTE_MARKER",
    "REPORTS_DIRNAME",
    "REQUESTS_DIRNAME",
    "jobs_dir",
    "legacy_artifact_count",
    "legacy_reports_root",
    "project_ids",
    "reports_root",
    "requests_dir",
    "state_home",
    "warn_legacy_reports",
]
