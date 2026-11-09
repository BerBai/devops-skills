"""Remote ``$HOME`` resolution and ``~`` expansion, shared by every script.

Why this module exists (BUG-001): ``shlex.quote('~/workspace/x')`` returns
``'~/workspace/x'`` — the single quotes stop the *remote* shell from expanding
the tilde, so ``[ -d '~/workspace/x' ]`` is always false and a pre-check can
silently answer "does not exist" for a path that does exist. The fix is to
resolve the remote home once per invocation and quote the resulting **absolute**
path. ``run_request.py`` had that logic inline; ``request_delete.py`` did not,
which is exactly how the ``--recursive`` gate got bypassed.

Both drafters and the executor now share this implementation.

Stable surface::

    resolve_remote_home(server, *, timeout_seconds=10,
                        failure_class="transport_failed", exit_code=57) -> str
    expand_remote_tilde(path, remote_home) -> str
    uses_tilde(path) -> bool
"""

from __future__ import annotations

import subprocess

from ._errors import ArtifactError

#: Budget for the one-shot ``ssh <alias> 'printf %s "$HOME"'`` probe.
DEFAULT_HOME_TIMEOUT_SECONDS = 10

#: failure_class/exit_code used by the executor when the probe fails.
FAILURE_TRANSPORT = "transport_failed"
EXIT_TRANSPORT = 57


def uses_tilde(path: str) -> bool:
    """True when ``path`` needs remote-home resolution before it can be quoted."""
    return path == "~" or path.startswith("~/")


def resolve_remote_home(
    server: str,
    *,
    timeout_seconds: int = DEFAULT_HOME_TIMEOUT_SECONDS,
    failure_class: str = FAILURE_TRANSPORT,
    exit_code: int = EXIT_TRANSPORT,
) -> str:
    """Return the remote ``$HOME`` for ``server`` as an absolute POSIX path.

    One ``BatchMode`` SSH probe; no interactive fallback. Raises
    ``ArtifactError`` (default ``transport_failed``/57) on ssh failure,
    timeout, or non-absolute output — never silently falls back, because a
    wrong home silently targets the wrong files.

    Callers that have their own failure taxonomy pass ``failure_class`` /
    ``exit_code`` (``request_delete.py`` uses ``pre_check_stat_failed``/49 so a
    failed pre-check can never be mistaken for "the path does not exist").
    """
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout_seconds}",
        server,
        'printf %s "$HOME"',
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 5,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArtifactError(
            failure_class=failure_class,
            exit_code=exit_code,
            message=(
                f"resolving remote $HOME on '{server}' timed out after "
                f"{timeout_seconds}s"
            ),
        ) from exc
    except OSError as exc:
        raise ArtifactError(
            failure_class=failure_class,
            exit_code=exit_code,
            message=f"resolving remote $HOME on '{server}' failed: {exc}",
        ) from exc
    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[:200]
        raise ArtifactError(
            failure_class=failure_class,
            exit_code=exit_code,
            message=(
                f"resolving remote $HOME on '{server}' failed "
                f"(ssh exit={proc.returncode}): {stderr_tail or '<no stderr>'}"
            ),
        )
    home = (proc.stdout or "").strip()
    if not home.startswith("/"):
        raise ArtifactError(
            failure_class=failure_class,
            exit_code=exit_code,
            message=(
                f"resolving remote $HOME on '{server}' produced "
                f"non-absolute path: {home!r}"
            ),
        )
    return home


def expand_remote_tilde(path: str, remote_home: str) -> str:
    """Expand a leading ``~/`` (or a bare ``~``) to ``<remote_home>/...``.

    Only ``~/`` and ``~`` forms are touched; a bare absolute path and the
    ``~user/`` form pass through unchanged. ``remote_home`` must be an absolute
    POSIX path (guaranteed by :func:`resolve_remote_home`).
    """
    if path == "~":
        return remote_home
    if path.startswith("~/"):
        suffix = path[2:]
        return f"{remote_home}/{suffix}" if suffix else remote_home
    return path


__all__ = [
    "DEFAULT_HOME_TIMEOUT_SECONDS",
    "EXIT_TRANSPORT",
    "FAILURE_TRANSPORT",
    "expand_remote_tilde",
    "resolve_remote_home",
    "uses_tilde",
]
