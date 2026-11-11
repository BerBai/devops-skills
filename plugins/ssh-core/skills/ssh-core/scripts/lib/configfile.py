"""Locked, atomic, versioned writes for ``~/.ssh/config`` (SSH-02/03/04/05).

The original implementation copied the file to ``<cfg>.bak.<unix-ts>`` and
then wrote ``<cfg>.tmp`` before ``os.replace``. Three defects followed:

* **No lock (SSH-03).** Six concurrent ``create`` calls lost three updates
  that had already been reported as successful, and the rest failed with
  ``permission denied`` / ``WinError 2``. Fixed with an advisory lock held
  across the whole read-modify-write cycle (:class:`ConfigLock`).
* **Fixed tmp name (SSH-03).** A shared ``<cfg>.tmp`` lets one writer's
  ``os.replace`` move another's scratch file. Now ``<cfg>.tmp.<pid>.<rand>``.
* **Second-resolution backups (SSH-04).** Four writes inside one second
  shared one ``.bak.<ts>`` and silently overwrote history. Now the suffix is
  ``<epoch-ms>-<pid>-<counter>`` and is guaranteed unique on disk.

Everything here is intentionally dependency-free (``msvcrt`` / ``fcntl`` only)
so it works on a bare Windows box and on Kylin alike.
"""

from __future__ import annotations

import contextlib
import os
import random
import shutil
import time
from pathlib import Path

from . import write_text_lf

_LOCK_SUFFIX = ".lock"
_BACKUP_SUFFIX = ".bak"
#: Written by ``restore`` before it overwrites the file. It must NOT count as a
#: restore candidate (FA-40): every restore would otherwise create a new
#: "latest" backup holding the state it just replaced, so two consecutive
#: ``restore --from latest`` flip-flopped the file between two revisions.
_SAFETY_SUFFIX = ".pre-restore"
#: How long to wait for the lock before giving up (seconds).
LOCK_TIMEOUT_S = 10.0
#: How many times ``os.replace`` is retried on a transient Windows sharing error.
REPLACE_RETRIES = 3


class ConfigLockTimeout(OSError):
    """Raised when another process holds the config lock for too long."""


