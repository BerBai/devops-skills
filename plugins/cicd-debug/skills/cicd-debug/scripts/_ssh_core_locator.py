"""Locate the ssh-core plugin from a sibling devops-skills plugin.

Every diagnostic plugin in this marketplace shells out to ssh-core's
``ssh_execute.py`` (and sometimes ``ssh_upload.py``) exactly like a user would;
none of them import its Python. This module is the *single* place that answers
"where is ssh-core?" for one plugin, so the answer can be overridden instead of
being hardcoded as ``Path(__file__).parents[4]``.

The hardcoded form broke as soon as the plugins were installed the way the
marketplace installs them: each plugin lands in its own directory, so
``parents[4]`` no longer points at a ``plugins/`` folder that contains ssh-core.

Resolution order (first hit wins), independent of where this file itself sits:

1. ``$DEVOPS_SSH_CORE_SCRIPTS`` -- an explicit ``.../ssh-core/scripts`` dir.
2. ``$DEVOPS_SSH_CORE_DIR``     -- a ``.../ssh-core`` skill root.
3. Every ancestor of this file, innermost first, joined with
   ``ssh-core/skills/ssh-core/scripts`` (covers the repo layout
   ``plugins/<plugin>/skills/<plugin>/scripts`` at any nesting depth).
4. Installed agent roots: ``~/.claude/plugins/``, ``~/.dsh/skills/``,
   ``~/.agents/skills/``, ``~/.codex/skills/`` -- each directly and one level
   down, for the marketplace layout.

This file is duplicated verbatim into every plugin that needs it (plugins are
distributed independently and must not import across plugin boundaries). The
copies are generated from ``scripts/templates/_ssh_core_locator.py``: edit the
template, then run ``python scripts/sync_vendored.py``. Never edit a copy in
place -- ``tests/test_manifests.py`` fails the build when one drifts.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Environment variable pointing at the ``ssh-core`` skill root
#: (the directory that holds ``SKILL.md`` and ``scripts/``).
SSH_CORE_DIR_ENV = "DEVOPS_SSH_CORE_DIR"

#: Environment variable pointing straight at the ``scripts/`` directory.
SSH_CORE_SCRIPTS_ENV = "DEVOPS_SSH_CORE_SCRIPTS"

#: Path from any ancestor to ssh-core's ``scripts/`` dir in the repo layout.
_SKILL_RELATIVE = ("ssh-core", "skills", "ssh-core", "scripts")

#: Entry point that must exist for a directory to count as ssh-core's scripts.
_ENTRY_POINT = "ssh_execute.py"

#: Roots used by the agent ecosystems this marketplace publishes into.
_AGENT_ROOTS = (
    (".claude", "plugins"),
    (".dsh", "skills"),
    (".agents", "skills"),
    (".codex", "skills"),
)


def _is_ssh_core_scripts(path: Path) -> bool:
    """A usable ssh-core scripts dir must contain the exec entry point."""
    return (path / _ENTRY_POINT).is_file()


def _candidates(this_file: Path) -> list[Path]:
    """Return candidate ``scripts/`` dirs for ssh-core, best guess first."""
    out: list[Path] = []

    explicit_scripts = os.environ.get(SSH_CORE_SCRIPTS_ENV, "").strip()
    if explicit_scripts:
        out.append(Path(explicit_scripts).expanduser())

    explicit_dir = os.environ.get(SSH_CORE_DIR_ENV, "").strip()
    if explicit_dir:
        root = Path(explicit_dir).expanduser()
        out.append(root / "scripts")
        out.append(root)

    for parent in this_file.parents:
        out.append(parent.joinpath(*_SKILL_RELATIVE))

    home = Path.home()
    for parts in _AGENT_ROOTS:
        base = home.joinpath(*parts)
        if not base.is_dir():
            continue
        out.append(base.joinpath(*_SKILL_RELATIVE))
        try:
            children = list(base.iterdir())
        except OSError:  # pragma: no cover - unreadable home dir
            continue
        for child in children:
            if child.is_dir():
                out.append(child.joinpath(*_SKILL_RELATIVE))
    return out


def find_ssh_core_scripts(this_file: Path | None = None) -> Path | None:
    """Return ssh-core's ``scripts/`` directory, or ``None`` when not found.

    ``this_file`` defaults to *this* module's location; tests may pass any path
    to simulate a plugin installed somewhere unexpected.
    """
    anchor = Path(this_file) if this_file is not None else Path(__file__)
    try:
        anchor = anchor.resolve()
    except OSError:  # pragma: no cover - broken symlink
        return None
    seen: set[Path] = set()
    for candidate in _candidates(anchor):
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover - broken symlink
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_ssh_core_scripts(resolved):
            return resolved
    return None


def ssh_core_script(name: str, this_file: Path | None = None) -> Path:
    """Return the path to ``name`` inside ssh-core's ``scripts/`` dir.

    The path is returned even when it does not exist, so callers can report
    *where* they looked. Use :func:`require_ssh_core_script` to fail early with
    an actionable message.
    """
    found = find_ssh_core_scripts(this_file)
    if found is not None:
        return found / name
    anchor = Path(this_file) if this_file is not None else Path(__file__)
    # Nothing resolved: name the path the repo layout *would* use.
    try:
        anchor = anchor.resolve()
    except OSError:  # pragma: no cover - broken symlink
        pass
    for parent in anchor.parents:
        candidate = parent.joinpath(*_SKILL_RELATIVE)
        if (candidate / _ENTRY_POINT).is_file():
            return candidate / name
    return anchor.parent / name


def require_ssh_core_script(
    name: str, *, prog: str, this_file: Path | None = None
) -> Path:
    """Like :func:`ssh_core_script`, but raises ``FileNotFoundError`` if absent.

    The message names both environment variables so the fix is a one-liner even
    when the plugins were installed into unrelated directories.
    """
    path = ssh_core_script(name, this_file)
    if not path.is_file():
        raise FileNotFoundError(
            f"{prog}: ssh-core's {name} was not found (looked for {path}). "
            "ssh-core provides the transport for this plugin; install the "
            f"ssh-core plugin, or set {SSH_CORE_DIR_ENV}=<path to the ssh-core "
            f"skill dir> (or {SSH_CORE_SCRIPTS_ENV}=<path to its scripts/ dir>)."
        )
    return path
