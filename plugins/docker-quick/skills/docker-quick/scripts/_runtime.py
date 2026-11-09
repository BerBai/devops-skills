"""Container-runtime resolution and honest local-exit classification (DQ-01).

Two defects in one place:

**DQ-01a — ``remote_error`` for a missing local binary.**
``docker``/``podman`` not being installed is not a remote failure. Reporting
``remote_error`` (as all three docker-quick scripts did) sends the operator to
check the network and the SSH alias; the actual fix is ``apt install docker``
on *this* host. The class is ``dependency_missing`` (exit 3), which is what the
rest of the marketplace uses for this.

**DQ-01b — no runtime auto-detection.**
``--runtime`` defaulted to ``docker``, so a podman-only box needed the flag on
every call. ``iac-state`` already autodetects ``tofu`` → ``terraform``
(``lib/terraform.py:resolve_tool``); this gives docker-quick the same
behaviour: ``docker`` first, then ``podman``, and an honest
``dependency_missing`` when neither is present.

The remote route keeps its existing contract: the binary name is sent as-is
because PATH resolves on the far side, and auto-detection stays local-only.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys

#: Explicit runtimes a caller may pass.
ALLOWED_RUNTIMES = ("docker", "podman")

#: Sentinel meaning "pick the first runtime that exists".
RUNTIME_AUTO = "auto"

#: Order tried by auto-detection. Docker first: it is the more common install,
#: and a box with both usually means "use docker" (podman-docker shims exist,
#: but then `docker` IS podman and the argv is identical anyway).
RUNTIME_PREFERENCE = ("docker", "podman")

EXIT_DEPENDENCY_MISSING = 3


def utf8_stdio() -> None:
    """Force UTF-8 on this interpreter's stdout/stderr (DQ-02).

    A *piped* stdout on Windows defaults to the ANSI code page (cp936), so
    ``json.dump(..., ensure_ascii=False)`` writes GBK bytes and any consumer
    reading the envelope as UTF-8 gets ``\\ufffd`` instead of the Chinese text.
    The JSON still parses, so nothing looks broken — the data is just wrong.

    Same helper as ``remote-debug/_local_exec.utf8_stdio``; duplicated because
    plugins do not import across package boundaries (CONTRIBUTING.md line 29).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def find_runtime(preference: tuple[str, ...] = RUNTIME_PREFERENCE) -> str | None:
    """Return the first installed runtime, or ``None``."""
    for candidate in preference:
        if shutil.which(candidate):
            return candidate
    return None


def resolve_runtime(
    requested: str,
    *,
    host: str,
    finder=None,
) -> tuple[str, str | None]:
    """Resolve ``--runtime`` for one invocation.

    Returns ``(runtime, error)``:

    * explicit ``docker`` / ``podman`` → ``(value, None)``. An explicit choice
      is never second-guessed, not even when the binary is absent: the far
      side of a remote call has its own PATH.
    * ``auto`` on ``local`` → the first installed runtime. When neither is
      found it falls back to ``"docker"`` **without** an error: the actual
      invocation then reports ``dependency_missing`` (exit 3) with a message
      naming the binary it looked for. A PATH probe is a hint, not proof — a
      shim, an alias or a container-local install can make ``which`` wrong,
      and gating on it would refuse a call that would have worked.
    * ``auto`` on a remote host → ``"docker"``. Auto-detecting there would cost
      an SSH round trip per call; the remote shell resolves the name.

    ``finder`` is a test hook (``None`` means :func:`find_runtime`); it is
    resolved at call time so tests can monkeypatch this module.

    The ``error`` element is kept in the signature for callers that want to
    add a hard gate later; nothing produces it today.
    """
    if requested != RUNTIME_AUTO:
        return requested, None
    if host != "local":
        return RUNTIME_PREFERENCE[0], None
    lookup = finder or find_runtime
    return (lookup() or RUNTIME_PREFERENCE[0]), None


def classify_local_exit(
    exc: BaseException | None, returncode: int | None
) -> str | None:
    """failure_class for a *completed* local runtime invocation.

    Unlike :func:`classify_spawn_failure` this is not about the spawn: it
    interprets the exit code of a command that ran. ``None`` means "it ran and
    exited 0"; any non-zero exit keeps the historical ``remote_error`` label so
    existing callers that branch on it keep working — the *missing binary*
    case no longer shares that label, which was the actual defect (DQ-01).
    """
    if isinstance(exc, FileNotFoundError):
        return "dependency_missing"
    if isinstance(exc, NotADirectoryError):
        return "project_dir_invalid"
    if exc is not None:
        return "local_error"
    return "remote_error" if returncode else None


#: errno / winerror values that specifically mean "the directory is wrong".
#: 267 == ERROR_DIRECTORY, 20 == ENOTDIR. errno 2 (ENOENT) is ambiguous: it is
#: what CPython raises for both a missing executable and a missing cwd.
_NOT_A_DIR_ERRNOS = frozenset({20})
_NOT_A_DIR_WINERRORS = frozenset({267})


def classify_spawn_failure(
    exc: BaseException, cwd: str | None
) -> str:
    """Decide whether a failed local spawn was a bad cwd or a missing binary.

    ``subprocess.run(..., cwd=bad)`` and ``subprocess.run(["no-such-bin"])``
    raise the *same* exception on Windows: ``FileNotFoundError`` with
    ``errno=2`` and ``filename=None`` (CPython does not populate ``filename``
    on Windows). Reporting the wrong one sends the operator to the wrong fix,
    so the ambiguity is resolved in this order:

    1. ``NotADirectoryError``, or errno/winerror that names a directory
       problem → ``project_dir_invalid``.
    2. ``FileNotFoundError`` whose ``filename`` is the cwd (POSIX) →
       ``project_dir_invalid``.
    3. ``FileNotFoundError`` while the cwd does not exist on disk →
       ``project_dir_invalid``. This is the Windows tiebreak: the cwd is
       checkable, the binary is not (a PATH probe can be wrong), so a
       demonstrably bad directory wins.
    4. otherwise → ``dependency_missing``.
    """
    if isinstance(exc, NotADirectoryError):
        return "project_dir_invalid"
    errno_val = getattr(exc, "errno", None)
    winerror_val = getattr(exc, "winerror", None)
    if errno_val in _NOT_A_DIR_ERRNOS or winerror_val in _NOT_A_DIR_WINERRORS:
        return "project_dir_invalid"
    filename = getattr(exc, "filename", None)
    if cwd is not None and filename is not None and filename == cwd:
        return "project_dir_invalid"
    if cwd is not None and not os.path.isdir(cwd):
        return "project_dir_invalid"
    if isinstance(exc, FileNotFoundError):
        return "dependency_missing"
    return "local_error"


__all__ = [
    "ALLOWED_RUNTIMES",
    "EXIT_DEPENDENCY_MISSING",
    "RUNTIME_AUTO",
    "RUNTIME_PREFERENCE",
    "classify_local_exit",
    "classify_spawn_failure",
    "find_runtime",
    "resolve_runtime",
]