class ConfigLock:
    """Cross-platform advisory lock next to the config file.

    POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` on a byte range
    of a dedicated ``<cfg>.lock`` file. The lock file is *never* deleted: on
    POSIX, unlinking a locked file is a classic way to hand two processes the
    same lock, and an empty ``.lock`` next to ``~/.ssh/config`` is harmless.
    """

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_S) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None

    def __enter__(self) -> ConfigLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self) -> None:
        lock_path = self.path.with_name(self.path.name + _LOCK_SUFFIX)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(lock_path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise ConfigLockTimeout(
                        f"timed out after {self.timeout:.0f}s waiting for the "
                        f"config lock {lock_path} (another ssh-core process is "
                        "editing the same file)"
                    )
                time.sleep(0.02 + random.random() * 0.05)

    def _try_lock(self) -> None:
        assert self._fh is not None
        if os.name == "nt":
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                with contextlib.suppress(OSError):
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None


_counter = 0


def next_backup_path(path: Path) -> Path:
    """Return a backup path that does not exist yet (SSH-04).

    ``<epoch-ms>-<pid>-<counter>`` — monotonic within a process, unique across
    processes, and still sortable by time for humans.
    """
    global _counter
    path = Path(path)
    while True:
        _counter += 1
        stamp = f"{int(time.time() * 1000)}-{os.getpid()}-{_counter}"
        candidate = path.with_name(f"{path.name}{_BACKUP_SUFFIX}.{stamp}")
        if not candidate.exists():
            return candidate


def atomic_write_with_backup(path: Path, content: str) -> Path:
    """Back up ``path``, then atomically replace it with ``content``.

    Callers should hold :class:`ConfigLock` around the *whole* read-modify-write
    cycle; this helper only guarantees the write itself is atomic. Returns the
    backup path that was created.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file does not exist: {path}")

    backup = next_backup_path(path)
    shutil.copy2(path, backup)
    _safe_chmod(backup, path)

    tmp = path.with_name(
        f"{path.name}.tmp.{os.getpid()}.{random.randrange(1 << 30):08x}"
    )
    try:
        # write_text_lf, not Path.write_text(newline=""): the keyword is 3.10+
        # and this project's floor is 3.8 (FA-08). On Kylin 3 / CentOS 7 the
        # old call raised TypeError, so every config write failed while reads
        # kept working — the worst possible failure mode.
        write_text_lf(tmp, content)
        _safe_chmod(tmp, path)
        _replace_with_retry(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
        raise
    return backup


def _safe_chmod(target: Path, source: Path) -> None:
    """Best-effort mode copy; Windows ACLs are not modelled by ``st_mode``."""
    with contextlib.suppress(OSError, AttributeError):
        target.chmod(source.stat().st_mode & 0o777)


def _replace_with_retry(tmp: Path, path: Path) -> None:
    last: OSError | None = None
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:  # WinError 5/32: reader has the file open
            last = exc
            time.sleep(0.05 * (attempt + 1))
    assert last is not None
    raise last


def list_backups(path: Path) -> list[Path]:
    """All ``<name>.bak.*`` files for ``path``, newest last."""
    path = Path(path)
    prefix = f"{path.name}{_BACKUP_SUFFIX}."
    candidates = [
        p for p in path.parent.glob(f"{path.name}{_BACKUP_SUFFIX}.*") if p.name.startswith(prefix)
    ]
    # The filename carries ``<epoch-ms>-<pid>-<counter>``. mtime is not a
    # reliable order: copy2 preserves the *source* mtime, so three backups of
    # the same read-then-write cycle can share it and sorted() would fall back
    # to the filesystem's arbitrary glob order.
    return sorted(candidates, key=lambda p: p.name)


def latest_backup(path: Path) -> Path | None:
    backups = list_backups(path)
    return backups[-1] if backups else None


def earliest_backup(path: Path) -> Path | None:
    """Oldest backup for ``path`` (``restore --from earliest``)."""
    backups = list_backups(path)
    return backups[0] if backups else None


#: Tokens accepted by ``--from`` in addition to a path/suffix fragment.
_EARLIEST_TOKENS = frozenset({"earliest", "oldest", "first"})
_LATEST_TOKENS = frozenset({"latest", "newest", "last"})


def find_backup(path: Path, token: str | None) -> Path | None:
    """Resolve ``--from`` against the backup set.

    ``token`` may be a full path, a bare suffix (``1793345880-1234-1``),
    ``latest``/``newest``/``last`` (the default), or
    ``earliest``/``oldest``/``first`` (FA-40: those three used to answer
    ``not_found`` even though the set obviously has a first element).
    """
    path = Path(path)
    normalised = (token or "").strip().lower()
    if not normalised or normalised in _LATEST_TOKENS:
        return latest_backup(path)
    if normalised in _EARLIEST_TOKENS:
        return earliest_backup(path)
    candidate = Path(token or "").expanduser()
    if candidate.is_file():
        return candidate
    for backup in list_backups(path):
        if normalised in backup.name.lower():
            return backup
    return None


def restore_backup(path: Path, backup: Path) -> Path:
    """Copy ``backup`` over ``path`` keeping a safety copy of the current state.

    The safety copy uses :data:`_SAFETY_SUFFIX`, not ``.bak.``, so it is not a
    ``--from`` candidate and cannot become tomorrow's "latest" (FA-40).
    """
    path = Path(path)
    current_backup: Path | None = None
    if path.exists():
        current_backup = path.with_name(
            f"{path.name}{_SAFETY_SUFFIX}.{int(time.time() * 1000)}-{os.getpid()}"
        )
        shutil.copy2(path, current_backup)
    shutil.copy2(backup, path)
    _safe_chmod(path, backup)
    return current_backup or path


__all__ = [
    "ConfigLock",
    "ConfigLockTimeout",
    "atomic_write_with_backup",
    "earliest_backup",
    "find_backup",
    "latest_backup",
    "list_backups",
    "next_backup_path",
    "restore_backup",
]
