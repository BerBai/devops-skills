"""Cross-platform exclusive file lock for ssh-guarded ``run_request --execute``.

Stable surface::

    @contextmanager
    def exclusive_lock(path: Path) -> Iterator[LockInfo]: ...

    class LockHeld(Exception):
        holder_pid: int | None

    @dataclass
    class LockInfo:
        path: Path        # the .lock sidecar file
        holder_pid: int   # current process PID

Design source: design.md §1 C6 + §3 D7 step 7 + §4.3.

POSIX:
    Acquires ``<path>.lock`` via ``fcntl.flock(LOCK_EX | LOCK_NB)``.
    Best-effort ``holder_pid`` of the contending holder via ``/proc/locks``
    (Linux) or ``fuser`` (cross-Unix). Either may be unavailable → None.

Windows / non-POSIX (FR-7 degraded mode):
    Falls back to ``<path>.lock`` file existence + sentinel content equal
    to the holding PID. Acquired by writing our PID to a fresh
    ``<path>.lock``; released by deleting it. Not crash-safe (stale lock
    handling left for v1.1 follow-up per PRD Notes O1).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl  # type: ignore[import-not-found]
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover — Windows path tested via monkeypatch
    fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

# /proc/locks parsing constants (Linux kernel format, see proc(5)).
_PROC_LOCKS_MIN_FIELDS = 7
_PROC_LOCKS_DEVINODE_PARTS = 2


@dataclass
class LockInfo:
    """Returned by the context manager on successful acquire."""

    path: Path
    holder_pid: int


class LockHeld(Exception):
    """Raised when the lock is already held by another process."""

    def __init__(self, holder_pid: int | None) -> None:
        super().__init__(f"lock held by pid={holder_pid}")
        self.holder_pid = holder_pid


def _is_posix() -> bool:
    """Override-friendly POSIX detection (tests monkeypatch ``sys.platform``)."""
    return sys.platform != "win32" and _HAS_FCNTL


# --- POSIX path ----------------------------------------------------------------


def _read_holder_pid_posix(lock_path: Path) -> int | None:
    """Best-effort holder PID via ``/proc/locks`` or ``fuser``."""
    # Try /proc/locks (Linux-only) — match the inode number.
    proc_locks = Path("/proc/locks")
    if proc_locks.exists():
        try:
            st = lock_path.stat()
            target_ino = st.st_ino
            for line in proc_locks.read_text().splitlines():
                # Format: "1: POSIX  ADVISORY  WRITE 1234 fd:00:inode 0 EOF"
                fields = line.split()
                if len(fields) < _PROC_LOCKS_MIN_FIELDS:
                    continue
                # Field 4 is PID, field 5 is "MAJ:MIN:INO"
                pid_str = fields[4]
                dev_inode = fields[5]
                if ":" in dev_inode:
                    parts = dev_inode.rsplit(":", 1)
                    if (
                        len(parts) == _PROC_LOCKS_DEVINODE_PARTS
                        and parts[1].isdigit()
                        and int(parts[1]) == target_ino
                        and pid_str.isdigit()
                    ):
                        return int(pid_str)
        except (OSError, ValueError):
            pass
    # Try fuser as fallback.
    fuser = shutil.which("fuser")
    if fuser is not None:
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [fuser, str(lock_path)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            # fuser prints PIDs on stdout (or stderr on some unices); merge.
            text = (proc.stdout + " " + proc.stderr).strip()
            for tok in text.replace(":", " ").split():
                if tok.isdigit():
                    return int(tok)
        except (subprocess.SubprocessError, OSError):
            pass
    return None


@contextmanager
def _exclusive_lock_posix(path: Path) -> Iterator[LockInfo]:
    lock_path = path.with_suffix(path.suffix + ".lock") if path.suffix else Path(
        str(path) + ".lock"
    )
    # Open / create the lock file (mode 0o600).
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[union-attr]
        except (OSError, BlockingIOError) as exc:
            # Read holder PID best-effort; close fd before reading so the
            # lookup does not see *our* fd as the holder.
            os.close(fd)
            fd = -1
            holder_pid = _read_holder_pid_posix(lock_path)
            raise LockHeld(holder_pid) from exc
        # Write our PID into the sentinel file for human debugging.
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield LockInfo(path=lock_path, holder_pid=os.getpid())
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


# --- Windows / degraded path --------------------------------------------------


def _read_sentinel_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if text.isdigit():
        return int(text)
    return None


@contextmanager
def _exclusive_lock_degraded(path: Path) -> Iterator[LockInfo]:
    lock_path = path.with_suffix(path.suffix + ".lock") if path.suffix else Path(
        str(path) + ".lock"
    )
    try:
        # O_CREAT|O_EXCL is the atomic acquire on this branch.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        holder_pid = _read_sentinel_pid(lock_path)
        raise LockHeld(holder_pid) from exc
    try:
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        try:
            yield LockInfo(path=lock_path, holder_pid=os.getpid())
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()
    except BaseException:
        # On any exception during yield-setup, clean up the sentinel.
        with contextlib.suppress(OSError):
            lock_path.unlink()
        raise


# --- Public entrypoint --------------------------------------------------------


@contextmanager
def exclusive_lock(path: Path) -> Iterator[LockInfo]:
    """Acquire an exclusive lock on ``<path>.lock`` for the duration of the block.

    POSIX → ``fcntl.flock(LOCK_EX | LOCK_NB)``.
    Non-POSIX / no fcntl → ``<path>.lock`` existence + PID sentinel.

    Raises ``LockHeld`` if the lock is already held by another process.
    """
    if _is_posix():
        with _exclusive_lock_posix(path) as info:
            yield info
    else:
        with _exclusive_lock_degraded(path) as info:
            yield info


__all__ = ["LockHeld", "LockInfo", "exclusive_lock"]
