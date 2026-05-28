"""Workdir sandbox + denylist enforcement for ssh-guarded.

Stable surface:
    resolve_project_id(args, env, cwd) -> str
    normalize_remote_path(remote_path: str) -> str   # expand ~, resolve .., reject escape
    is_inside_sandbox(remote_path: str, project_id: str) -> bool
    is_in_denylist(remote_path: str) -> bool

Design source: design.md §1 C3, C4, §4.1 + D2.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from ._errors import ArtifactError

# ---- Denylist (C4) ---------------------------------------------------------

# Prefix-match against the normalized remote_path (after ~ stays as `~/` per
# this module's normalize_remote_path — we do not resolve to remote $HOME).
_DENY_PREFIXES: tuple[str, ...] = (
    "~/.ssh/",
    "~/.aws/",
    "~/.kube/",
    "~/.gnupg/",
    "~/.config/gh/",
)

# Basename match. Any path segment matching one of these (case-insensitive
# per references/workdir-sandbox.md) triggers a denylist hit.
_DENY_BASENAMES_RE = re.compile(
    r"^(?:"
    r"\.env(?:\..+)?"
    r"|id_rsa(?:\..+)?"
    r"|id_ed25519.*"
    r"|id_ecdsa.*"
    r"|id_dsa.*"
    r"|.+\.pem"
    r"|.+\.key"
    r"|.+\.p12"
    r"|.+\.pfx"
    r"|authorized_keys"
    r"|known_hosts"
    r")$",
    re.IGNORECASE,
)


# ---- project_id resolution (D2) --------------------------------------------


def resolve_project_id(args: Any, env: dict[str, str], cwd: Path) -> str:
    """Resolve <project_id> by the locked priority order.

    1. ``--project-id`` explicit CLI flag (read via getattr — SUPPRESS-safe).
    2. ``SSH_GUARDED_PROJECT_ID`` env var.
    3. Local git repo basename via ``git rev-parse --show-toplevel``.

    Raises ``ArtifactError(failure_class='project_id_unresolved', exit_code=1, ...)``
    when none of the three sources resolves. Hard fail aligns with
    ``references/workdir-sandbox.md:22`` "missing → reject all writes".
    """
    explicit = getattr(args, "project_id", None)
    if explicit:
        return str(explicit)
    env_val = env.get("SSH_GUARDED_PROJECT_ID")
    if env_val:
        return env_val
    try:
        root = subprocess.check_output(  # noqa: S603 — fixed argv, no shell
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if root:
            return Path(root).name
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    raise ArtifactError(
        failure_class="project_id_unresolved",
        exit_code=1,
        message=(
            "--project-id not given, SSH_GUARDED_PROJECT_ID not set, and cwd "
            "is not a git repo. Resolve one before retrying."
        ),
    )


# ---- normalization ---------------------------------------------------------


def normalize_remote_path(remote_path: str) -> str:
    """Normalize a remote path for sandbox/denylist checks.

    POSIX semantics (remote is always POSIX even when this script runs on
    Windows). Preserves the literal ``~/`` prefix (we do NOT resolve to a
    machine-local $HOME). Resolves ``.`` and ``..`` segments. Empty
    segments collapse.

    Returns the cleaned path string. Raises ``ArtifactError`` (exit 50,
    ``workdir_outside_sandbox``) if the path normalizes to escape its own
    root via ``..`` traversal (e.g. ``~/foo/../../etc``).
    """
    if not remote_path:
        raise ArtifactError(
            failure_class="workdir_outside_sandbox",
            exit_code=50,
            message="empty remote_path",
        )
    if "\x00" in remote_path:
        raise ArtifactError(
            failure_class="workdir_outside_sandbox",
            exit_code=50,
            message=f"null byte in remote_path: {remote_path!r}",
        )

    leading_tilde = remote_path.startswith("~/")
    leading_slash = remote_path.startswith("/")
    body = remote_path[2:] if leading_tilde else remote_path.lstrip("/")

    parts: list[str] = []
    for raw in body.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            if not parts:
                # escape attempt above the anchor
                raise ArtifactError(
                    failure_class="workdir_outside_sandbox",
                    exit_code=50,
                    message=f"path escapes root via '..': {remote_path!r}",
                )
            parts.pop()
            continue
        parts.append(raw)

    cleaned = "/".join(parts)
    if leading_tilde:
        return "~/" + cleaned if cleaned else "~/"
    if leading_slash:
        return "/" + cleaned
    return cleaned


# ---- sandbox check (C3) ----------------------------------------------------


def is_inside_sandbox(remote_path: str, project_id: str) -> bool:
    """Return True iff ``remote_path`` resolves inside ``~/workspace/<project_id>/``.

    Both arguments are taken literally — ``~`` is preserved (this is the
    remote shell's convention; we do not query the remote HOME). The sandbox
    root is always ``~/workspace/<project_id>``.
    """
    try:
        normalized = normalize_remote_path(remote_path)
    except ArtifactError:
        return False
    sandbox_root = f"~/workspace/{project_id}"
    if normalized == sandbox_root:
        return True
    return normalized.startswith(sandbox_root + "/")


# ---- denylist check (C4) ---------------------------------------------------


def is_in_denylist(remote_path: str) -> bool:
    """Return True iff ``remote_path`` matches any denylist rule.

    Matches both:
      * prefix rules (``~/.ssh/**``, ``~/.aws/**`` ...)
      * any single path-segment basename pattern (id_rsa, .env, *.pem, ...)

    Inputs are normalized first; an unparseable path is treated as denied
    (defensive: better to over-reject than leak).
    """
    try:
        normalized = normalize_remote_path(remote_path)
    except ArtifactError:
        return True

    for prefix in _DENY_PREFIXES:
        if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
            return True

    # POSIX segment scan: every component checked.
    pp = PurePosixPath(normalized.lstrip("~/").lstrip("/"))
    return any(_DENY_BASENAMES_RE.match(segment) for segment in pp.parts)


__all__ = [
    "resolve_project_id",
    "normalize_remote_path",
    "is_inside_sandbox",
    "is_in_denylist",
]
